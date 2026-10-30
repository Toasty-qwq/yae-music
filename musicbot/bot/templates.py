"""Placeholders in anything the bot posts for a server (welcome, farewell, tags, auto-replies...).

Both styles work, so Carl-bot habits carry over: ``{user.name}`` and ``{user(name)}``.
Unknown placeholders are left as they are.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import discord

PLACEHOLDERS = {
    "{user}": "mentions the member (pings them)",
    "{user.name}": "their username",
    "{user.display}": "their name in this server",
    "{user.id}": "their user ID",
    "{user.avatar}": "their avatar picture, for image fields",
    "{user.created}": "how old their account is",
    "{server}": "the server's name",
    "{server.icon}": "the server icon, for image fields",
    "{membercount}": "how many members the server has",
    "{channel}": "the channel it is posted in",
}
# {user}, {user.name}, {user(name)}, {mention}, {server.icon}, {server(icon)}, {membercount}, {channel} ...
TOKEN = re.compile(r"\{(user|mention|server|membercount|channel)(?:[.(]([a-z_]+)\)?)?\}", re.I)

# Text from the dashboard can contain anything; never let it ping @everyone or a role.
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)


@dataclass
class Ctx:
    """Who and where a message is about."""
    guild: discord.Guild
    user: discord.abc.User | None = None
    channel: discord.abc.GuildChannel | None = None


def _value(ctx: Ctx, kind: str, attr: str, url: bool) -> str | None:
    esc = (lambda s: s) if url else discord.utils.escape_markdown
    kind = kind.lower()
    attr = (attr or "").lower()
    g, u = ctx.guild, ctx.user
    if kind == "mention" or (kind == "user" and attr in ("", "mention")):
        return u.mention if u else ""
    if kind == "user":
        if u is None:
            return ""
        if attr in ("name", "username", "tag"):
            return esc(u.name)
        if attr in ("display", "nick", "nickname", "display_name"):
            return esc(getattr(u, "display_name", u.name))
        if attr == "id":
            return str(u.id)
        if attr in ("avatar", "icon", "avatar_url"):
            return u.display_avatar.url
        if attr in ("created", "created_at", "age"):
            return discord.utils.format_dt(u.created_at, "R")
        return None
    if kind == "server":
        if attr in ("", "name"):
            return esc(g.name)
        if attr in ("icon", "icon_url"):
            return g.icon.url if g.icon else ""
        if attr == "id":
            return str(g.id)
        if attr in ("members", "member_count", "membercount"):
            return str(g.member_count or len(g.members))
        return None
    if kind == "membercount":
        return str(g.member_count or len(g.members))
    if kind == "channel":
        return ctx.channel.mention if ctx.channel else ""
    return None


def render(text: str, ctx: Ctx, *, url: bool = False) -> str:
    if not text:
        return ""

    def sub(m: re.Match) -> str:
        v = _value(ctx, m.group(1), m.group(2) or "", url)
        return m.group(0) if v is None else v
    return TOKEN.sub(sub, text)


def color(hex_value: str) -> discord.Colour:
    try:
        return discord.Colour(int(hex_value.lstrip("#"), 16))
    except (ValueError, AttributeError):
        return discord.Colour(0xF2879F)
