"""Welcome and farewell messages, the welcome DM, roles handed out on join, roles given back on
rejoin, and the /greetings commands."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from . import messages, ui
from .guildconfig import role_problem
from .templates import Ctx

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.welcome")


class Welcome(commands.Cog):
    greetings = app_commands.Group(name="greetings", description="Welcome and farewell messages",
                                   guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    # ---- sending -------------------------------------------------------------------------------

    async def send_welcome(self, member: discord.Member, *, test: bool = False,
                           channel: discord.abc.Messageable | None = None) -> str | None:
        """Post the welcome (and the DM). Returns a problem in words, or None."""
        cfg = await self.bot.configs.section(member.guild.id, "welcome")
        if not cfg["enabled"] and not test:
            return None
        ch = channel or member.guild.get_channel(cfg["channel_id"] or 0)
        if ch is None:
            return "No welcome channel is set. Pick one on the dashboard (Servers → Welcome)."
        try:
            await messages.send(ch, cfg["message"], Ctx(member.guild, member, ch))
        except discord.HTTPException as e:
            log.warning("guild %s: welcome failed: %s", member.guild.id, e)
            return f"Discord refused the message: {e.text or e}"
        if cfg["dm_enabled"] and not member.bot and (not test or channel is None):
            try:
                await messages.send(member, cfg["dm"], Ctx(member.guild, member))
            except discord.HTTPException:
                pass  # their DMs are closed: nothing to do
        return None

    async def send_farewell(self, member: discord.abc.User, guild: discord.Guild, *, test: bool = False,
                            channel: discord.abc.Messageable | None = None) -> str | None:
        cfg = await self.bot.configs.section(guild.id, "farewell")
        if not cfg["enabled"] and not test:
            return None
        ch = channel or guild.get_channel(cfg["channel_id"] or 0)
        if ch is None:
            return "No farewell channel is set. Pick one on the dashboard (Servers → Welcome)."
        try:
            await messages.send(ch, cfg["message"], Ctx(guild, member, ch))
        except discord.HTTPException as e:
            log.warning("guild %s: farewell failed: %s", guild.id, e)
            return f"Discord refused the message: {e.text or e}"
        return None

    # ---- roles ---------------------------------------------------------------------------------

    async def give_roles(self, member: discord.Member) -> None:
        guild = member.guild
        cfg = await self.bot.configs.section(guild.id, "autorole")
        ids: list[int] = []
        if cfg["enabled"]:
            ids += cfg["bot_role_ids"] if member.bot else cfg["role_ids"]
        if cfg["restore_roles"]:
            row = await self.bot.db.one("SELECT role_ids FROM member_roles WHERE guild_id=? AND user_id=?",
                                        (guild.id, member.id))
            if row:
                ids += json.loads(row["role_ids"])
                await self.bot.db.run("DELETE FROM member_roles WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        roles = [r for r in (guild.get_role(i) for i in dict.fromkeys(ids))
                 if r and r not in member.roles and not role_problem(guild, r)]
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Autorole / roles given back on rejoin")
            log.info("guild %s: gave %s %s", guild.id, member, ", ".join(r.name for r in roles))
        except discord.HTTPException as e:
            log.warning("guild %s: giving roles to %s failed: %s", guild.id, member, e)

    # ---- events --------------------------------------------------------------------------------

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
        if guild is None:
            return
        user = payload.user
        if isinstance(user, discord.Member) and not user.bot:
            cfg = await self.bot.configs.section(guild.id, "autorole")
            keep = [r.id for r in user.roles if not r.is_default() and not r.managed]
            if cfg["restore_roles"] and keep:
                await self.bot.db.run(
                    "INSERT INTO member_roles(guild_id, user_id, role_ids) VALUES(?,?,?) "
                    "ON CONFLICT(guild_id, user_id) DO UPDATE SET role_ids=excluded.role_ids",
                    (guild.id, user.id, json.dumps(keep)))
        await self.send_farewell(user, guild)

    # ---- /greetings ------------------------------------------------------------------------------

    @greetings.command(name="testgreet", description="Post the welcome message here, as if someone just joined")
    @app_commands.describe(member="Who it greets (you, if left empty)")
    async def testgreet(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True)
        problem = await self.send_welcome(member or interaction.user, test=True, channel=interaction.channel)
        await interaction.followup.send(embed=ui.error(problem) if problem else ui.ok(
            "That's the welcome message. It goes to the welcome channel when someone joins"
            + (" (it's switched off right now)." if not (await self.bot.configs.section(interaction.guild_id, "welcome"))["enabled"] else ".")),
            ephemeral=True)

    @greetings.command(name="testfarewell", description="Post the farewell message here, as if someone just left")
    @app_commands.describe(member="Who it says goodbye to (you, if left empty)")
    async def testfarewell(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True)
        problem = await self.send_farewell(member or interaction.user, interaction.guild, test=True,
                                           channel=interaction.channel)
        await interaction.followup.send(embed=ui.error(problem) if problem else ui.ok("That's the farewell message."),
                                        ephemeral=True)

    @greetings.command(name="testdm", description="Send yourself the welcome DM")
    async def testdm(self, interaction: discord.Interaction):
        cfg = await self.bot.configs.section(interaction.guild_id, "welcome")
        try:
            await messages.send(interaction.user, cfg["dm"], Ctx(interaction.guild, interaction.user))
        except discord.HTTPException:
            return await interaction.response.send_message(
                embed=ui.error("Your DMs are closed for this server, so the DM couldn't be sent."), ephemeral=True)
        await interaction.response.send_message(embed=ui.ok("Check your DMs."), ephemeral=True)

    @greetings.command(name="status", description="What's switched on: welcome, farewell, DM, autorole")
    async def status(self, interaction: discord.Interaction):
        g = interaction.guild
        cfg = await self.bot.configs.get(g.id)
        w, f, a = cfg["welcome"], cfg["farewell"], cfg["autorole"]

        def where(cid):
            return f"<#{cid}>" if cid else "no channel"
        roles = " ".join(f"<@&{r}>" for r in a["role_ids"]) or "none"
        e = discord.Embed(title="Greetings", colour=ui.COLOR)
        e.add_field(name="Welcome", value=f"{'On' if w['enabled'] else 'Off'} · {where(w['channel_id'])}")
        e.add_field(name="Farewell", value=f"{'On' if f['enabled'] else 'Off'} · {where(f['channel_id'])}")
        e.add_field(name="Welcome DM", value="On" if w["dm_enabled"] else "Off")
        e.add_field(name="Autorole", value=f"{'On' if a['enabled'] else 'Off'} · {roles}", inline=False)
        e.add_field(name="Give roles back on rejoin", value="On" if a["restore_roles"] else "Off", inline=False)
        e.set_footer(text="Change these on the dashboard: Servers → Welcome.")
        await interaction.response.send_message(embed=e, ephemeral=True)
