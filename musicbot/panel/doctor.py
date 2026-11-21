"""The check-up: finds what's wrong, fixes what it safely can, and says what to do about the rest.

Runs as one background job; the page polls ``Doctor.snapshot()`` and shows the steps as they go.
Each step ends ok | fixed | warn | fail | skip, with a sentence, and sometimes a button.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import shutil
import sqlite3
import sys
import time
from typing import TYPE_CHECKING, Awaitable, Callable

import aiohttp

from .. import settings

if TYPE_CHECKING:
    from .supervisor import Supervisor

DISCORD = "https://discord.com/api/v10"
TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo": the first video, never going away
MEMBERS = (1 << 14) | (1 << 15)    # ApplicationFlags gateway_guild_members(_limited)
CONTENT = (1 << 18) | (1 << 19)    # ApplicationFlags gateway_message_content(_limited)
PORTAL = "https://discord.com/developers/applications"


def _installed(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


class Stop(Exception):
    """Later steps can't run (no token, no internet)."""


class Doctor:
    def __init__(self, sup: "Supervisor", update_ytdlp: Callable[[], Awaitable[tuple[bool, str]]],
                 ffmpeg: Callable[[], str], forget_ffmpeg: Callable[[], None]):
        self.sup = sup
        self.update_ytdlp = update_ytdlp
        self.ffmpeg = ffmpeg
        self.forget_ffmpeg = forget_ffmpeg
        self.steps: list[dict] = []
        self.started: float | None = None
        self.finished: float | None = None
        self.task: asyncio.Task | None = None
        self.need_restart = False
        self.bot_ok = False
        self.token = ""
        self.voice_report: dict | None = None

    @property
    def busy(self) -> bool:
        return bool(self.task and not self.task.done())

    def snapshot(self) -> dict:
        return {"busy": self.busy, "started": self.started, "finished": self.finished, "steps": self.steps}

    def run(self) -> None:
        if self.busy:
            return
        self.steps, self.started, self.finished = [], time.time(), None
        self.need_restart = self.bot_ok = False
        self.token, self.voice_report = "", None
        self.task = asyncio.create_task(self._run(), name="check-up")

    # ---- step bookkeeping ---------------------------------------------------------------------

    def _step(self, title: str) -> dict:
        step = {"title": title, "status": "running", "detail": "", "action": None}
        self.steps.append(step)
        return step

    @staticmethod
    def _end(step: dict, status: str, detail: str, action: dict | None = None) -> None:
        step.update(status=status, detail=detail, action=action)

    async def _run(self) -> None:
        self.sup.panel_log("Check-up started.")
        checks = [self._token, self._intents, self._process, self._ffmpeg, self._ytdlp, self._youtube,
                  self._spotify, self._database, self._disk, self._servers, self._voice, self._public, self._restart]
        try:
            for check in checks:
                try:
                    await check()
                except Stop:
                    for rest in checks[checks.index(check) + 1:]:
                        if rest in (self._ffmpeg, self._ytdlp, self._youtube, self._database, self._disk):
                            await rest()  # these don't need Discord
                    break
                except Exception as e:  # noqa: BLE001 — one broken check mustn't end the check-up
                    step = self.steps[-1] if self.steps and self.steps[-1]["status"] == "running" else self._step("Check")
                    self._end(step, "warn", f"This check itself failed: {e}")
        finally:
            for s in self.steps:
                if s["status"] == "running":
                    self._end(s, "skip", "Stopped before it finished.")
            self.finished = time.time()
            counts = {k: sum(s["status"] == k for s in self.steps) for k in ("fixed", "warn", "fail")}
            self.sup.panel_log(f"Check-up done: {counts['fixed']} fixed, {counts['warn']} to look at, "
                               f"{counts['fail']} problem(s).", "warn" if counts["fail"] else "info")

    async def _discord(self, path: str, token: str) -> tuple[int, dict]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(DISCORD + path, headers={"Authorization": "Bot " + token}) as r:
                return r.status, (await r.json(content_type=None) if r.status == 200 else {})

    async def _state(self) -> dict | None:
        try:
            return await self.sup.bot_request("GET", "/state", timeout=4)
        except Exception:  # noqa: BLE001
            return None

    async def _wait_ready(self, seconds: float = 60) -> dict | None:
        end = time.time() + seconds
        while time.time() < end:
            await asyncio.sleep(2)
            if not self.sup.running:
                return None
            st = await self._state()
            if st and st.get("ready"):
                return st
        return None

    def _last_error(self) -> str:
        for line in reversed(self.sup.lines):
            if line["level"] == "error" and " panel: " not in line["text"]:
                return line["text"][-220:]
        return ""

    # ---- the checks -----------------------------------------------------------------------------

    async def _token(self) -> None:
        step = self._step("Bot token and internet")
        token = settings.load()["discord_token"]
        if not token:
            self._end(step, "fail", "There's no Discord bot token yet. Add it in Settings.", {"label": "Open Settings", "href": "#settings"})
            raise Stop
        try:
            code, me = await self._discord("/users/@me", token)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._end(step, "fail", "Can't reach Discord. Check this machine's internet connection, then run the check-up again.")
            raise Stop from None
        if code == 401:
            self._end(step, "fail", "Discord rejects the bot token. In the Developer Portal open your app → Bot → Reset Token, "
                                    "and paste the new one in Settings.", {"label": "Open Settings", "href": "#settings"})
            raise Stop
        if code != 200:
            self._end(step, "warn", f"Discord answered {code}. It may be having trouble; try again in a few minutes.")
            raise Stop
        self.token = token
        self._end(step, "ok", f"Discord accepts the token for {me.get('username', 'the bot')}.")

    async def _intents(self) -> None:
        step = self._step("Privileged intents")
        code, app = await self._discord("/applications/@me", self.token)
        if code != 200:
            self._end(step, "skip", "Couldn't read the app's settings from Discord.")
            return
        flags = app.get("flags") or 0
        off = [name for name, bits in (("Server Members", MEMBERS), ("Message Content", CONTENT)) if not flags & bits]
        if off:
            self._end(step, "warn", f"{' and '.join(off)} {'is' if len(off) == 1 else 'are'} off, so welcome messages, "
                                    "autorole and the logs can't work. Developer Portal → your app → Bot → Privileged "
                                    "Gateway Intents: turn them on and save. The check-up restarts the bot afterwards.",
                      {"label": "Developer Portal", "href": PORTAL})
            return
        st = await self._state() if self.sup.running else None
        if st and not (st.get("intents") or {}).get("members"):
            self.need_restart = True  # switched on after the bot started: it only picks them up on a restart
            self._end(step, "fixed", "They're on now, but the bot started before that. It gets restarted at the end.")
            return
        self._end(step, "ok", "Server Members and Message Content are on.")

    async def _process(self) -> None:
        step = self._step("The bot is running")
        sup = self.sup
        if not sup.running:
            why = sup.hint or "It was stopped."
            sup._crashes.clear()  # a fresh start, even after it gave up on crash-restarts
            await sup.start()
            st = await self._wait_ready()
            if st:
                self.bot_ok = True
                self._end(step, "fixed", f"It wasn't running ({why.rstrip('.')}). Started it, and it's online.")
            else:
                err = self._last_error()
                self._end(step, "fail", "Started it, but it didn't come online. " + (f"Last error: {err}" if err else
                                        "The Logs page shows why."), {"label": "Open Logs", "href": "#logs"})
            return
        st = await self._state()
        up = time.time() - (sup.started_at or time.time())
        if (not st or not st.get("ready")) and up > 90:
            await sup.restart()
            st = await self._wait_ready()
            if st:
                self.bot_ok = True
                self._end(step, "fixed", "It was running but not answering or not connected to Discord. Restarted it, and it's online.")
            else:
                self._end(step, "fail", "It doesn't come online even after a restart. The Logs page shows why.", {"label": "Open Logs", "href": "#logs"})
            return
        if not st or not st.get("ready"):
            st = await self._wait_ready()
            if not st:
                self._end(step, "warn", "It's still connecting to Discord. Run the check-up again in a minute.")
                return
        self.bot_ok = True
        ping = st.get("latency_ms")
        if ping is None:
            self._end(step, "ok", "Online.")
        elif ping < 250:
            self._end(step, "ok", f"Online. Ping {ping} ms: normal (under about 250 ms is fine, and it doesn't affect music).")
        else:
            self._end(step, "warn", f"Online, but the ping is {ping} ms. That's this machine's internet or Discord itself, "
                                    "not the bot; a restart won't lower it.")

    async def _ffmpeg(self) -> None:
        step = self._step("ffmpeg (plays the audio)")
        self.forget_ffmpeg()
        v = await asyncio.to_thread(self.ffmpeg)
        if v in ("not found", "error"):
            self._end(step, "fail", "ffmpeg isn't found, so nothing can play. Install it (on Windows: winget install ffmpeg) "
                                    "or set its path in Settings, then restart the bot.", {"label": "Open Settings", "href": "#settings"})
        else:
            self._end(step, "ok", f"Found ({v.split(' ')[0]}).")

    async def _ytdlp(self) -> None:
        step = self._step("yt-dlp is up to date")
        before = _installed("yt-dlp")
        ok, after = await self.update_ytdlp()
        if not ok:
            self._end(step, "warn", "The update failed; the Logs page has pip's message. The bot keeps using " + (before or "the old one") + ".")
        elif after != before:
            self.need_restart = True
            self._end(step, "fixed", f"Updated {before} → {after}. The bot gets restarted at the end to use it.")
        else:
            self._end(step, "ok", f"{after} is the newest.")

    async def _youtube(self) -> None:
        step = self._step("YouTube playback")
        cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", "--socket-timeout", "15",
               "-f", "bestaudio[acodec=opus]/bestaudio/best", "--get-url", TEST_VIDEO]
        cookies = settings.COOKIES_FILE.is_file()
        if cookies:
            cmd[3:3] = ["--cookies", str(settings.COOKIES_FILE)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 120)
        except asyncio.TimeoutError:
            proc.kill()
            self._end(step, "warn", "YouTube took over two minutes to answer. It may be slow right now; try again later.")
            return
        if proc.returncode == 0 and out.decode().strip().startswith("http"):
            self._end(step, "ok", "A test video loads fine" + (" (with your cookies)." if cookies else "."))
            return
        text = err.decode("utf-8", "replace")
        last = next((ln for ln in reversed(text.splitlines()) if ln.strip()), "no message")[-220:]
        if "not a bot" in text or "Sign in to confirm" in text:
            self._end(step, "warn", ("YouTube asks the bot to sign in again: the cookies have run out. Export a fresh cookies.txt "
                                     "and upload it below.") if cookies else
                      "YouTube wants proof the bot isn't a robot. Upload a cookies.txt from a browser signed in to YouTube (below).",
                      {"label": "Upload cookies", "run": "cookies"})
        else:
            self._end(step, "fail", f"The test video doesn't load. yt-dlp says: {last}")

    async def _spotify(self) -> None:
        step = self._step("Spotify keys")
        cfg = settings.load()
        cid, secret = cfg["spotify_client_id"], cfg["spotify_client_secret"]
        if not cid or not secret:
            self._end(step, "skip", "No keys set. Spotify links still work through the public page (up to 100 songs per playlist).")
            return
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                                  auth=aiohttp.BasicAuth(cid, secret)) as r:
                    code = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._end(step, "warn", "Couldn't reach Spotify right now.")
            return
        if code == 200:
            self._end(step, "ok", "Spotify accepts the Client ID and secret.")
        elif code in (400, 401):
            self._end(step, "fail", "Spotify rejects the Client ID or secret. Copy them again from the Spotify developer "
                                    "dashboard into Settings.", {"label": "Open Settings", "href": "#settings"})
        else:
            self._end(step, "warn", f"Spotify answered {code}; it may be having trouble.")

    async def _database(self) -> None:
        step = self._step("Server settings database")
        path = settings.DATA_DIR / "bot.db"
        if not path.is_file():
            self._end(step, "skip", "Not made yet; the bot makes it on its first start.")
            return

        def check() -> str:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
            try:
                return con.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                con.close()
        result = await asyncio.to_thread(check)
        if result == "ok":
            self._end(step, "ok", f"Healthy ({path.stat().st_size // 1024} KB).")
        else:
            self._end(step, "fail", f"The database is damaged ({result[:160]}). Stop the bot and put back a backup of "
                                    f"{path.name} from the data folder.")

    async def _disk(self) -> None:
        step = self._step("Disk space")
        free = shutil.disk_usage(settings.DATA_DIR).free
        if free > 1 << 30:
            self._end(step, "ok", f"{free / (1 << 30):.1f} GB free.")
            return
        freed = 0
        for old in settings.LOG_DIR.glob("musicbot.log.*"):  # rotated logs; the live one stays
            freed += old.stat().st_size
            old.unlink(missing_ok=True)
        free = shutil.disk_usage(settings.DATA_DIR).free
        status = "fixed" if freed else "warn"
        self._end(step, status if free > 200 << 20 else "fail",
                  (f"Deleted old logs ({freed // (1 << 20)} MB). " if freed else "") +
                  f"Only {free / (1 << 20):.0f} MB free on the drive with the bot's data. Free up some space.")

    async def _servers(self) -> None:
        step = self._step("Permissions in your servers")
        if not self.bot_ok:
            self._end(step, "skip", "Needs the bot online.")
            return
        rep = await self.sup.bot_request("GET", "/doctor", timeout=30)
        self.voice_report = rep.get("voice") or {}
        problems = rep.get("guilds") or []
        if not problems:
            self._end(step, "ok", "The bot has every permission it needs, and can hand out every role it's set up to give.")
            return
        lines = []
        for g in problems[:8]:
            lines.append(f"{g['name']}:")
            if g["missing"]:
                lines.append("  • missing " + ", ".join(f"{m['perm'].replace('_', ' ').title()} (for {m['for']})"
                                                     for m in g["missing"]))
            lines += [f"  • {r}" for r in g["roles"]]
        tips = []
        if any(g["missing"] for g in problems):
            tips.append("Missing permissions: open the invite link and pick the server again (it only updates the "
                        "permissions), or give the bot's role those permissions.")
        if any(g["roles"] for g in problems):
            tips.append("Roles: Server Settings → Roles, and drag the bot's role above the ones it hands out.")
        st = await self._state() or {}
        action = None
        if st.get("id") and any(g["missing"] for g in problems):
            from ..bot.client import invite_url
            action = {"label": "Invite link", "href": invite_url(int(st["id"]))}
        self._end(step, "warn", "\n".join(lines + [""] + tips), action)

    async def _voice(self) -> None:
        step = self._step("Voice connections")
        if not self.bot_ok:
            self._end(step, "skip", "Needs the bot online.")
            return
        v = self.voice_report or (await self.sup.bot_request("GET", "/doctor", timeout=30)).get("voice", {})
        if not v.get("orphans") and not v.get("dead"):
            self._end(step, "ok", "Nothing stuck.")
            return
        fixed = (await self.sup.bot_request("POST", "/doctor/fix-voice", timeout=30)).get("fixed", 0)
        self._end(step, "fixed", f"Cleared {fixed} stuck voice connection(s). /play works there again.")

    async def _public(self) -> None:
        url = settings.load().get("public_url")
        if not url:
            return
        step = self._step("Panel reachable from the internet")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(url.rstrip("/") + "/healthz") as r:
                    ok = r.status == 200 and (await r.text()).strip() == "ok"
                    code = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError):
            ok, code = False, None
        if ok:
            self._end(step, "ok", f"{url} answers.")
        else:
            self._end(step, "warn", f"{url} doesn't answer{f' ({code})' if code else ''}, so Discord sign-in and the dashboard "
                                    "don't work from outside. Check the Cloudflare tunnel (cloudflared) is running and points at this panel.")

    async def _restart(self) -> None:
        if not self.need_restart:
            return
        step = self._step("Restart to use the fixes")
        if not self.sup.running:
            self._end(step, "skip", "The bot isn't running; it uses them when it starts.")
            return
        st = await self._state() or {}
        if any(p.get("current") and p.get("listeners") for p in st.get("players") or []):
            self._end(step, "warn", "Someone is listening to music, so it wasn't restarted. Restart it when they're done.",
                      {"label": "Restart now", "run": "restart"})
            return
        await self.sup.restart()
        ok = await self._wait_ready()
        self._end(step, "fixed" if ok else "fail", "Restarted, and it's online." if ok else
                  "Restarted, but it didn't come back online. The Logs page shows why.")
