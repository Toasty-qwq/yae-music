"""Forum roles: when a forum post gets a chosen tag (say "Accepted"), the person who made the post gets
a role. Optionally: the role goes again when the tag is taken off, a message in the post, a DM.

Discord sends a thread update when a post's tags change; the bot compares the tags before and after.
Whoever added the tag doesn't matter, which is why the dashboard asks for moderator-only tags.
When the bot starts it catches up on posts tagged while it was offline: it gives those roles quietly
(no message, no DM) so a restart never floods old posts.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from . import messages
from .guildconfig import role_problem
from .templates import Ctx

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.forumroles")


def tag_ids(thread: discord.Thread) -> set[int]:
    return {t.id for t in thread.applied_tags}


class ForumRoles(commands.Cog):
    def __init__(self, bot: "MusicBot"):
        self.bot = bot
        self._caught_up = False

    # ---- events --------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        if not isinstance(after.parent, discord.ForumChannel):
            return
        old, new = tag_ids(before), tag_ids(after)
        if old == new:
            return
        cfg = await self.bot.configs.section(after.guild.id, "forumroles")
        if not cfg["enabled"]:
            return
        for rule in cfg["rules"]:
            if rule["forum_id"] != after.parent_id:
                continue
            if rule["tag_id"] in new - old:
                await self.grant(after, rule, cfg)
            elif rule["tag_id"] in old - new and rule["remove_on_untag"]:
                await self.revoke(after, rule, cfg)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._caught_up:
            return  # on_ready also fires after reconnects
        self._caught_up = True
        for guild in self.bot.guilds:
            try:
                await self.catch_up(guild)
            except Exception:  # noqa: BLE001 — one server's trouble must not stop the others
                log.exception("guild %s: forum-role catch-up failed", guild.id)

    # ---- giving and taking -----------------------------------------------------------------------

    async def owner(self, thread: discord.Thread) -> discord.Member | None:
        m = thread.guild.get_member(thread.owner_id)
        if m is None:
            try:
                m = await thread.guild.fetch_member(thread.owner_id)
            except discord.HTTPException:
                return None
        return m

    async def grant(self, thread: discord.Thread, rule: dict, cfg: dict, *, quiet: bool = False) -> str:
        """Give the post's author the rule's role. Returns what happened, in words."""
        guild = thread.guild
        role = guild.get_role(rule["role_id"])
        member = await self.owner(thread)
        tag = next((t.name for t in thread.applied_tags if t.id == rule["tag_id"]), "the tag")
        if member is None:
            await self.note(guild, cfg, f"**{discord.utils.escape_markdown(thread.name)}** ({thread.mention}) got "
                                        f"**{tag}**, but its author left the server, so nobody got a role.")
            return "author left"
        if role is None or role_problem(guild, role):
            await self.note(guild, cfg, f"{thread.mention} got **{tag}**, but the bot can't give "
                                        f"{role.mention if role else 'the role (it was deleted)'}: "
                                        f"{role_problem(guild, role) if role else 'pick another on the dashboard'}.")
            return "role problem"
        had = role in member.roles
        if not had:
            try:
                await member.add_roles(role, reason=f"Forum post tagged {tag}: {thread.name}"[:512])
            except discord.HTTPException as e:
                log.warning("guild %s: forum role for %s failed: %s", guild.id, member, e)
                await self.note(guild, cfg, f"Giving {role.mention} to {member.mention} for {thread.mention} failed: {e.text or e}")
                return "failed"
        await self.bot.db.run(
            "INSERT OR REPLACE INTO forum_grants(guild_id, thread_id, tag_id, user_id, role_id, granted_at) VALUES(?,?,?,?,?,?)",
            (guild.id, thread.id, rule["tag_id"], member.id, role.id, time.time()))
        log.info("guild %s: %s got @%s for forum post %r (%s)%s", guild.id, member, role.name, thread.name, tag,
                 " quietly" if quiet else "")
        if not quiet:
            ctx = Ctx(guild, member, thread)
            if rule["message_enabled"]:
                try:
                    if thread.archived:
                        await thread.edit(archived=False)
                    await messages.send(thread, rule["message"], ctx)
                except discord.HTTPException as e:
                    log.warning("guild %s: message in forum post %s failed: %s", guild.id, thread.id, e)
            if rule["dm_enabled"]:
                try:
                    await messages.send(member, rule["dm"], ctx)
                except discord.HTTPException:
                    pass  # DMs closed
        who = await self.tagged_by(thread)
        await self.note(guild, cfg, f"{member.mention} {'already had' if had else 'got'} {role.mention}: their post "
                                    f"{thread.mention} was tagged **{tag}**{who}{' while the bot was offline' if quiet else ''}.")
        return "already had it" if had else "given"

    async def revoke(self, thread: discord.Thread, rule: dict, cfg: dict) -> None:
        guild = thread.guild
        role = guild.get_role(rule["role_id"])
        member = await self.owner(thread)
        await self.bot.db.run("DELETE FROM forum_grants WHERE guild_id=? AND thread_id=? AND tag_id=?",
                              (guild.id, thread.id, rule["tag_id"]))
        if member is None or role is None or role not in member.roles or role_problem(guild, role):
            return
        # Another post of theirs with the same tag still earns the role: keep it then.
        other = await self.bot.db.one(
            "SELECT 1 FROM forum_grants WHERE guild_id=? AND user_id=? AND role_id=? AND thread_id!=?",
            (guild.id, member.id, role.id, thread.id))
        if other:
            return
        try:
            await member.remove_roles(role, reason=f"Forum post untagged: {thread.name}"[:512])
        except discord.HTTPException as e:
            log.warning("guild %s: removing forum role from %s failed: %s", guild.id, member, e)
            return
        who = await self.tagged_by(thread)
        await self.note(guild, cfg, f"{member.mention} lost {role.mention}: the tag was taken off {thread.mention}{who}.")

    async def catch_up(self, guild: discord.Guild) -> None:
        cfg = await self.bot.configs.section(guild.id, "forumroles")
        if not cfg["enabled"] or not cfg["rules"]:
            return
        given = 0
        for rule in cfg["rules"]:
            forum = guild.get_channel(rule["forum_id"])
            if not isinstance(forum, discord.ForumChannel):
                continue
            threads = list(forum.threads)
            try:
                threads += [t async for t in forum.archived_threads(limit=50)]
            except discord.HTTPException:
                pass
            for t in threads:
                if rule["tag_id"] not in tag_ids(t):
                    continue
                done = await self.bot.db.one("SELECT 1 FROM forum_grants WHERE guild_id=? AND thread_id=? AND tag_id=?",
                                             (guild.id, t.id, rule["tag_id"]))
                if done:
                    continue
                if await self.grant(t, rule, cfg, quiet=True) == "given":
                    given += 1
                await asyncio.sleep(0.5)  # gentle on Discord's rate limits
        if given:
            log.info("guild %s: forum roles caught up, %d role(s) given", guild.id, given)

    # ---- the log ---------------------------------------------------------------------------------

    async def tagged_by(self, thread: discord.Thread) -> str:
        """' by @Mod' from the audit log, when the bot may read it."""
        guild = thread.guild
        if not guild.me.guild_permissions.view_audit_log:
            return ""
        try:
            async for e in guild.audit_logs(limit=5, action=discord.AuditLogAction.thread_update):
                fresh = (dt.datetime.now(dt.timezone.utc) - e.created_at).total_seconds() < 30
                if e.target and e.target.id == thread.id and fresh and e.user:
                    return f" by {e.user.mention}"
        except discord.HTTPException:
            pass
        return ""

    async def note(self, guild: discord.Guild, cfg: dict, text: str) -> None:
        ch = guild.get_channel(cfg.get("log_channel_id") or 0)
        if ch is None:
            return
        try:
            await ch.send(embed=discord.Embed(description=text[:4000], colour=0xE8C560,
                                              timestamp=dt.datetime.now(dt.timezone.utc)),
                          allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("guild %s: forum-role log failed: %s", guild.id, e)
