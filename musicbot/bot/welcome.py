"""Welcome and farewell messages, the welcome DM, and roles handed out on join."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from . import templates
from .guildconfig import role_problem

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.welcome")


def build(section: dict, member: discord.abc.User, guild: discord.Guild) -> dict:
    """Keyword arguments for ``channel.send`` from a welcome/farewell section."""
    text = templates.render(section["message"], member, guild)
    if not section.get("embed"):
        return {"content": text[:2000], "allowed_mentions": templates.SAFE_MENTIONS}
    e = discord.Embed(description=text[:4096], colour=templates.color(section["color"]))
    e.set_thumbnail(url=member.display_avatar.url)
    e.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    content = member.mention if section.get("ping") else None
    return {"content": content, "embed": e, "allowed_mentions": templates.SAFE_MENTIONS}


class Welcome(commands.Cog):
    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    async def send_welcome(self, member: discord.Member, *, test: bool = False) -> str | None:
        """Post the welcome (and DM). Returns a problem in words, or None."""
        cfg = await self.bot.configs.section(member.guild.id, "welcome")
        if not cfg["enabled"] and not test:
            return None
        ch = member.guild.get_channel(cfg["channel_id"] or 0)
        if ch is None:
            return "No welcome channel is set."
        try:
            await ch.send(**build(cfg, member, member.guild))
        except discord.HTTPException as e:
            log.warning("guild %s: welcome failed: %s", member.guild.id, e)
            return f"Discord refused the message: {e.text or e}"
        if cfg["dm_enabled"] and not test and not member.bot:
            try:
                await member.send(templates.render(cfg["dm_message"], member, member.guild)[:2000])
            except discord.HTTPException:
                pass  # their DMs are closed: nothing to do
        return None

    async def send_farewell(self, member: discord.abc.User, guild: discord.Guild, *, test: bool = False) -> str | None:
        cfg = await self.bot.configs.section(guild.id, "farewell")
        if not cfg["enabled"] and not test:
            return None
        ch = guild.get_channel(cfg["channel_id"] or 0)
        if ch is None:
            return "No farewell channel is set."
        try:
            await ch.send(**build(cfg, member, guild))
        except discord.HTTPException as e:
            log.warning("guild %s: farewell failed: %s", guild.id, e)
            return f"Discord refused the message: {e.text or e}"
        return None

    async def give_roles(self, member: discord.Member) -> None:
        cfg = await self.bot.configs.section(member.guild.id, "autorole")
        if not cfg["enabled"]:
            return
        ids = cfg["bot_role_ids"] if member.bot else cfg["role_ids"]
        roles = [r for r in (member.guild.get_role(i) for i in ids) if r and not role_problem(member.guild, r)]
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Autorole")
            log.info("guild %s: gave %s %s", member.guild.id, member, ", ".join(r.name for r in roles))
        except discord.HTTPException as e:
            log.warning("guild %s: autorole for %s failed: %s", member.guild.id, member, e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self.send_welcome(member)
        # With membership screening, roles wait until the member accepts the rules.
        if not member.pending:
            await self.give_roles(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.pending and not after.pending:
            await self.give_roles(after)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if guild:
            await self.send_farewell(payload.user, guild)
