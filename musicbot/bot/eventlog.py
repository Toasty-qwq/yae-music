"""Server event logs: each category goes to the channel picked for it on the dashboard.

messages    deleted / edited / bulk-deleted messages
members     joins, leaves, nickname and role changes
moderation  bans, unbans, kicks, timeouts (also ones done by hand, found through the audit log)
voice       joining, leaving and moving between voice channels
server      channels and roles created, deleted or renamed
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.eventlog")

RED, AMBER, GREEN, BLUE, PINK, GREY = 0xFF8A93, 0xEFBB70, 0x8FCFA3, 0x9EC1F0, 0xF2879F, 0xA492A2


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clip(text: str, n: int = 1000) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


class EventLog(commands.Cog):
    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    async def channel_for(self, guild: discord.Guild | None, category: str) -> discord.abc.Messageable | None:
        if guild is None:
            return None
        cfg = await self.bot.configs.section(guild.id, "logging")
        if not cfg["enabled"]:
            return None
        return guild.get_channel(cfg["channels"].get(category) or 0)

    async def post(self, guild: discord.Guild | None, category: str, embed: discord.Embed) -> None:
        ch = await self.channel_for(guild, category)
        if ch is None:
            return
        embed.timestamp = embed.timestamp or _now()
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("guild %s: %s log failed: %s", guild.id, category, e)

    async def skip_message(self, guild: discord.Guild | None, channel_id: int, author: discord.abc.User | None) -> bool:
        if guild is None:
            return True
        cfg = await self.bot.configs.section(guild.id, "logging")
        if channel_id in cfg["ignored_channel_ids"] or channel_id in cfg["channels"].values():
            return True  # never log the log channels themselves
        return bool(author and author.bot and cfg["ignore_bots"])

    @staticmethod
    def who(e: discord.Embed, user: discord.abc.User) -> discord.Embed:
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.set_footer(text=f"User ID {user.id}")
        return e

    async def audit(self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int,
                    within: float = 15) -> discord.AuditLogEntry | None:
        """The audit-log entry for something that just happened to ``target_id`` (who did it, why)."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(1.5)  # the entry lands shortly after the gateway event
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                if entry.target and entry.target.id == target_id and (_now() - entry.created_at).total_seconds() < within:
                    return entry
        except discord.HTTPException:
            pass
        return None

    @staticmethod
    def by(entry: discord.AuditLogEntry | None) -> str:
        if entry is None or entry.user is None:
            return ""
        reason = f"\n**Reason:** {_clip(entry.reason, 500)}" if entry.reason else ""
        return f"\n**By:** {entry.user.mention}{reason}"

    # ---- messages ------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, p: discord.RawMessageDeleteEvent):
        guild = self.bot.get_guild(p.guild_id) if p.guild_id else None
        m = p.cached_message
        if await self.skip_message(guild, p.channel_id, m.author if m else None):
            return
        if m is None:
            e = discord.Embed(description=f"A message was deleted in <#{p.channel_id}>. It was sent before the "
                                          "bot last started, so its text isn't known.", colour=RED)
            return await self.post(guild, "messages", e)
        e = discord.Embed(description=f"**Message deleted in {m.channel.mention}**\n{_clip(m.content, 3500) or '*(no text)*'}",
                          colour=RED)
        if m.attachments:
            e.add_field(name="Attachments", value=_clip("\n".join(a.filename for a in m.attachments), 1000))
        await self.post(guild, "messages", self.who(e, m.author))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, p: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(p.guild_id) if p.guild_id else None
        if await self.skip_message(guild, p.channel_id, None):
            return
        e = discord.Embed(description=f"**{len(p.message_ids)} messages bulk-deleted in <#{p.channel_id}>**",
                          colour=RED)
        await self.post(guild, "messages", e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content == after.content or await self.skip_message(after.guild, after.channel.id, after.author):
            return
        e = discord.Embed(description=f"**Message edited in {after.channel.mention}** · [jump]({after.jump_url})",
                          colour=AMBER)
        e.add_field(name="Before", value=_clip(before.content) or "*(no text)*", inline=False)
        e.add_field(name="After", value=_clip(after.content) or "*(no text)*", inline=False)
        await self.post(after.guild, "messages", self.who(e, after.author))

    # ---- members ---------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        age = _now() - member.created_at
        new = " **New account.**" if age.days < 7 else ""
        e = discord.Embed(description=f"**{member.mention} joined.** Account made {discord.utils.format_dt(member.created_at, 'R')}.{new}",
                          colour=GREEN)
        e.add_field(name="Members now", value=str(member.guild.member_count))
        await self.post(member.guild, "members", self.who(e, member))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, p: discord.RawMemberRemoveEvent):
        guild = self.bot.get_guild(p.guild_id)
        if guild is None:
            return
        user = p.user
        roles = ""
        if isinstance(user, discord.Member):
            names = [r.mention for r in reversed(user.roles) if not r.is_default()]
            roles = ("\n**Roles:** " + _clip(" ".join(names), 900)) if names else ""
        e = discord.Embed(description=f"**{user.mention} left.**{roles}", colour=GREY)
        await self.post(guild, "members", self.who(e, user))
        kick = await self.audit(guild, discord.AuditLogAction.kick, user.id)
        if kick:
            k = discord.Embed(description=f"**{user.mention} was kicked.**{self.by(kick)}", colour=AMBER)
            await self.post(guild, "moderation", self.who(k, user))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if before.nick != after.nick:
            e = discord.Embed(description=f"**{after.mention} changed nickname**\n"
                                          f"{discord.utils.escape_markdown(before.nick or before.name)} → "
                                          f"{discord.utils.escape_markdown(after.nick or after.name)}", colour=BLUE)
            await self.post(g, "members", self.who(e, after))
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            lines = []
            if added:
                lines.append("**Added:** " + " ".join(r.mention for r in added))
            if removed:
                lines.append("**Removed:** " + " ".join(r.mention for r in removed))
            e = discord.Embed(description=f"**{after.mention}'s roles changed**\n" + "\n".join(lines), colour=PINK)
            await self.post(g, "members", self.who(e, after))
        if before.timed_out_until != after.timed_out_until:
            if after.is_timed_out():
                entry = await self.audit(g, discord.AuditLogAction.member_update, after.id)
                e = discord.Embed(description=f"**{after.mention} was timed out** until "
                                              f"{discord.utils.format_dt(after.timed_out_until, 'f')}.{self.by(entry)}",
                                  colour=AMBER)
            else:
                e = discord.Embed(description=f"**{after.mention}'s timeout ended.**", colour=GREEN)
            await self.post(g, "moderation", self.who(e, after))

    # ---- moderation --------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        entry = await self.audit(guild, discord.AuditLogAction.ban, user.id)
        e = discord.Embed(description=f"**{user.mention} was banned.**{self.by(entry)}", colour=RED)
        await self.post(guild, "moderation", self.who(e, user))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        entry = await self.audit(guild, discord.AuditLogAction.unban, user.id)
        e = discord.Embed(description=f"**{user.mention} was unbanned.**{self.by(entry)}", colour=GREEN)
        await self.post(guild, "moderation", self.who(e, user))

    # ---- voice -------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        if before.channel == after.channel:
            return
        if member.bot and (await self.bot.configs.section(member.guild.id, "logging"))["ignore_bots"]:
            return
        if before.channel is None:
            text, colour = f"**{member.mention} joined** {after.channel.mention}", GREEN
        elif after.channel is None:
            text, colour = f"**{member.mention} left** {before.channel.mention}", GREY
        else:
            text, colour = f"**{member.mention} moved** {before.channel.mention} → {after.channel.mention}", BLUE
        await self.post(member.guild, "voice", self.who(discord.Embed(description=text, colour=colour), member))

    # ---- server ------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch: discord.abc.GuildChannel):
        await self.post(ch.guild, "server", discord.Embed(description=f"**Channel created:** {ch.mention}", colour=GREEN))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, ch: discord.abc.GuildChannel):
        await self.post(ch.guild, "server", discord.Embed(description=f"**Channel deleted:** #{ch.name}", colour=RED))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if before.name != after.name:
            await self.post(after.guild, "server", discord.Embed(
                description=f"**Channel renamed:** #{before.name} → {after.mention}", colour=BLUE))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        await self.post(role.guild, "server", discord.Embed(description=f"**Role created:** {role.mention}", colour=GREEN))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await self.post(role.guild, "server", discord.Embed(description=f"**Role deleted:** @{role.name}", colour=RED))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"name @{before.name} → @{after.name}")
        if before.colour != after.colour:
            changes.append(f"colour {before.colour} → {after.colour}")
        if before.permissions != after.permissions:
            gained = [p for p, v in after.permissions if v and not getattr(before.permissions, p)]
            lost = [p for p, v in after.permissions if not v and getattr(before.permissions, p)]
            if gained:
                changes.append("can now " + ", ".join(p.replace("_", " ") for p in gained))
            if lost:
                changes.append("can no longer " + ", ".join(p.replace("_", " ") for p in lost))
        if changes:
            await self.post(after.guild, "server", discord.Embed(
                description=f"**Role changed:** {after.mention}\n" + _clip("\n".join(changes), 3500), colour=BLUE))
