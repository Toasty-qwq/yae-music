"""Placeholders in welcome / farewell text: {user}, {user.name}, {user.display}, {user.id},
{server}, {membercount}. Anything else in braces is left as it is."""
from __future__ import annotations

import re

import discord

PLACEHOLDERS = {
    "{user}": "mentions the member",
    "{user.name}": "their username",
    "{user.display}": "their name in this server",
    "{user.id}": "their user ID",
    "{server}": "the server's name",
    "{membercount}": "how many members the server has",
}
TOKEN = re.compile(r"\{(user(?:\.(?:name|display|id|mention))?|server|membercount|server\.member_count)\}")

# Text from the dashboard can contain anything; never let it ping @everyone or a role.
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)


def render(text: str, member: discord.abc.User, guild: discord.Guild) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in ("user", "user.mention"):
            return member.mention
        if key == "user.name":
            return discord.utils.escape_markdown(member.name)
        if key == "user.display":
            return discord.utils.escape_markdown(member.display_name)
        if key == "user.id":
            return str(member.id)
        if key == "server":
            return discord.utils.escape_markdown(guild.name)
        return str(guild.member_count or len(guild.members))
    return TOKEN.sub(sub, text)


def color(hex_value: str) -> discord.Colour:
    try:
        return discord.Colour(int(hex_value.lstrip("#"), 16))
    except (ValueError, AttributeError):
        return discord.Colour(0xF2879F)
