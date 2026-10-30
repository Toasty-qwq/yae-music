"""Per-server settings: defaults, a cache over the database, and validation of what the dashboard
sends (every channel and role is checked against the real server before it is saved)."""
from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .db import Database

LOG_CATEGORIES = ("messages", "members", "moderation", "voice", "server")

DEFAULTS: dict = {
    "welcome": {
        "enabled": False, "channel_id": None, "embed": True, "ping": True, "color": "#f2879f",
        "message": "Welcome to **{server}**, {user}! You are member number {membercount}.",
        "dm_enabled": False, "dm_message": "Welcome to {server}, {user.name}. Glad you're here.",
    },
    "farewell": {
        "enabled": False, "channel_id": None, "embed": True, "color": "#7c687a",
        "message": "**{user.name}** left. {server} has {membercount} members now.",
    },
    "autorole": {"enabled": False, "role_ids": [], "bot_role_ids": []},
    "moderation": {"modlog_channel_id": None, "dm_users": True},
    "logging": {
        "enabled": False,
        "channels": {c: None for c in LOG_CATEGORIES},
        "ignored_channel_ids": [],
        "ignore_bots": True,
    },
}
SECTIONS = tuple(DEFAULTS)
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ConfigError(ValueError):
    """A message the dashboard shows next to the field."""


def merged(stored: dict) -> dict:
    """Defaults with the stored values laid over them (new keys appear by themselves)."""
    out = copy.deepcopy(DEFAULTS)
    for section, values in (stored or {}).items():
        if section in out and isinstance(values, dict):
            for k, v in values.items():
                if k in out[section]:
                    if isinstance(out[section][k], dict) and isinstance(v, dict):
                        out[section][k].update({kk: vv for kk, vv in v.items() if kk in out[section][k]})
                    else:
                        out[section][k] = v
    return out


class GuildConfigs:
    def __init__(self, db: "Database"):
        self.db = db
        self._cache: dict[int, dict] = {}

    async def get(self, guild_id: int) -> dict:
        if guild_id not in self._cache:
            self._cache[guild_id] = merged(await self.db.guild_config(guild_id))
        return self._cache[guild_id]

    async def section(self, guild_id: int, name: str) -> dict:
        return (await self.get(guild_id))[name]

    async def save_section(self, guild: discord.Guild, name: str, data: dict) -> dict:
        if name not in DEFAULTS:
            raise ConfigError(f"Unknown section {name!r}.")
        clean = VALIDATORS[name](guild, data)
        cfg = copy.deepcopy(await self.get(guild.id))
        cfg[name] = clean
        await self.db.save_guild_config(guild.id, cfg)
        self._cache[guild.id] = cfg
        return clean


# ---- validation --------------------------------------------------------------------------------

def _bool(data: dict, key: str, default: bool) -> bool:
    return bool(data.get(key, default))


def _text(data: dict, key: str, default: str, limit: int, label: str) -> str:
    value = str(data.get(key, default) or "").strip()
    if len(value) > limit:
        raise ConfigError(f"{label} is {len(value)} characters; Discord allows {limit}.")
    return value


def _color(data: dict, key: str, default: str) -> str:
    value = str(data.get(key) or default)
    if not HEX.match(value):
        raise ConfigError(f"{value!r} is not a colour. Use the #rrggbb form, like #f2879f.")
    return value.lower()


def _channel(guild: discord.Guild, value, label: str, *, required: bool) -> int | None:
    if value in (None, "", 0, "0"):
        if required:
            raise ConfigError(f"Pick a channel for {label}.")
        return None
    try:
        ch = guild.get_channel(int(value))
    except (TypeError, ValueError):
        ch = None
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):  # announcement channels are TextChannels too
        raise ConfigError(f"The channel picked for {label} isn't a text channel in this server any more.")
    perms = ch.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise ConfigError(f"The bot can't send messages with embeds in #{ch.name}. "
                          "Give it View Channel, Send Messages and Embed Links there.")
    return ch.id


def _roles(guild: discord.Guild, values, label: str) -> list[int]:
    out = []
    for v in values or []:
        try:
            role = guild.get_role(int(v))
        except (TypeError, ValueError):
            role = None
        if role is None:
            continue  # deleted since: drop it quietly
        problem = role_problem(guild, role)
        if problem:
            raise ConfigError(f"{label}: {problem}")
        out.append(role.id)
    return list(dict.fromkeys(out))


def role_problem(guild: discord.Guild, role: discord.Role) -> str | None:
    """Why the bot can't hand out ``role`` (None = it can)."""
    if role.is_default():
        return "@everyone can't be handed out."
    if role.managed:
        return f"@{role.name} belongs to an integration or bot, so nobody can hand it out."
    if not guild.me.guild_permissions.manage_roles:
        return "the bot needs the Manage Roles permission."
    if role >= guild.me.top_role:
        return (f"@{role.name} is above the bot's own highest role. In Server Settings → Roles, "
                f"drag the bot's role above it.")
    return None


def _welcome(guild, d):
    enabled = _bool(d, "enabled", False)
    return {
        "enabled": enabled,
        "channel_id": _channel(guild, d.get("channel_id"), "the welcome message", required=enabled),
        "embed": _bool(d, "embed", True),
        "ping": _bool(d, "ping", True),
        "color": _color(d, "color", DEFAULTS["welcome"]["color"]),
        "message": _text(d, "message", "", 2000, "The welcome message") or DEFAULTS["welcome"]["message"],
        "dm_enabled": _bool(d, "dm_enabled", False),
        "dm_message": _text(d, "dm_message", "", 2000, "The welcome DM") or DEFAULTS["welcome"]["dm_message"],
    }


def _farewell(guild, d):
    enabled = _bool(d, "enabled", False)
    return {
        "enabled": enabled,
        "channel_id": _channel(guild, d.get("channel_id"), "the farewell message", required=enabled),
        "embed": _bool(d, "embed", True),
        "color": _color(d, "color", DEFAULTS["farewell"]["color"]),
        "message": _text(d, "message", "", 2000, "The farewell message") or DEFAULTS["farewell"]["message"],
    }


def _autorole(guild, d):
    return {
        "enabled": _bool(d, "enabled", False),
        "role_ids": _roles(guild, d.get("role_ids"), "Roles for new members"),
        "bot_role_ids": _roles(guild, d.get("bot_role_ids"), "Roles for new bots"),
    }


def _moderation(guild, d):
    return {
        "modlog_channel_id": _channel(guild, d.get("modlog_channel_id"), "the mod log", required=False),
        "dm_users": _bool(d, "dm_users", True),
    }


def _logging(guild, d):
    chans = d.get("channels") or {}
    ignored = []
    for v in d.get("ignored_channel_ids") or []:
        try:
            if guild.get_channel(int(v)):
                ignored.append(int(v))
        except (TypeError, ValueError):
            pass
    return {
        "enabled": _bool(d, "enabled", False),
        "channels": {c: _channel(guild, chans.get(c), f"{c} logs", required=False) for c in LOG_CATEGORIES},
        "ignored_channel_ids": list(dict.fromkeys(ignored)),
        "ignore_bots": _bool(d, "ignore_bots", True),
    }


VALIDATORS = {"welcome": _welcome, "farewell": _farewell, "autorole": _autorole,
              "moderation": _moderation, "logging": _logging}
