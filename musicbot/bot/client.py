from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .. import settings
from . import api
from .commands import Music
from .player import GuildPlayer
from .spotify import Spotify

log = logging.getLogger("musicbot")

# View Channel, Send Messages, Embed Links, Read Message History, Connect, Speak
INVITE_PERMISSIONS = 1024 | 2048 | 16384 | 65536 | 1048576 | 2097152


def invite_url(app_id: int) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")


class MusicBot(commands.Bot):
    def __init__(self, cfg: dict):
        intents = discord.Intents.default()  # guilds + voice states; no privileged intents needed
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         help_command=None, activity=discord.Activity(type=discord.ActivityType.listening,
                                                                      name="/play"))
        self.cfg = cfg
        self.players: dict[int, GuildPlayer] = {}
        self.spotify = Spotify(cfg["spotify_client_id"], cfg["spotify_client_secret"])
        self.api: api.ControlApi | None = None

    async def setup_hook(self) -> None:
        await self.add_cog(Music(self))
        self.api = await api.start(self)
        synced = await self.tree.sync()
        log.info("Slash commands registered: %s", ", ".join("/" + c.name for c in synced))

    async def on_ready(self) -> None:
        assert self.user
        log.info("Logged in as %s (id %s) in %d server(s)", self.user, self.user.id, len(self.guilds))
        log.info("Invite link: %s", invite_url(self.user.id))
        if settings.COOKIES_FILE.is_file():
            log.info("Using YouTube cookies from %s", settings.COOKIES_FILE)

    async def close(self) -> None:
        for p in list(self.players.values()):
            await p.stop()
        if self.api:
            await self.api.close()
        await super().close()
