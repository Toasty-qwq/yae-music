"""Auto-replies, the starboard and suggestions."""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from . import messages, ui
from .templates import Ctx

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.community")
STATUS = {  # status -> (word, colour)
    "open": ("Open for votes", 0xF2879F),
    "considered": ("Being considered", 0xEFBB70),
    "approved": ("Approved", 0x8FCFA3),
    "denied": ("Denied", 0xFF8A93),
    "implemented": ("Done", 0x9EC1F0),
}


def _emoji_key(e) -> str:
    s = str(e).strip()
    if s.startswith("<") and s.endswith(">"):
        return s.split(":")[-1].rstrip(">")
    return s.replace("️", "")


def matches(item: dict, text: str) -> bool:
    t, low = item["trigger"].lower(), text.lower()
    m = item["match"]
    if m == "exact":
        return low.strip() == t
    if m == "starts":
        return low.startswith(t)
    if m == "word":
        return re.search(rf"(?<!\w){re.escape(t)}(?!\w)", low) is not None
    return t in low


class Community(commands.Cog):
    suggestion = app_commands.Group(name="suggestion", description="Decide on a suggestion", guild_only=True,
                                    default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: "MusicBot"):
        self.bot = bot
        self._cooldown: dict[tuple, float] = {}

    # ---- auto-replies ------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or not message.content:
            return
        cfg = await self.bot.configs.section(message.guild.id, "autoresponders")
        if not cfg["enabled"]:
            return
        for i, item in enumerate(cfg["items"]):
            if not matches(item, message.content):
                continue
            key = (message.guild.id, i, message.channel.id)
            if time.monotonic() - self._cooldown.get(key, 0) < 10:  # no reply storms
                return
            self._cooldown[key] = time.monotonic()
            ctx = Ctx(message.guild, message.author, message.channel)
            try:
                if item.get("reply", True):
                    await message.reply(**messages.build(item["message"], ctx), mention_author=False)
                else:
                    await messages.send(message.channel, item["message"], ctx)
            except discord.HTTPException as e:
                log.warning("guild %s: auto-reply %r failed: %s", message.guild.id, item["trigger"], e)
            return  # one reply per message

    # ---- starboard -----------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, p: discord.RawReactionActionEvent):
        await self._star(p)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, p: discord.RawReactionActionEvent):
        await self._star(p)

    async def _star(self, p: discord.RawReactionActionEvent) -> None:
        if not p.guild_id:
            return
        guild = self.bot.get_guild(p.guild_id)
        cfg = await self.bot.configs.section(p.guild_id, "starboard")
        if not guild or not cfg["enabled"] or _emoji_key(p.emoji) != _emoji_key(cfg["emoji"]):
            return
        board = guild.get_channel(cfg["channel_id"] or 0)
        source = guild.get_channel(p.channel_id) or guild.get_thread(p.channel_id)
        if board is None or source is None or source.id == board.id or source.id in cfg["ignored_channel_ids"]:
            return
        if getattr(source, "is_nsfw", lambda: False)() and not board.is_nsfw():
            return  # never move NSFW posts into a safe channel
        try:
            msg = await source.fetch_message(p.message_id)
        except discord.HTTPException:
            return
        stars = 0
        for r in msg.reactions:
            if _emoji_key(r.emoji) == _emoji_key(cfg["emoji"]):
                stars = r.count
                if not cfg["self_star"] and stars:
                    try:
                        async for u in r.users(limit=100):
                            if u.id == msg.author.id:
                                stars -= 1
                                break
                    except discord.HTTPException:
                        pass
        row = await self.bot.db.one("SELECT * FROM starboard WHERE guild_id=? AND message_id=?", (guild.id, msg.id))
        if stars < cfg["threshold"]:
            if row and row.get("star_message_id"):
                try:
                    await board.get_partial_message(row["star_message_id"]).delete()
                except discord.HTTPException:
                    pass
                await self.bot.db.run("DELETE FROM starboard WHERE guild_id=? AND message_id=?", (guild.id, msg.id))
            return
        head = f"{cfg['emoji']} **{stars}** · {source.mention}"
        e = discord.Embed(description=msg.content[:4000] or None, colour=0xE8C560, timestamp=msg.created_at)
        e.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
        e.add_field(name="Original", value=f"[Jump to the message]({msg.jump_url})")
        img = next((a.url for a in msg.attachments if (a.content_type or "").startswith("image/")), None)
        if img:
            e.set_image(url=img)
        elif msg.embeds and msg.embeds[0].image:
            e.set_image(url=msg.embeds[0].image.url)
        e.set_footer(text=f"Message ID {msg.id}")
        try:
            if row and row.get("star_message_id"):
                await board.get_partial_message(row["star_message_id"]).edit(content=head, embed=e)
                await self.bot.db.run("UPDATE starboard SET stars=? WHERE guild_id=? AND message_id=?", (stars, guild.id, msg.id))
            else:
                posted = await board.send(content=head, embed=e, allowed_mentions=discord.AllowedMentions.none())
                await self.bot.db.run(
                    "INSERT OR REPLACE INTO starboard(guild_id, message_id, channel_id, star_message_id, stars) VALUES(?,?,?,?,?)",
                    (guild.id, msg.id, source.id, posted.id, stars))
        except discord.HTTPException as ex:
            log.warning("guild %s: starboard post failed: %s", guild.id, ex)

    # ---- suggestions ---------------------------------------------------------------------------

    def suggestion_embed(self, s: dict, author: discord.abc.User | None, anonymous: bool) -> discord.Embed:
        word, colour = STATUS.get(s["status"], STATUS["open"])
        e = discord.Embed(title=f"Suggestion #{s['number']}", description=s["content"][:4000], colour=colour,
                          timestamp=dt.datetime.fromtimestamp(s["created_at"], dt.timezone.utc))
        if author and not anonymous:
            e.set_author(name=author.display_name, icon_url=author.display_avatar.url)
        e.add_field(name="Status", value=word)
        if s.get("mod_id") and s["status"] != "open":
            e.add_field(name="Decided by", value=f"<@{s['mod_id']}>")
            if s.get("reason"):
                e.add_field(name="Reason", value=s["reason"][:1024], inline=False)
        e.set_footer(text="Vote with the reactions below")
        return e

    @app_commands.command(name="suggest", description="Suggest something for this server")
    @app_commands.describe(idea="Your suggestion")
    @app_commands.guild_only()
    async def suggest(self, interaction: discord.Interaction, idea: app_commands.Range[str, 5, 2000]):
        g = interaction.guild
        cfg = await self.bot.configs.section(g.id, "suggestions")
        ch = g.get_channel(cfg["channel_id"] or 0)
        if not cfg["enabled"] or ch is None:
            return await interaction.response.send_message(embed=ui.error("Suggestions aren't switched on in this server."),
                                                           ephemeral=True)
        row = await self.bot.db.one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM suggestions WHERE guild_id=?", (g.id,))
        s = {"number": row["n"], "content": idea, "status": "open", "created_at": time.time(), "mod_id": None, "reason": None}
        try:
            posted = await ch.send(embed=self.suggestion_embed(s, interaction.user, cfg["anonymous"]))
            for emoji in ("\U0001f44d", "\U0001f44e"):
                await posted.add_reaction(emoji)
        except discord.HTTPException as e:
            return await interaction.response.send_message(embed=ui.error(f"The suggestion couldn't be posted: {e.text or e}"),
                                                           ephemeral=True)
        await self.bot.db.run(
            "INSERT INTO suggestions(guild_id, number, author_id, content, channel_id, message_id, status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (g.id, s["number"], interaction.user.id, idea, ch.id, posted.id, "open", s["created_at"]))
        await interaction.response.send_message(embed=ui.ok(f"Suggestion #{s['number']} posted in {ch.mention}. [See it]({posted.jump_url})"),
                                                ephemeral=True)

    async def decide(self, interaction: discord.Interaction, number: int, status: str, reason: str) -> None:
        g = interaction.guild
        s = await self.bot.db.one("SELECT * FROM suggestions WHERE guild_id=? AND number=?", (g.id, number))
        if not s:
            return await interaction.response.send_message(embed=ui.error(f"There's no suggestion #{number}."), ephemeral=True)
        await self.bot.db.run("UPDATE suggestions SET status=?, mod_id=?, reason=? WHERE guild_id=? AND number=?",
                              (status, interaction.user.id, reason or None, g.id, number))
        s.update(status=status, mod_id=interaction.user.id, reason=reason or None)
        cfg = await self.bot.configs.section(g.id, "suggestions")
        author = g.get_member(s["author_id"]) or await self._user(s["author_id"])
        ch = g.get_channel(s["channel_id"] or 0)
        if ch and s.get("message_id"):
            try:
                await ch.get_partial_message(s["message_id"]).edit(embed=self.suggestion_embed(s, author, cfg["anonymous"]))
            except discord.HTTPException:
                pass
        word = STATUS[status][0].lower()
        if cfg["dm_on_decision"] and author:
            try:
                await author.send(f"Your suggestion #{number} in **{g.name}** is now: **{word}**."
                                  + (f"\nReason: {reason}" if reason else ""))
            except discord.HTTPException:
                pass
        await interaction.response.send_message(embed=ui.ok(f"Suggestion #{number} is now {word}."), ephemeral=True)

    async def _user(self, uid: int):
        try:
            return await self.bot.fetch_user(uid)
        except discord.HTTPException:
            return None

    @suggestion.command(name="approve", description="Approve a suggestion")
    async def approve(self, interaction: discord.Interaction, number: app_commands.Range[int, 1], reason: str = ""):
        await self.decide(interaction, number, "approved", reason)

    @suggestion.command(name="deny", description="Turn a suggestion down")
    async def deny(self, interaction: discord.Interaction, number: app_commands.Range[int, 1], reason: str = ""):
        await self.decide(interaction, number, "denied", reason)

    @suggestion.command(name="consider", description="Mark a suggestion as being considered")
    async def consider(self, interaction: discord.Interaction, number: app_commands.Range[int, 1], reason: str = ""):
        await self.decide(interaction, number, "considered", reason)

    @suggestion.command(name="implemented", description="Mark a suggestion as done")
    async def implemented(self, interaction: discord.Interaction, number: app_commands.Range[int, 1], reason: str = ""):
        await self.decide(interaction, number, "implemented", reason)
