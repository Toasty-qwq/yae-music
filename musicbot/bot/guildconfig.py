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
VM_FEATURES = ("name", "limit", "status", "lock", "claim", "reject", "permit", "ghost", "lfm", "text",
               "bitrate", "invite", "transfer", "nsfw", "region")
VM_MODES = ("default", "sequence", "predefined", "clone", "dynamic")
VM_TEXT_MESSAGE = messages.message("This is {user}'s channel. It goes away when the voice channel does.")
FORUM_MESSAGE = messages.message("Congratulations {user}, your post was accepted!")
FORUM_DM = messages.message("Your post in {server} was accepted, and you've been given a new role.")

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
    "forumroles": {"enabled": False, "log_channel_id": None, "rules": []},
    "voicemaster": {
        "enabled": False, "setups": [],
        "features": {f: True for f in VM_FEATURES},
        "owner_manage_channel": False, "owner_move_members": False,
        "interface": True, "interface_ping": False,
        "log_channel_id": None, "lfm_channel_id": None,
        "temp_role_id": None, "auto_text": False, "text_access_role_id": None, "text_message": VM_TEXT_MESSAGE,
    },
    "tickets": {
        "log_channel_id": None, "transcript_channel_id": None,
        "two_step_close": True, "keep_closed": True, "save_transcript": True, "dm_transcript": False,
        "claiming": True, "auto_close_hours": 0, "close_when_owner_leaves": True, "global_limit": 0,
    },
}
SECTIONS = tuple(DEFAULTS)
MESSAGE_KEYS = {"message", "dm", "text_message"}  # whole message dicts: replaced, never merged key by key


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


def _int(d, key, default, lo, hi, label):
    try:
        v = int(d.get(key, default) if d.get(key, default) not in (None, "") else default)
    except (TypeError, ValueError):
        raise ConfigError(f"{label} must be a whole number.") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{label} must be between {lo} and {hi}.")
    return v


def _category(guild, value, label, *, required=False):
    if value in (None, "", 0, "0"):
        if required:
            raise ConfigError(f"Pick a category for {label}.")
        return None
    try:
        cat = guild.get_channel(int(value))
    except (TypeError, ValueError):
        cat = None
    if not isinstance(cat, discord.CategoryChannel):
        raise ConfigError(f"The category picked for {label} doesn't exist any more.")
    p = cat.permissions_for(guild.me)
    if not (p.manage_channels and p.view_channel):
        raise ConfigError(f"The bot needs Manage Channels in the {cat.name} category for {label}.")
    return cat.id


def _optional_role(guild, value, label):
    if value in (None, "", 0, "0"):
        return None
    roles = _roles(guild, [value], label)
    return roles[0] if roles else None


def _voicemaster(guild, d):
    setups, seen = [], set()
    for i, st in enumerate(d.get("setups") or [], start=1):
        mode = st.get("mode") if st.get("mode") in VM_MODES else "default"
        sid = str(st.get("id") or "")[:12] or f"s{i}"
        gen = None
        if mode != "dynamic":
            try:
                gen = guild.get_channel(int(st.get("channel_id") or 0))
            except (TypeError, ValueError):
                gen = None
            if not isinstance(gen, discord.VoiceChannel):
                raise ConfigError(f"Generator {i}: pick the voice channel people join to get their own.")
            if gen.id in seen:
                raise ConfigError(f"Generator {i}: that voice channel is already a generator.")
            seen.add(gen.id)
        category = _category(guild, st.get("category_id"), f"generator {i}", required=mode == "dynamic") \
            or (gen.category_id if gen and gen.category_id else None)
        if category is None:
            raise ConfigError(f"Generator {i}: pick a category for the new channels (the generator isn't in one).")
        name = str(st.get("name") or "").strip()[:90]
        if mode in ("sequence", "predefined", "dynamic") and not name:
            raise ConfigError(f"Generator {i}: give the channels a name, like Gaming or {{username}}'s Room.")
        setups.append({
            "id": sid, "mode": mode, "channel_id": gen.id if gen else None, "category_id": category,
            "name": name or "{username}'s channel",
            "limit": _int(st, "limit", 0, 0, 99, f"Generator {i}'s user limit"),
            "bitrate": _int(st, "bitrate", 64, 8, 384, f"Generator {i}'s bitrate"),
            "editable": _bool(st, "editable", True),
            "permissions_from": "generator" if st.get("permissions_from") == "generator" else "category",
        })
    if len(setups) > 10:
        raise ConfigError("A server can have up to 10 generators.")
    if len({s["id"] for s in setups}) != len(setups):
        raise ConfigError("Two generators have the same id; remove one and add it again.")
    feats = d.get("features") or {}
    return {
        "enabled": _bool(d, "enabled", False), "setups": setups,
        "features": {f: bool(feats.get(f, True)) for f in VM_FEATURES},
        "owner_manage_channel": _bool(d, "owner_manage_channel", False),
        "owner_move_members": _bool(d, "owner_move_members", False),
        "interface": _bool(d, "interface", True), "interface_ping": _bool(d, "interface_ping", False),
        "log_channel_id": _channel(guild, d.get("log_channel_id"), "the VC maker's log", required=False),
        "lfm_channel_id": _channel(guild, d.get("lfm_channel_id"), "Looking for members posts", required=False),
        "temp_role_id": _optional_role(guild, d.get("temp_role_id"), "The channel owner role"),
        "auto_text": _bool(d, "auto_text", False),
        "text_access_role_id": _optional_role(guild, d.get("text_access_role_id"), "The text channel access role"),
        "text_message": messages.validate(d.get("text_message") or VM_TEXT_MESSAGE, "The text channel message"),
    }


