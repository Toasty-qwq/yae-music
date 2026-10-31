from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .. import settings
from . import api
from .commands import Music
from .community import Community
from .db import Database
from .eventlog import EventLog
from .forumroles import ForumRoles
from .guildconfig import GuildConfigs
from .moderation import Moderation
from .profile import Profile
from .player import GuildPlayer
from .roles import ReactionRoles
from .spotify import Spotify
from .tags import Tags
from .tickets import Tickets
from .utility import Utility
from .voicemaster import VoiceMaster
from .welcome import Welcome

log = logging.getLogger("musicbot")

INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True,
    add_reactions=True, connect=True, speak=True,                    # music
    manage_roles=True,                                               # autorole, reaction roles
    kick_members=True, ban_members=True, moderate_members=True,     # moderation
    manage_messages=True, view_audit_log=True,                       # purge, logs
    manage_channels=True, move_members=True, set_voice_channel_status=True,  # VC maker
    manage_threads=True, create_private_threads=True, send_messages_in_threads=True,  # tickets
    use_external_emojis=True,
).value


def invite_url(app_id: int) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={app_id}"
            f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")


class MusicBot(commands.Bot):
    def __init__(self, cfg: dict, *, privileged: bool = True):
        intents = discord.Intents.default()
        # Server Members: welcome/farewell, autorole, member logs, dashboard permission checks.
        # Message Content: the text of edited and deleted messages in the logs.
        intents.members = privileged
        intents.message_content = privileged
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         max_messages=5000,  # cached messages are what edit/delete logs can quote
                         activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))
        self.cfg = cfg
        self.players: dict[int, GuildPlayer] = {}
        self.spotify = Spotify(cfg["spotify_client_id"], cfg["spotify_client_secret"])
        self.api: api.ControlApi | None = None
        self.db: Database | None = None
        self.configs: GuildConfigs | None = None

    async def setup_hook(self) -> None:
        self.db = await Database.open()
        self.configs = GuildConfigs(self.db)
        for cog in (Music, Welcome, ReactionRoles, Moderation, EventLog, Tags, Community, Utility, ForumRoles,
                    VoiceMaster, Tickets, Profile):
            await self.add_cog(cog(self))
        self.api = await api.start(self)
        synced = await self.tree.sync()
        log.info("Slash commands registered: %s", ", ".join("/" + c.name for c in synced))

    async def on_ready(self) -> None:
        assert self.user
        log.info("Logged in as %s (id %s) in %d server(s)", self.user, self.user.id, len(self.guilds))
        log.info("Invite link: %s", invite_url(self.user.id))
        if not self.intents.members:
            log.warning("Running without the Server Members intent: welcome messages, autorole and member logs are off.")
        if settings.COOKIES_FILE.is_file():
            log.info("Using YouTube cookies from %s", settings.COOKIES_FILE)

    async def close(self) -> None:
        for p in list(self.players.values()):
            await p.stop()
        if self.api:
            await self.api.close()
        if self.db:
            await self.db.close()
        await super().close()
