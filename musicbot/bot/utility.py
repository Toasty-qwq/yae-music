"""Reminders and the small everyday commands: /remind, /reminders, /userinfo, /serverinfo, /avatar, /say."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import ui
from .moderation import human, parse_duration

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.utility")
MAX_REMINDERS = 25


class Utility(commands.Cog):
    reminders = app_commands.Group(name="reminders", description="Your reminders")

    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.due.start()

    async def cog_unload(self) -> None:
        self.due.cancel()

    # ---- reminders ---------------------------------------------------------------------------

    @app_commands.command(name="remind", description="Get a reminder later, e.g. in 2h")
    @app_commands.describe(when="How long from now: 10m, 2h, 1d, 1w", what="What to remind you of")
    async def remind(self, interaction: discord.Interaction, when: str, what: app_commands.Range[str, 1, 1000]):
        try:
            seconds = parse_duration(when)
        except ValueError as e:
            return await interaction.response.send_message(embed=ui.error(str(e)), ephemeral=True)
        if not seconds or seconds < 30 or seconds > 365 * 86400:
            return await interaction.response.send_message(embed=ui.error("Pick a time between 30 seconds and a year."),
                                                           ephemeral=True)
        n = (await self.bot.db.one("SELECT COUNT(*) AS n FROM reminders WHERE user_id=?", (interaction.user.id,)))["n"]
        if n >= MAX_REMINDERS:
            return await interaction.response.send_message(
                embed=ui.error(f"You have {n} reminders already. Remove some with /reminders delete."), ephemeral=True)
        due = time.time() + seconds
        rid = await self.bot.db.run(
            "INSERT INTO reminders(user_id, guild_id, channel_id, due, text, created_at) VALUES(?,?,?,?,?,?)",
            (interaction.user.id, interaction.guild_id, interaction.channel_id, due, what, time.time()))
        await interaction.response.send_message(
            embed=ui.ok(f"Reminder {rid} set for <t:{int(due)}:f> (in {human(seconds)})."), ephemeral=True)

    @reminders.command(name="list", description="Your reminders that haven't gone off yet")
    async def list_cmd(self, interaction: discord.Interaction):
        rows = await self.bot.db.all("SELECT * FROM reminders WHERE user_id=? ORDER BY due", (interaction.user.id,))
        if not rows:
            return await interaction.response.send_message(embed=ui.ok("You have no reminders. Set one with /remind."),
                                                           ephemeral=True)
        lines = [f"`{r['id']}` <t:{int(r['due'])}:R>: {discord.utils.escape_markdown(r['text'][:80])}" for r in rows]
        await interaction.response.send_message(embed=discord.Embed(title="Your reminders", description="\n".join(lines),
                                                                    colour=ui.COLOR), ephemeral=True)

    @reminders.command(name="delete", description="Remove a reminder")
    @app_commands.describe(number="Its number, from /reminders list")
    async def delete(self, interaction: discord.Interaction, number: int):
        row = await self.bot.db.one("SELECT id FROM reminders WHERE id=? AND user_id=?", (number, interaction.user.id))
        if not row:
            return await interaction.response.send_message(embed=ui.error(f"You have no reminder {number}."), ephemeral=True)
        await self.bot.db.run("DELETE FROM reminders WHERE id=?", (number,))
        await interaction.response.send_message(embed=ui.ok(f"Reminder {number} removed."), ephemeral=True)

    @tasks.loop(seconds=15)
    async def due(self):
        for r in await self.bot.db.all("SELECT * FROM reminders WHERE due<=? ORDER BY due LIMIT 50", (time.time(),)):
            await self.bot.db.run("DELETE FROM reminders WHERE id=?", (r["id"],))
            text = f"<@{r['user_id']}>, reminder from <t:{int(r['created_at'])}:R>: {r['text']}"
            channel = self.bot.get_channel(r["channel_id"] or 0)
            sent = False
            if channel is not None:
                try:
                    await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False))
                    sent = True
                except discord.HTTPException:
                    pass
            if not sent:
                try:
                    user = self.bot.get_user(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
                    await user.send(text[:2000])
                except discord.HTTPException:
                    log.info("reminder %s couldn't be delivered", r["id"])

    @due.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()

    # ---- info ---------------------------------------------------------------------------------

    @app_commands.command(name="userinfo", description="Facts about someone: account age, join date, roles")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        m = member or interaction.user
        e = discord.Embed(title=str(m), colour=m.colour if m.colour.value else ui.COLOR)
        e.set_thumbnail(url=m.display_avatar.url)
        e.add_field(name="Account made", value=discord.utils.format_dt(m.created_at, "R"))
        if m.joined_at:
            e.add_field(name="Joined here", value=discord.utils.format_dt(m.joined_at, "R"))
        roles = [r.mention for r in reversed(m.roles) if not r.is_default()]
        e.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles)[:1024] or "none", inline=False)
        if m.is_timed_out():
            e.add_field(name="Timed out until", value=discord.utils.format_dt(m.timed_out_until, "f"))
        e.set_footer(text=f"User ID {m.id}")
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="serverinfo", description="Facts about this server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        e = discord.Embed(title=g.name, description=g.description or None, colour=ui.COLOR)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name="Owner", value=f"<@{g.owner_id}>")
        e.add_field(name="Members", value=str(g.member_count))
        e.add_field(name="Made", value=discord.utils.format_dt(g.created_at, "R"))
        e.add_field(name="Channels", value=f"{len(g.text_channels)} text, {len(g.voice_channels)} voice")
        e.add_field(name="Roles", value=str(len(g.roles) - 1))
        e.add_field(name="Boosts", value=f"{g.premium_subscription_count} (level {g.premium_tier})")
        e.set_footer(text=f"Server ID {g.id}")
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="avatar", description="Someone's avatar, full size")
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None):
        u = user or interaction.user
        e = discord.Embed(title=f"{u.display_name}'s avatar", colour=ui.COLOR, url=u.display_avatar.url)
        e.set_image(url=u.display_avatar.with_size(1024).url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="say", description="Have the bot post a message")
    @app_commands.describe(text="What to post", channel="Where (this channel if empty)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 2000],
                  channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        perms = ch.permissions_for(interaction.user)
        if not perms.send_messages:
            return await interaction.response.send_message(embed=ui.error(f"You can't post in {ch.mention} yourself."),
                                                           ephemeral=True)
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        except discord.HTTPException as e:
            return await interaction.response.send_message(embed=ui.error(f"Discord refused: {e.text or e}"), ephemeral=True)
        log.info("guild %s: /say by %s in #%s", interaction.guild_id, interaction.user, ch)
        await interaction.response.send_message(embed=ui.ok(f"Posted in {ch.mention}."), ephemeral=True)
