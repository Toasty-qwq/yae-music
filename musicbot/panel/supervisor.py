"""Runs the bot as a child process: start / stop / restart, crash restarts, and its log stream."""
from __future__ import annotations

import asyncio
import collections
import logging
import logging.handlers
import os
import re
import secrets
import socket
import sys
import time

import aiohttp

from .. import settings

LINE_HEAD = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (\w+)")
LEVELS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}
HINTS = {
    2: "No Discord bot token yet — add it in Settings, then press Start.",
    3: "Discord rejected the bot token — paste a fresh one in Settings, then press Start.",
}
CRASH_WINDOW = 300  # s
CRASH_LIMIT = 5     # crashes inside the window before auto-restart gives up


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Supervisor:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"          # stopped | starting | running | stopping | crashed
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.hint: str | None = None
        self.lines: collections.deque[dict] = collections.deque(maxlen=5000)
        self.seq = 0
        self.subscribers: set[asyncio.Queue] = set()
        self.api_port = 0
        self.api_token = ""
        self._manual_stop = False
        self._crashes: collections.deque[float] = collections.deque(maxlen=CRASH_LIMIT)
        self._lock = asyncio.Lock()
        self._last_level = "info"
        self._restart_task: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None

        settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._file = logging.handlers.RotatingFileHandler(settings.LOG_DIR / "musicbot.log", maxBytes=2_000_000,
                                                          backupCount=5, encoding="utf-8")
        self._file.setFormatter(logging.Formatter("%(message)s"))

    @property
    def log_path(self):
        return settings.LOG_DIR / "musicbot.log"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # ---- log stream -----------------------------------------------------------------------------

    def emit(self, text: str, level: str | None = None) -> None:
        """Add a line to the buffer, the log file and every live viewer."""
        text = text.rstrip("\r\n")
        if level is None:
            m = LINE_HEAD.match(text)
            if m:
                level = LEVELS.get(m.group(1), "info")
            elif text.startswith("Traceback"):
                level = "error"
            else:  # continuation of the previous line (traceback, multi-line message)
                level = self._last_level
        self._last_level = level
        self.seq += 1
        entry = {"seq": self.seq, "t": time.time(), "level": level, "text": text}
        self.lines.append(entry)
        self._file.emit(logging.makeLogRecord({"msg": text}))
        try:  # also to our own stdout, so `docker logs` / CasaOS's log view show the same thing
            print(text, flush=True)
        except (OSError, UnicodeEncodeError):
            pass
        for q in list(self.subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # a stuck viewer just misses lines

    def panel_log(self, text: str, level: str = "info") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.emit(f"{stamp} {level.upper().replace('WARN', 'WARNING'):<7} panel: {text}", level)

    def clear(self) -> None:
        self.lines.clear()

    # ---- process control ------------------------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            pending = self._restart_task
            if pending and not pending.done() and pending is not asyncio.current_task():
                pending.cancel()  # a manual start replaces a scheduled crash-restart
            self._restart_task = None
            self._manual_stop = False
            self.hint = None
            self.exit_code = None
            self.state = "starting"
            self.api_port = _free_port()
            self.api_token = secrets.token_hex(16)
            env = {**os.environ, "BOT_API_PORT": str(self.api_port), "BOT_API_TOKEN": self.api_token,
                   "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "DATA_DIR": str(settings.DATA_DIR)}
            self.panel_log("Starting the bot…")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", "-m", "musicbot.bot",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    cwd=str(settings.ROOT), env=env, limit=1 << 20)
            except OSError as e:
                self.state = "crashed"
                self.hint = f"Couldn't start Python: {e}"
                self.panel_log(self.hint, "error")
                return
            self.started_at = time.time()
            self.state = "running"
            proc = self.proc
            pump = asyncio.create_task(self._pump(proc))
            self._watcher = asyncio.create_task(self._watch(proc, pump))  # keep a reference

    async def stop(self, timeout: float = 10) -> None:
        async with self._lock:
            self._manual_stop = True
            if self._restart_task and not self._restart_task.done():
                self._restart_task.cancel()
                self._restart_task = None
                self.state = "stopped"
            proc = self.proc
            if proc is None or proc.returncode is not None:
                return
            self.state = "stopping"
            self.panel_log("Stopping the bot…")
            try:  # graceful: leave voice channels, close the gateway
                await self.bot_request("POST", "/control", {"action": "shutdown"}, timeout=3)
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                self.panel_log("The bot didn't stop in time; killing it.", "warn")
                proc.kill()
                await proc.wait()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not raw:
                return
            self.emit(raw.decode("utf-8", "replace"))

    async def _watch(self, proc: asyncio.subprocess.Process, pump: asyncio.Task) -> None:
        code = await proc.wait()
        try:
            await asyncio.wait_for(pump, 5)
        except asyncio.TimeoutError:
            pump.cancel()
        if proc is not self.proc:
            return
        self.proc = None
        self.exit_code = code
        self.started_at = None
        if self._manual_stop or code == 0:
            self.state = "stopped"
            self.panel_log(f"Bot stopped (exit code {code}).")
            return
        self.state = "crashed"
        self.hint = HINTS.get(code, f"The bot exited with code {code}. See the log for the error.")
        self.panel_log(f"Bot exited with code {code}. {self.hint}", "error")
        if code in HINTS or not settings.load().get("auto_restart", True):
            return
        now = time.time()
        self._crashes.append(now)
        recent = [t for t in self._crashes if now - t < CRASH_WINDOW]
        if len(recent) >= CRASH_LIMIT:
            self.hint = f"Crashed {len(recent)} times in {CRASH_WINDOW // 60} minutes; not restarting it again."
            self.panel_log(self.hint, "error")
            return
        delay = 5 * len(recent)
        self.panel_log(f"Restarting in {delay} s…", "warn")
        self._restart_task = asyncio.create_task(self._delayed_start(delay))

    async def _delayed_start(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self._manual_stop:
            await self.start()

    # ---- talking to the running bot ----------------------------------------------------------

    async def bot_request(self, method: str, path: str, body: dict | None = None, timeout: float = 4):
        if not self.running:
            raise RuntimeError("The bot isn't running.")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, f"http://127.0.0.1:{self.api_port}{path}", json=body,
                                 headers={"X-Token": self.api_token}) as r:
                return await r.json(content_type=None)

    def status(self) -> dict:
        return {
            "state": self.state,
            "pid": self.proc.pid if self.running else None,
            "started_at": self.started_at,
            "exit_code": self.exit_code,
            "hint": self.hint,
            "restart_pending": bool(self._restart_task and not self._restart_task.done()),
        }
