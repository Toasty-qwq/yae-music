"""Per-server settings: defaults, a cache over the database, and validation of what the dashboard
sends (every channel and role is checked against the real server before it is saved)."""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import discord

from . import messages
from .errors import HEX, ConfigError  # noqa: F401  (re-exported)

if TYPE_CHECKING:
    from .db import Database

LOG_CATEGORIES = ("messages", "members", "moderation", "voice", "server")

WELCOME_DEFAULT = messages.message(
    "{user}", color="#f2879f", title="Welcome to {server}!",
    description="Glad you're here, {user.display}. You're member number {membercount}.",
    thumbnail="{user.avatar}", footer="{server}", footer_icon="{server.icon}", timestamp=True)
FAREWELL_DEFAULT = messages.message(
    "", color="#7c687a", description="**{user.name}** left. {server} has {membercount} members now.",
    thumbnail="{user.avatar}")
DM_DEFAULT = messages.message("Welcome to {server}, {user.name}. Glad you're here.")
MATCHES = ("contains", "word", "exact", "starts")

DEFAULTS: dict = {
    "welcome": {"enabled": False, "channel_id": None, "message": WELCOME_DEFAULT,
                "dm_enabled": False, "dm": DM_DEFAULT},
    "farewell": {"enabled": False, "channel_id": None, "message": FAREWELL_DEFAULT},
    "autorole": {"enabled": False, "role_ids": [], "bot_role_ids": [], "restore_roles": False},
    "moderation": {"modlog_channel_id": None, "dm_users": True},
    "logging": {
        "enabled": False,
        "channels": {c: None for c in LOG_CATEGORIES},
        "ignored_channel_ids": [],
        "ignore_bots": True,
    },
    "autoresponders": {"enabled": False, "items": []},
    "starboard": {"enabled": False, "channel_id": None, "emoji": "\u2b50", "threshold": 3,
                  "self_star": False, "ignored_channel_ids": []},
    "suggestions": {"enabled": False, "channel_id": None, "anonymous": False, "dm_on_decision": True},
}
SECTIONS = tuple(DEFAULTS)
MESSAGE_KEYS = {"message", "dm"}  # whole message dicts: replaced, never merged key by key


def _legacy(section: str, v: dict) -> dict:
    """Welcome/farewell saved before the embed builder kept flat text settings."""
    if section not in ("welcome", "farewell") or not isinstance(v.get("message"), str):
        return v
    v = dict(v)
    text = v.pop("message")
    if v.pop("embed", True):
        mention = "{user}" if v.pop("ping", section == "welcome") else ""
        v["message"] = messages.message(mention, color=v.pop("color", "#f2879f"), description=text,
                                        thumbnail="{user.avatar}")
    else:
        v["message"] = messages.message(text)
    if "dm_message" in v:
        v["dm"] = messages.message(v.pop("dm_message"))
    return v


def merged(stored: dict) -> dict:
    """Defaults with the stored values laid over them (new keys appear by themselves)."""
    out = copy.deepcopy(DEFAULTS)
    for section, values in (stored or {}).items():
        if section in out and isinstance(values, dict):
            values = _legacy(section, values)
            for k, v in values.items():
                if k not in out[section]:
                    continue
                if k in MESSAGE_KEYS:
                    out[section][k] = messages.upgrade(v)
                elif isinstance(out[section][k], dict) and isinstance(v, dict):
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
    dm_enabled = _bool(d, "dm_enabled", False)
    return {
        "enabled": enabled,
        "channel_id": _channel(guild, d.get("channel_id"), "the welcome message", required=enabled),
        "message": messages.validate(d.get("message"), "The welcome message"),
        "dm_enabled": dm_enabled,
        "dm": messages.validate(d.get("dm"), "The welcome DM") if dm_enabled or d.get("dm") else DM_DEFAULT,
    }


def _farewell(guild, d):
    enabled = _bool(d, "enabled", False)
    return {
        "enabled": enabled,
        "channel_id": _channel(guild, d.get("channel_id"), "the farewell message", required=enabled),
        "message": messages.validate(d.get("message"), "The farewell message"),
    }


def _autorole(guild, d):
    return {
        "enabled": _bool(d, "enabled", False),
        "role_ids": _roles(guild, d.get("role_ids"), "Roles for new members"),
        "bot_role_ids": _roles(guild, d.get("bot_role_ids"), "Roles for new bots"),
        "restore_roles": _bool(d, "restore_roles", False),
    }


def _channels(guild, values) -> list[int]:
    out = []
    for v in values or []:
        try:
            if guild.get_channel(int(v)):
                out.append(int(v))
        except (TypeError, ValueError):
            pass
    return list(dict.fromkeys(out))


def _autoresponders(guild, d):
    items = []
    for i, it in enumerate(d.get("items") or [], start=1):
        trigger = str(it.get("trigger") or "").strip()
        if not trigger:
            raise ConfigError(f"Auto-reply {i} needs a trigger: the words that set it off.")
        if len(trigger) > 200:
            raise ConfigError(f"Auto-reply {i}: keep the trigger under 200 characters.")
        match = it.get("match") if it.get("match") in MATCHES else "contains"
        items.append({"trigger": trigger, "match": match, "reply": bool(it.get("reply", True)),
                      "message": messages.validate(it.get("message"), f"Auto-reply {i} ({trigger})")})
    if len(items) > 50:
        raise ConfigError("A server can have up to 50 auto-replies.")
    return {"enabled": _bool(d, "enabled", False), "items": items}


def _starboard(guild, d):
    enabled = _bool(d, "enabled", False)
    emoji = str(d.get("emoji") or "\u2b50").strip()[:64]
    try:
        threshold = int(d.get("threshold") or 3)
    except (TypeError, ValueError):
        threshold = 3
    if not 1 <= threshold <= 100:
        raise ConfigError("The number of stars needed must be between 1 and 100.")
    return {
        "enabled": enabled,
        "channel_id": _channel(guild, d.get("channel_id"), "the starboard", required=enabled),
        "emoji": emoji, "threshold": threshold,
        "self_star": _bool(d, "self_star", False),
        "ignored_channel_ids": _channels(guild, d.get("ignored_channel_ids")),
    }


def _suggestions(guild, d):
    enabled = _bool(d, "enabled", False)
    return {
        "enabled": enabled,
        "channel_id": _channel(guild, d.get("channel_id"), "suggestions", required=enabled),
        "anonymous": _bool(d, "anonymous", False),
        "dm_on_decision": _bool(d, "dm_on_decision", True),
    }


def _moderation(guild, d):
    return {
        "modlog_channel_id": _channel(guild, d.get("modlog_channel_id"), "the mod log", required=False),
        "dm_users": _bool(d, "dm_users", True),
    }


def _logging(guild, d):
    chans = d.get("channels") or {}
    ignored = _channels(guild, d.get("ignored_channel_ids"))
    return {
        "enabled": _bool(d, "enabled", False),
        "channels": {c: _channel(guild, chans.get(c), f"{c} logs", required=False) for c in LOG_CATEGORIES},
        "ignored_channel_ids": ignored,
        "ignore_bots": _bool(d, "ignore_bots", True),
    }


VALIDATORS = {"welcome": _welcome, "farewell": _farewell, "autorole": _autorole,
              "moderation": _moderation, "logging": _logging, "autoresponders": _autoresponders,
              "starboard": _starboard, "suggestions": _suggestions}
