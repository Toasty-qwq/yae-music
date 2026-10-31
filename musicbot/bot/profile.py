"""The bot's own profile: avatar, banner, username, About me, and its status/activity.

Avatar, banner and username are the bot user's (Discord allows 2 username changes an hour);
About me is the application's description. The presence isn't stored by Discord, so it's kept in
the bot's database and put back every time the bot connects.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from .errors import ConfigError

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.profile")
STATUSES = {"online": discord.Status.online, "idle": discord.Status.idle, "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible}
ACTIVITIES = {"playing": discord.ActivityType.playing, "listening": discord.ActivityType.listening,
              "watching": discord.ActivityType.watching, "competing": discord.ActivityType.competing}
DEFAULT_PRESENCE = {"status": "online", "activity": "listening", "text": "/play", "url": ""}


def _image(data: str | None) -> bytes | None:
    if not data:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ConfigError("That picture didn't arrive in one piece. Try again.") from None
    if len(raw) > 8 * 1024 * 1024:
        raise ConfigError("Pictures can be up to 8 MB.")
    return raw


class Profile(commands.Cog):
    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    async def presence(self) -> dict:
        row = await self.bot.db.one("SELECT value FROM bot_state WHERE key='presence'")
        return {**DEFAULT_PRESENCE, **(json.loads(row["value"]) if row else {})}

    async def apply_presence(self, p: dict) -> None:
        kind, text = p.get("activity") or "none", (p.get("text") or "").strip()[:128]
        activity = None
        if kind == "custom" and text:
            activity = discord.CustomActivity(name=text)
        elif kind == "streaming" and text and p.get("url"):
            activity = discord.Streaming(name=text, url=p["url"])
        elif kind in ACTIVITIES and text:
            activity = discord.Activity(type=ACTIVITIES[kind], name=text)
        await self.bot.change_presence(status=STATUSES.get(p.get("status"), discord.Status.online), activity=activity)

    @commands.Cog.listener()
    async def on_ready(self):
        try:
            await self.apply_presence(await self.presence())
        except Exception:  # noqa: BLE001
            log.exception("putting the saved status back failed")

    async def read(self) -> dict:
        u = self.bot.user
        app = await self.bot.application_info()
        return {
            "username": u.name, "id": str(u.id),
            "avatar": u.display_avatar.with_size(256).url,
            "banner": (b.with_size(600).url if (b := (await self.bot.fetch_user(u.id)).banner) else None),  # needs a full fetch
            "about": app.description or "",
            "presence": await self.presence(),
        }

    async def update(self, body: dict) -> list[str]:
        """Apply what changed; returns what was done, in words."""
        done = []
        edit = {}
        if body.get("username") and body["username"].strip() != self.bot.user.name:
            name = body["username"].strip()
            if not 2 <= len(name) <= 32:
                raise ConfigError("A username is 2 to 32 characters.")
            edit["username"] = name
        if body.get("avatar"):
            edit["avatar"] = _image(body["avatar"])
        elif body.get("remove_avatar"):
            edit["avatar"] = None
        if body.get("banner"):
            edit["banner"] = _image(body["banner"])
        elif body.get("remove_banner"):
            edit["banner"] = None
        if edit:
            try:
                await self.bot.user.edit(**edit)
            except discord.HTTPException as e:
                text = (e.text or str(e))
                if "username" in text.lower() and ("fast" in text.lower() or e.status == 400):
                    text = "Discord only allows two username changes an hour. " + text
                raise ConfigError(f"Discord refused: {text}") from e
            done += [{"username": "username", "avatar": "avatar", "banner": "banner"}[k] for k in edit]
        if "about" in body:
            about = str(body["about"] or "")[:400]
            app = await self.bot.application_info()
            if about != (app.description or ""):
                try:
                    await app.edit(description=about)
                except discord.HTTPException as e:
                    raise ConfigError(f"Discord refused the About me: {e.text or e}") from e
                done.append("About me")
        if "presence" in body:
            p = body["presence"] or {}
            p = {"status": p.get("status") if p.get("status") in STATUSES else "online",
                 "activity": p.get("activity") if p.get("activity") in (*ACTIVITIES, "custom", "streaming", "none") else "none",
                 "text": str(p.get("text") or "")[:128], "url": str(p.get("url") or "")[:300]}
            if p["activity"] == "streaming" and not p["url"].startswith(("https://twitch.tv/", "https://www.twitch.tv/",
                                                                          "https://youtube.com/", "https://www.youtube.com/")):
                raise ConfigError("Streaming needs a Twitch or YouTube link.")
            await self.bot.db.run("INSERT OR REPLACE INTO bot_state(key, value) VALUES('presence', ?)", (json.dumps(p),))
            await self.apply_presence(p)
            done.append("status")
        log.info("bot profile changed: %s", ", ".join(done) or "nothing")
        return done
