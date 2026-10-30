"""Moderation: /ban /unban /kick /timeout /untimeout /warn /warnings /case /reason /purge.

Every action becomes a numbered case (stored in the database) and, when a mod-log channel is set,
an entry there. Temporary bans are lifted by a loop that checks every 30 seconds.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import ui

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.moderation")

ACTION_COLOR = {"ban": 0xFF8A93, "unban": 0x8FCFA3, "kick": 0xEFBB70, "timeout": 0xEFBB70,
                "untimeout": 0x8FCFA3, "warn": 0xF0D584}
ACTION_WORD = {"ban": "Ban", "unban": "Unban", "kick": "Kick", "timeout": "Timeout",
               "untimeout": "Timeout removed", "warn": "Warning"}
DURATION = re.compile(r"(\d+)\s*(w|d|h|m|s)", re.I)
UNIT = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
MAX_TIMEOUT = 28 * 86400


def parse_duration(text: str | None) -> int | None:
    """'1d12h', '30m', '2w' -> seconds. None for empty text; ValueError for nonsense."""
    if not text or not text.strip():
        return None
    text = text.strip().lower()
    parts = DURATION.findall(text)
    if not parts or DURATION.sub("", text).strip():
        raise ValueError(f"{text!r} isn't a length of time. Use something like 30m, 2h, 1d or 1w.")
    return sum(int(n) * UNIT[u] for n, u in parts)


def human(seconds: float) -> str:
    seconds = int(seconds)
    out = []
    for name, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        n, seconds = divmod(seconds, size)
        if n:
            out.append(f"{n} {name}{'s' if n != 1 else ''}")
    return ", ".join(out[:2]) or f"{seconds} seconds"


class Moderation(commands.Cog):
    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.tempbans.start()

    async def cog_unload(self) -> None:
        self.tempbans.cancel()

    # ---- helpers ---------------------------------------------------------------------------

    def hierarchy_problem(self, guild: discord.Guild, moderator: discord.Member, target: discord.abc.User,
                          verb: str) -> str | None:
        if target.id == moderator.id:
            return f"You can't {verb} yourself."
        if self.bot.user and target.id == self.bot.user.id:
            return f"The bot can't {verb} itself."
        if target.id == guild.owner_id:
            return f"Nobody can {verb} the server owner."
        if isinstance(target, discord.Member):
            if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
                return f"{target.mention}'s highest role is the same as or above yours, so you can't {verb} them."
            if target.top_role >= guild.me.top_role:
                return (f"{target.mention}'s highest role is above the bot's. Drag the bot's role higher in "
                        "Server Settings → Roles.")
        return None

    async def record(self, guild: discord.Guild, action: str, user: discord.abc.User, moderator: discord.abc.User,
                     reason: str, *, expires_at: float | None = None) -> dict:
        case = await self.bot.db.add_case(
            guild.id, action=action, user_id=user.id, user_name=str(user), mod_id=moderator.id,
            mod_name=str(moderator), reason=reason, expires_at=expires_at,
            active=1 if action in ("ban", "timeout", "warn") else 0)
        cfg = await self.bot.configs.section(guild.id, "moderation")
        ch = guild.get_channel(cfg["modlog_channel_id"] or 0)
        if ch:
            try:
                msg = await ch.send(embed=self.case_embed(case))
                await self.bot.db.update_case(guild.id, case["case_no"], log_channel_id=ch.id, log_message_id=msg.id)
            except discord.HTTPException as e:
                log.warning("guild %s: mod log post failed: %s", guild.id, e)
        log.info("guild %s: case %s %s %s by %s: %s", guild.id, case["case_no"], action, user, moderator, reason)
        return case

    @staticmethod
    def case_embed(c: dict) -> discord.Embed:
        e = discord.Embed(title=f"Case {c['case_no']} · {ACTION_WORD.get(c['action'], c['action'])}",
                          colour=ACTION_COLOR.get(c["action"], ui.COLOR),
                          timestamp=dt.datetime.fromtimestamp(c["created_at"], dt.timezone.utc))
        e.add_field(name="Member", value=f"<@{c['user_id']}> ({discord.utils.escape_markdown(c['user_name'])})")
        e.add_field(name="Moderator", value=f"<@{c['mod_id']}>")
        e.add_field(name="Reason", value=c["reason"][:1024] or "No reason given", inline=False)
        if c.get("expires_at"):
            e.add_field(name="Until", value=f"<t:{int(c['expires_at'])}:f> (<t:{int(c['expires_at'])}:R>)",
                        inline=False)
        e.set_footer(text=f"User ID {c['user_id']}")
        return e

    async def tell(self, guild: discord.Guild, user: discord.abc.User, text: str) -> bool:
        cfg = await self.bot.configs.section(guild.id, "moderation")
        if not cfg["dm_users"] or user.bot:
            return False
        try:
            await user.send(text[:2000])
            return True
        except discord.HTTPException:
            return False

    async def done(self, interaction: discord.Interaction, case: dict, line: str, dmed: bool) -> None:
        note = "" if dmed else " (they weren't told by DM: their DMs are closed, or DMs are off for this server)"
        await interaction.followup.send(embed=ui.ok(f"Case {case['case_no']}: {line}{note}."))

    # ---- commands ----------------------------------------------------------------------------

    @app_commands.command(description="Ban someone, for good or for a while")
    @app_commands.describe(user="Who to ban", reason="Why (they see this in the DM)",
                           duration="Leave empty for a permanent ban, or e.g. 1d, 2w",
                           delete_messages="Also delete their recent messages")
    @app_commands.choices(delete_messages=[
        app_commands.Choice(name="Don't delete any", value=0),
        app_commands.Choice(name="The last hour", value=3600),
        app_commands.Choice(name="The last 24 hours", value=86400),
        app_commands.Choice(name="The last 7 days", value=604800),
    ])
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: str = "",
                  duration: str = "", delete_messages: int = 0):
        guild, mod = interaction.guild, interaction.user
        assert guild and isinstance(mod, discord.Member)
        target = guild.get_member(user.id) or user
        problem = self.hierarchy_problem(guild, mod, target, "ban")
        if problem:
            return await interaction.response.send_message(embed=ui.error(problem), ephemeral=True)
        try:
            seconds = parse_duration(duration)
        except ValueError as e:
            return await interaction.response.send_message(embed=ui.error(str(e)), ephemeral=True)
        await interaction.response.defer()
        until = time.time() + seconds if seconds else None
        how_long = f" for {human(seconds)}" if seconds else ""
        dmed = await self.tell(guild, user, f"You were banned from **{guild.name}**{how_long}."
                                            + (f"\nReason: {reason}" if reason else ""))
        try:
            await guild.ban(user, reason=f"{mod}: {reason or 'no reason given'}"[:512],
                            delete_message_seconds=delete_messages)
        except discord.HTTPException as e:
            return await interaction.followup.send(embed=ui.error(f"Discord refused the ban: {e.text or e}"))
        await self.bot.db.deactivate_bans(guild.id, user.id)  # a new ban replaces an older temporary one
        case = await self.record(guild, "ban", user, mod, reason, expires_at=until)
        await self.done(interaction, case, f"banned {user.mention}{how_long}", dmed)

    @app_commands.command(description="Lift a ban")
    @app_commands.describe(user_id="The banned person's user ID (or their @mention)", reason="Why")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = ""):
        guild = interaction.guild
        assert guild
        digits = re.sub(r"\D", "", user_id)
        if not digits:
            return await interaction.response.send_message(embed=ui.error("Give the user's ID: right-click them → Copy User ID."), ephemeral=True)
        await interaction.response.defer()
        try:
            ban = await guild.fetch_ban(discord.Object(int(digits)))
            await guild.unban(ban.user, reason=f"{interaction.user}: {reason or 'no reason given'}"[:512])
        except discord.NotFound:
            return await interaction.followup.send(embed=ui.error("That user isn't banned here."))
        except discord.HTTPException as e:
            return await interaction.followup.send(embed=ui.error(f"Discord refused the unban: {e.text or e}"))
        await self.bot.db.deactivate_bans(guild.id, ban.user.id)
        case = await self.record(guild, "unban", ban.user, interaction.user, reason)
        await self.done(interaction, case, f"unbanned {ban.user.mention}", True)

    @app_commands.command(description="Kick someone out (they can come back with an invite)")
    @app_commands.describe(member="Who to kick", reason="Why (they see this in the DM)")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = ""):
        guild, mod = interaction.guild, interaction.user
        assert guild and isinstance(mod, discord.Member)
        problem = self.hierarchy_problem(guild, mod, member, "kick")
        if problem:
            return await interaction.response.send_message(embed=ui.error(problem), ephemeral=True)
        await interaction.response.defer()
        dmed = await self.tell(guild, member, f"You were kicked from **{guild.name}**."
                                              + (f"\nReason: {reason}" if reason else ""))
        try:
            await guild.kick(member, reason=f"{mod}: {reason or 'no reason given'}"[:512])
        except discord.HTTPException as e:
            return await interaction.followup.send(embed=ui.error(f"Discord refused the kick: {e.text or e}"))
        case = await self.record(guild, "kick", member, mod, reason)
        await self.done(interaction, case, f"kicked {member.mention}", dmed)

    @app_commands.command(description="Time someone out: they can't talk or join voice for a while")
    @app_commands.describe(member="Who", duration="How long, e.g. 10m, 2h, 1d (at most 28d)", reason="Why")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str,
                      reason: str = ""):
        guild, mod = interaction.guild, interaction.user
        assert guild and isinstance(mod, discord.Member)
        problem = self.hierarchy_problem(guild, mod, member, "time out")
        if problem:
            return await interaction.response.send_message(embed=ui.error(problem), ephemeral=True)
        try:
            seconds = parse_duration(duration)
        except ValueError as e:
            return await interaction.response.send_message(embed=ui.error(str(e)), ephemeral=True)
        if not seconds or seconds > MAX_TIMEOUT:
            return await interaction.response.send_message(
                embed=ui.error("A timeout lasts from a few seconds up to 28 days. For longer, use /ban with a duration."),
                ephemeral=True)
        await interaction.response.defer()
        try:
            await member.timeout(dt.timedelta(seconds=seconds), reason=f"{mod}: {reason or 'no reason given'}"[:512])
        except discord.HTTPException as e:
            return await interaction.followup.send(embed=ui.error(f"Discord refused the timeout: {e.text or e}"))
        dmed = await self.tell(guild, member, f"You were timed out in **{guild.name}** for {human(seconds)}."
                                              + (f"\nReason: {reason}" if reason else ""))
        case = await self.record(guild, "timeout", member, mod, reason, expires_at=time.time() + seconds)
        await self.done(interaction, case, f"timed out {member.mention} for {human(seconds)}", dmed)

    @app_commands.command(description="End someone's timeout early")
    @app_commands.describe(member="Who", reason="Why")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = ""):
        guild = interaction.guild
        assert guild
        if not member.is_timed_out():
            return await interaction.response.send_message(embed=ui.error(f"{member.mention} isn't timed out."),
                                                           ephemeral=True)
        await interaction.response.defer()
        try:
            await member.timeout(None, reason=f"{interaction.user}: {reason or 'no reason given'}"[:512])
        except discord.HTTPException as e:
            return await interaction.followup.send(embed=ui.error(f"Discord refused: {e.text or e}"))
        case = await self.record(guild, "untimeout", member, interaction.user, reason)
        await self.done(interaction, case, f"ended {member.mention}'s timeout", True)

    @app_commands.command(description="Give someone a warning (it's kept in their history)")
    @app_commands.describe(member="Who", reason="What for (they see this in the DM)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        guild, mod = interaction.guild, interaction.user
        assert guild and isinstance(mod, discord.Member)
        problem = self.hierarchy_problem(guild, mod, member, "warn")
        if problem:
            return await interaction.response.send_message(embed=ui.error(problem), ephemeral=True)
        await interaction.response.defer()
        dmed = await self.tell(guild, member, f"You were warned in **{guild.name}**.\nReason: {reason}")
        case = await self.record(guild, "warn", member, mod, reason)
        _, total = await self.bot.db.cases(guild.id, user_id=member.id, action="warn", limit=1)
        await self.done(interaction, case, f"warned {member.mention} (warning number {total})", dmed)

    @app_commands.command(description="Someone's moderation history")
    @app_commands.describe(user="Whose history")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, user: discord.User):
        guild = interaction.guild
        assert guild
        rows, total = await self.bot.db.cases(guild.id, user_id=user.id, limit=15)
        if not rows:
            return await interaction.response.send_message(embed=ui.ok(f"{user.mention} has a clean record here."),
                                                           ephemeral=True)
        lines = [f"`#{c['case_no']}` **{ACTION_WORD.get(c['action'], c['action'])}** <t:{int(c['created_at'])}:d>"
                 f" by <@{c['mod_id']}>: {discord.utils.escape_markdown(c['reason'][:90]) or 'no reason given'}"
                 for c in rows]
        e = discord.Embed(title=f"History of {user}", description="\n".join(lines), colour=ui.COLOR)
        if total > len(rows):
            e.set_footer(text=f"Newest {len(rows)} of {total} cases. See every case on the dashboard.")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(description="Show one moderation case")
    @app_commands.describe(number="The case number")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def case(self, interaction: discord.Interaction, number: app_commands.Range[int, 1]):
        c = await self.bot.db.case(interaction.guild_id, number)
        if not c:
            return await interaction.response.send_message(embed=ui.error(f"There is no case {number}."), ephemeral=True)
        await interaction.response.send_message(embed=self.case_embed(c), ephemeral=True)

    @app_commands.command(description="Change the reason on a case")
    @app_commands.describe(number="The case number", reason="The new reason")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def reason(self, interaction: discord.Interaction, number: app_commands.Range[int, 1], reason: str):
        guild = interaction.guild
        assert guild
        c = await self.bot.db.case(guild.id, number)
        if not c:
            return await interaction.response.send_message(embed=ui.error(f"There is no case {number}."), ephemeral=True)
        await self.bot.db.update_case(guild.id, number, reason=reason[:1000])
        c["reason"] = reason[:1000]
        ch = guild.get_channel(c.get("log_channel_id") or 0)
        if ch and c.get("log_message_id"):
            try:
                await ch.get_partial_message(c["log_message_id"]).edit(embed=self.case_embed(c))
            except discord.HTTPException:
                pass
        await interaction.response.send_message(embed=ui.ok(f"Case {number}'s reason is now: {reason[:200]}"),
                                                ephemeral=True)

    @app_commands.command(description="Delete recent messages in this channel")
    @app_commands.describe(amount="How many to look through (1-500)", user="Only this person's messages")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500],
                    user: discord.User | None = None):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            return await interaction.response.send_message(embed=ui.error("This only works in a text channel."),
                                                           ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == user.id) if user else (lambda m: True)
        try:
            deleted = await channel.purge(limit=amount, check=check, reason=f"/purge by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.followup.send(embed=ui.error(f"Discord refused: {e.text or e}"), ephemeral=True)
        who = f" from {user.mention}" if user else ""
        log.info("guild %s: %s purged %d messages%s in #%s", interaction.guild_id, interaction.user, len(deleted),
                 f" from {user}" if user else "", channel)
        await interaction.followup.send(
            embed=ui.ok(f"Deleted {len(deleted)} message{'s' if len(deleted) != 1 else ''}{who}. "
                        "Messages older than 14 days can't be bulk-deleted."), ephemeral=True)

    # ---- temporary bans ----------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def tempbans(self):
        for c in await self.bot.db.due_tempbans(time.time()):
            guild = self.bot.get_guild(c["guild_id"])
            await self.bot.db.update_case(c["guild_id"], c["case_no"], active=0)
            if guild is None:
                continue
            try:
                await guild.unban(discord.Object(c["user_id"]), reason=f"Temporary ban ended (case {c['case_no']})")
            except discord.NotFound:
                continue  # someone already lifted it
            except discord.HTTPException as e:
                log.warning("guild %s: lifting temp-ban case %s failed: %s", guild.id, c["case_no"], e)
                continue
            await self.record(guild, "unban", _Named(c["user_id"], c["user_name"]), guild.me,
                              f"Temporary ban from case {c['case_no']} ended")

    @tempbans.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("moderation command failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        try:
            await send(embed=ui.error("Something went wrong running that command. The panel's log has the details."),
                       ephemeral=True)
        except discord.HTTPException:
            pass


class _Named:
    """Enough of a user for record(): an id and a name (the banned user isn't in the server)."""

    def __init__(self, user_id: int, name: str):
        self.id = user_id
        self.name = name
        self.bot = False

    def __str__(self) -> str:
        return self.name

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"
