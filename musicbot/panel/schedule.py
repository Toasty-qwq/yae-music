"""Scheduled restarts: every day, on some weekdays, once a month, or once on a date.

The schedule lives in settings.json under ``restart_schedule`` with the owner's time zone (the
browser sends it), so "Monday 04:00" means their Monday 04:00 even when the container runs in UTC.
A restart first updates yt-dlp (optional) and can wait until nobody is listening to music.
"""
from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import re
import time
from typing import TYPE_CHECKING, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import settings

if TYPE_CHECKING:
    from .supervisor import Supervisor

REPEATS = ("daily", "weekly", "monthly", "once")
DEFAULT = {"enabled": False, "repeat": "weekly", "time": "04:00", "days": [0], "day": 1, "date": "",
           "tz": "UTC", "update_ytdlp": True, "wait_for_music": True}
MAX_WAIT = 2 * 3600   # s a restart waits for the music to stop before going ahead anyway
TICK = 20             # s between checks


def zone(name: str) -> dt.tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def load() -> dict:
    return {**DEFAULT, **(settings.raw().get("restart_schedule") or {})}


def validate(body: dict) -> dict:
    """The schedule as it will be stored; ValueError with a sentence when something's off."""
    s = {**load(), **{k: v for k, v in body.items() if k in DEFAULT}}
    s["enabled"] = bool(s["enabled"])
    s["update_ytdlp"] = bool(s["update_ytdlp"])
    s["wait_for_music"] = bool(s["wait_for_music"])
    if s["repeat"] not in REPEATS:
        raise ValueError("Pick how often it restarts.")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s["time"]).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError("The time must look like 04:00.")
    s["time"] = f"{int(m[1]):02d}:{m[2]}"
    try:
        s["days"] = sorted({int(d) for d in s["days"] if 0 <= int(d) <= 6})
        s["day"] = int(s["day"])
    except (TypeError, ValueError):
        raise ValueError("Those days don't look right.") from None
    if s["repeat"] == "weekly" and not s["days"]:
        raise ValueError("Pick at least one day of the week.")
    if not 0 <= s["day"] <= 31:
        raise ValueError("The day of the month is 1 to 31, or the last day.")
    if s["repeat"] == "once":
        try:
            dt.date.fromisoformat(str(s["date"]))
        except ValueError:
            raise ValueError("Pick the date for the restart.") from None
    s["tz"] = str(s["tz"] or "UTC")
    if zone(s["tz"]) is dt.timezone.utc and s["tz"] not in ("UTC", "Etc/UTC"):
        s["tz"] = "UTC"  # unknown zone name: say so in UTC rather than guess
    return s


def _matches(s: dict, d: dt.date) -> bool:
    if s["repeat"] == "daily":
        return True
    if s["repeat"] == "weekly":
        return d.weekday() in s["days"]
    if s["repeat"] == "monthly":
        last = calendar.monthrange(d.year, d.month)[1]
        return d.day == (last if s["day"] == 0 else min(s["day"], last))  # "the 31st" is the 30th in April
    return d.isoformat() == s["date"]


def next_run(s: dict, after: dt.datetime) -> dt.datetime | None:
    """The first scheduled moment strictly after ``after`` (aware), or None."""
    tz = zone(s["tz"])
    hh, mm = (int(x) for x in s["time"].split(":"))
    day = after.astimezone(tz).date()
    for _ in range(400):
        if _matches(s, day):
            at = dt.datetime.combine(day, dt.time(hh, mm), tzinfo=tz)
            if at > after:
                return at
        day += dt.timedelta(days=1)
    return None


class Scheduler:
    """Checks the schedule every few seconds and restarts the bot when it's due."""

    def __init__(self, sup: "Supervisor", update_ytdlp: Callable[[], Awaitable[tuple[bool, str]]]):
        self.sup = sup
        self.update_ytdlp = update_ytdlp
        self.booted = time.time()
        self.waiting_since: float | None = None
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="restart-schedule")

    def since(self) -> float:
        """Nothing before this moment counts as missed: panel start, last run, last save."""
        raw = settings.raw()
        return max(self.booted, raw.get("restart_last") or 0, raw.get("restart_schedule_saved") or 0)

    def next_at(self) -> dt.datetime | None:
        s = load()
        if not s["enabled"]:
            return None
        return next_run(s, dt.datetime.fromtimestamp(self.since(), dt.timezone.utc))

    def status(self) -> dict:
        s = load()
        nxt = self.next_at()
        return {**s, "next": nxt.timestamp() if nxt else None, "last": settings.raw().get("restart_last"),
                "waiting_since": self.waiting_since}

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            try:
                nxt = self.next_at()
                if nxt and time.time() >= nxt.timestamp():
                    await self._due()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — never let the schedule die
                self.sup.panel_log(f"Scheduled restart failed: {e}", "error")
                settings.update({"restart_last": time.time()})
                self.waiting_since = None

    async def _listening(self) -> bool:
        if not self.sup.running:
            return False
        try:
            state = await self.sup.bot_request("GET", "/state", timeout=3)
        except Exception:  # noqa: BLE001
            return False
        return any(p.get("current") and p.get("listeners") for p in state.get("players") or [])

    async def _due(self) -> None:
        s = load()
        if s["wait_for_music"] and await self._listening():
            if self.waiting_since is None:
                self.waiting_since = time.time()
                self.sup.panel_log("Scheduled restart: music is playing, so it waits until it stops (2 hours at most).")
            if time.time() - self.waiting_since < MAX_WAIT:
                return
            self.sup.panel_log("Scheduled restart: still playing after 2 hours, restarting anyway.", "warn")
        self.waiting_since = None
        settings.update({"restart_last": time.time()})  # first, so a failure below can't loop
        if s["repeat"] == "once":
            settings.update({"restart_schedule": {**s, "enabled": False}})
        if self.sup.state == "stopped":
            self.sup.panel_log("Scheduled restart skipped: the bot was stopped by hand.")
            return
        self.sup.panel_log("Scheduled restart starting.")
        if s["update_ytdlp"]:
            await self.update_ytdlp()
        await self.sup.restart()