def _tickets(guild, d):
    return {
        "log_channel_id": _channel(guild, d.get("log_channel_id"), "the ticket log", required=False),
        "transcript_channel_id": _channel(guild, d.get("transcript_channel_id"), "transcripts", required=False),
        "two_step_close": _bool(d, "two_step_close", True), "keep_closed": _bool(d, "keep_closed", True),
        "save_transcript": _bool(d, "save_transcript", True), "dm_transcript": _bool(d, "dm_transcript", False),
        "claiming": _bool(d, "claiming", True),
        "auto_close_hours": _int(d, "auto_close_hours", 0, 0, 720, "Auto-close after"),
        "close_when_owner_leaves": _bool(d, "close_when_owner_leaves", True),
        "global_limit": _int(d, "global_limit", 0, 0, 50, "Open tickets per person"),
    }


def _forumroles(guild, d):
    rules, seen = [], set()
    for i, r in enumerate(d.get("rules") or [], start=1):
        try:
            forum = guild.get_channel(int(r.get("forum_id") or 0))
        except (TypeError, ValueError):
            forum = None
        if not isinstance(forum, discord.ForumChannel):
            raise ConfigError(f"Rule {i}: pick the forum channel it watches.")
        tag = next((t for t in forum.available_tags if str(t.id) == str(r.get("tag_id"))), None)
        if tag is None:
            raise ConfigError(f"Rule {i}: pick one of #{forum.name}'s tags (that tag may have been deleted).")
        try:
            role = guild.get_role(int(r.get("role_id") or 0))
        except (TypeError, ValueError):
            role = None
        if role is None:
            raise ConfigError(f"Rule {i}: pick the role it gives.")
        problem = role_problem(guild, role)
        if problem:
            raise ConfigError(f"Rule {i}: {problem}")
        if (forum.id, tag.id) in seen:
            raise ConfigError(f"Rule {i}: #{forum.name} already has a rule for the {tag.name} tag.")
        seen.add((forum.id, tag.id))
        message_enabled = bool(r.get("message_enabled"))
        if message_enabled:
            p = forum.permissions_for(guild.me)
            if not (p.view_channel and p.send_messages_in_threads and p.embed_links):
                raise ConfigError(f"Rule {i}: to post in #{forum.name}'s posts the bot needs View Channel, "
                                  "Send Messages in Threads and Embed Links there.")
        dm_enabled = bool(r.get("dm_enabled"))
        rules.append({
            "forum_id": forum.id, "tag_id": tag.id, "role_id": role.id,
            "remove_on_untag": bool(r.get("remove_on_untag")),
            "message_enabled": message_enabled,
            "message": messages.validate(r.get("message"), f"Rule {i}'s message") if message_enabled else
                       (messages.upgrade(r["message"]) if r.get("message") else FORUM_MESSAGE),
            "dm_enabled": dm_enabled,
            "dm": messages.validate(r.get("dm"), f"Rule {i}'s DM") if dm_enabled else
                  (messages.upgrade(r["dm"]) if r.get("dm") else FORUM_DM),
        })
    if len(rules) > 25:
        raise ConfigError("A server can have up to 25 forum rules.")
    return {
        "enabled": _bool(d, "enabled", False),
        "log_channel_id": _channel(guild, d.get("log_channel_id"), "the forum-role log", required=False),
        "rules": rules,
    }


VALIDATORS = {"welcome": _welcome, "farewell": _farewell, "autorole": _autorole,
              "moderation": _moderation, "logging": _logging, "autoresponders": _autoresponders,
              "starboard": _starboard, "suggestions": _suggestions, "forumroles": _forumroles,
              "voicemaster": _voicemaster, "tickets": _tickets}
