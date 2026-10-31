"""Tickets (like Ticket Tool).

A panel is a message with one button (or dropdown option) per ticket type: "Support", "Apply to be
mod"... Each type has its own category, support roles, form questions (up to 5, asked in a pop-up),
ticket message, name pattern, per-person limit, and can make a channel or a private thread.
Panels are edited on the dashboard and the posted message is updated in place.

In a ticket: Close (asks to confirm, optionally with a reason), Claim; once closed: Reopen,
Transcript, Delete. Transcripts are HTML files of up to 1000 messages, saved to a transcript channel
and optionally sent to the ticket's owner. Tickets can close by themselves after a quiet time, or
when their owner leaves the server.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import io
import json
import logging
import re
import secrets
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import messages, ui
from .errors import ConfigError
from .templates import Ctx

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.tickets")
COLORS = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
          "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
DEFAULT_TICKET_MESSAGE = messages.message(
    "", color="#f2879f", title="Thanks for reaching out, {user.display}",
    description="Tell us what you need and someone from the team will be with you soon.\n"
                "Press **Close** when you're done.")
DEFAULT_PANEL_MESSAGE = messages.message(
    "", color="#f2879f", title="Need help?", description="Pick what it's about below to open a private ticket with the team.")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "ticket"


# ---- panel validation (what the dashboard sends) -----------------------------------------------

def validate_panel(bot: "MusicBot", guild: discord.Guild, d: dict) -> dict:
    try:
        ch = guild.get_channel(int(d.get("channel_id") or 0))
    except (TypeError, ValueError):
        ch = None
    if not isinstance(ch, discord.TextChannel):
        raise ConfigError("Pick the channel the panel goes in.")
    p = ch.permissions_for(guild.me)
    if not (p.view_channel and p.send_messages and p.embed_links):
        raise ConfigError(f"The bot can't post in #{ch.name}: it needs View Channel, Send Messages and Embed Links.")
    types = []
    for i, t in enumerate(d.get("types") or [], start=1):
        label = str(t.get("label") or "").strip()[:80]
        if not label:
            raise ConfigError(f"Ticket type {i} needs a name, like Support.")
        mode = "thread" if t.get("mode") == "thread" else "channel"
        cat = thread_parent = None
        if mode == "channel":
            try:
                cat = guild.get_channel(int(t.get("category_id") or 0))
            except (TypeError, ValueError):
                cat = None
            if not isinstance(cat, discord.CategoryChannel):
                raise ConfigError(f"{label}: pick the category new tickets go in.")
            cp = cat.permissions_for(guild.me)
            if not (cp.manage_channels and cp.manage_roles):
                raise ConfigError(f"{label}: the bot needs Manage Channels and Manage Permissions in the {cat.name} category.")
        else:
            try:
                thread_parent = guild.get_channel(int(t.get("thread_channel_id") or 0))
            except (TypeError, ValueError):
                thread_parent = None
            if not isinstance(thread_parent, discord.TextChannel):
                raise ConfigError(f"{label}: pick the channel the ticket threads are made in.")
            tp = thread_parent.permissions_for(guild.me)
            if not (tp.create_private_threads and tp.send_messages_in_threads and tp.manage_threads):
                raise ConfigError(f"{label}: in #{thread_parent.name} the bot needs Create Private Threads, "
                                  "Send Messages in Threads and Manage Threads.")
        closed = None
        if t.get("closed_category_id"):
            try:
                closed = guild.get_channel(int(t["closed_category_id"]))
            except (TypeError, ValueError):
                closed = None
            if not isinstance(closed, discord.CategoryChannel):
                closed = None
        roles = []
        for r in t.get("support_role_ids") or []:
            try:
                role = guild.get_role(int(r))
            except (TypeError, ValueError):
                role = None
            if role and not role.is_default():
                roles.append(role.id)
        emoji = str(t.get("emoji") or "").strip()
        if emoji and discord.PartialEmoji.from_str(emoji).id is None and re.search(r"[A-Za-z]", emoji):
            raise ConfigError(f"{label}: {emoji!r} isn't an emoji. Paste the emoji itself.")
        questions = []
        for q in (t.get("questions") or [])[:5]:
            ql = str(q.get("label") or "").strip()[:45]
            if not ql:
                continue
            try:
                lo = max(0, min(4000, int(q.get("min") or 0)))
                hi = max(1, min(4000, int(q.get("max") or 1000)))
            except (TypeError, ValueError):
                lo, hi = 0, 1000
            questions.append({"label": ql, "placeholder": str(q.get("placeholder") or "")[:100],
                              "required": bool(q.get("required", True)), "long": bool(q.get("long")),
                              "min": min(lo, hi), "max": hi})
        try:
            limit = max(0, min(10, int(t.get("limit_per_user") if t.get("limit_per_user") not in (None, "") else 1)))
        except (TypeError, ValueError):
            limit = 1
        types.append({
            "id": re.sub(r"[^a-z0-9]", "", str(t.get("id") or "").lower())[:10] or secrets.token_hex(3),
            "label": label, "emoji": emoji, "description": str(t.get("description") or "").strip()[:100],
            "color": t.get("color") if t.get("color") in COLORS else "secondary",
            "mode": mode, "category_id": cat.id if cat else None,
            "thread_channel_id": thread_parent.id if thread_parent else None,
            "closed_category_id": closed.id if closed else None, "support_role_ids": roles,
            "name_template": (str(t.get("name_template") or "").strip()[:80] or "{type}-{number}"),
            "ticket_message": messages.validate(t.get("ticket_message") or DEFAULT_TICKET_MESSAGE, f"{label}'s ticket message"),
            "questions": questions, "limit_per_user": limit, "ping_support": bool(t.get("ping_support", True)),
        })
    if not types:
        raise ConfigError("Add at least one ticket type (like Support).")
    if len(types) > 25:
        raise ConfigError("A panel holds up to 25 ticket types.")
    if len({t["id"] for t in types}) != len(types):
        raise ConfigError("Two ticket types have the same id. Remove one and add it again.")
    return {"name": str(d.get("name") or "").strip()[:80] or "Tickets", "channel_id": ch.id,
            "style": "dropdown" if d.get("style") == "dropdown" else "buttons",
            "placeholder": str(d.get("placeholder") or "").strip()[:150] or "Pick what it's about",
            "message": messages.validate(d.get("message") or DEFAULT_PANEL_MESSAGE, "The panel message"),
            "types": types}


def panel_view(panel_id: int, data: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if data["style"] == "dropdown":
        view.add_item(discord.ui.Select(custom_id=f"tk:s:{panel_id}", placeholder=data["placeholder"], options=[
            discord.SelectOption(label=t["label"], value=t["id"], description=t["description"] or None,
                                 emoji=t["emoji"] or None) for t in data["types"]]))
    else:
        for t in data["types"]:
            view.add_item(discord.ui.Button(custom_id=f"tk:o:{panel_id}:{t['id']}", label=t["label"],
                                            emoji=t["emoji"] or None, style=COLORS[t["color"]]))
    return view


def ticket_buttons(claiming: bool, claimed: bool = False) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(custom_id="tk:close", label="Close", style=discord.ButtonStyle.danger))
    if claiming:
        v.add_item(discord.ui.Button(custom_id="tk:claim", label="Unclaim" if claimed else "Claim",
                                     style=discord.ButtonStyle.secondary))
    return v


def closed_buttons() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(custom_id="tk:reopen", label="Reopen", style=discord.ButtonStyle.success))
    v.add_item(discord.ui.Button(custom_id="tk:transcript", label="Transcript", style=discord.ButtonStyle.secondary))
    v.add_item(discord.ui.Button(custom_id="tk:delete", label="Delete", style=discord.ButtonStyle.danger))
    return v


class TicketError(Exception):
    """A message for the person who tried."""


class Tickets(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Work with the ticket you're in", guild_only=True)

    def __init__(self, bot: "MusicBot"):
        self.bot = bot
        self.panels: dict[int, dict] = {}
        self.channels: set[int] = set()            # open/closed ticket channel ids (fast on_message check)
        self._seen: dict[int, float] = {}            # channel id -> last time activity was written
        self._renames: dict[int, list[float]] = defaultdict(list)
        self._creating: set[tuple[int, int]] = set()

    async def cog_load(self) -> None:
        for p in await self.bot.db.all("SELECT * FROM ticket_panels"):
            p["data"] = json.loads(p["data"])
            self.panels[p["id"]] = p
        for t in await self.bot.db.all("SELECT channel_id FROM tickets WHERE status!='deleted'"):
            self.channels.add(t["channel_id"])
        self.housekeeping.start()

    async def cog_unload(self) -> None:
        self.housekeeping.cancel()

    # ---- helpers ---------------------------------------------------------------------------------

    async def settings(self, guild_id: int) -> dict:
        return await self.bot.configs.section(guild_id, "tickets")

    def type_of(self, t: dict) -> dict | None:
        p = self.panels.get(t.get("panel_id") or 0)
        return next((x for x in p["data"]["types"] if x["id"] == t.get("type_id")), None) if p else None

    async def ticket_here(self, channel) -> dict:
        t = await self.bot.db.one("SELECT * FROM tickets WHERE channel_id=? AND status!='deleted'", (channel.id,))
        if not t:
            raise TicketError("This isn't a ticket.")
        return t

    def is_support(self, member: discord.Member, ttype: dict | None) -> bool:
        if member.guild_permissions.manage_channels or member.guild_permissions.administrator:
            return True
        roles = set((ttype or {}).get("support_role_ids") or [])
        return any(r.id in roles for r in member.roles)

    async def log(self, guild: discord.Guild, text: str, colour: int = ui.COLOR, file: discord.File | None = None) -> None:
        cfg = await self.settings(guild.id)
        ch = guild.get_channel(cfg.get("log_channel_id") or 0)
        if ch:
            try:
                await ch.send(embed=discord.Embed(description=text[:4000], colour=colour,
                                                  timestamp=dt.datetime.now(dt.timezone.utc)),
                              allowed_mentions=discord.AllowedMentions.none(), file=file)
            except discord.HTTPException:
                pass

    def channel(self, guild: discord.Guild, channel_id: int):
        return guild.get_channel(channel_id) or guild.get_thread(channel_id)

    async def reply(self, interaction: discord.Interaction, text: str, *, error: bool = False, **kw) -> None:
        embed = ui.error(text) if error else ui.ok(text)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True, **kw)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True, **kw)

    # ---- panels (dashboard) -------------------------------------------------------------------------

    async def save_panel(self, guild: discord.Guild, body: dict, panel_id: int | None = None) -> dict:
        data = validate_panel(self.bot, guild, body)
        old = self.panels.get(panel_id) if panel_id else None
        if panel_id and (not old or old["guild_id"] != guild.id):
            raise ConfigError("That panel doesn't exist any more.")
        if not old:
            panel_id = await self.bot.db.run("INSERT INTO ticket_panels(guild_id, channel_id, data, created_at) VALUES(?,?,?,?)",
                                             (guild.id, data["channel_id"], json.dumps(data), time.time()))
        ch = guild.get_channel(data["channel_id"])
        kw = messages.build(data["message"], Ctx(guild, None, ch))
        files = kw.pop("files", [])
        view = panel_view(panel_id, data)
        msg = None
        if old and old.get("message_id"):
            old_ch = guild.get_channel(old["channel_id"])
            if old_ch and old_ch.id == ch.id:
                try:
                    msg = await ch.fetch_message(old["message_id"])
                    await msg.edit(**kw, attachments=files, view=view)
                except discord.NotFound:
                    msg = None
            elif old_ch:
                try:
                    await old_ch.get_partial_message(old["message_id"]).delete()
                except discord.HTTPException:
                    pass
        try:
            if msg is None:
                msg = await ch.send(**kw, files=files, view=view)
        except discord.HTTPException as e:
            if not old:
                await self.bot.db.run("DELETE FROM ticket_panels WHERE id=?", (panel_id,))
            raise ConfigError(f"Discord refused the panel: {e.text or e}") from e
        await self.bot.db.run("UPDATE ticket_panels SET channel_id=?, message_id=?, data=? WHERE id=?",
                              (ch.id, msg.id, json.dumps(data), panel_id))
        row = {"id": panel_id, "guild_id": guild.id, "channel_id": ch.id, "message_id": msg.id, "data": data}
        self.panels[panel_id] = row
        log.info("guild %s: ticket panel %s saved (%d types)", guild.id, panel_id, len(data["types"]))
        return row

    async def delete_panel(self, guild: discord.Guild, panel_id: int) -> None:
        p = self.panels.pop(panel_id, None)
        if not p or p["guild_id"] != guild.id:
            raise ConfigError("That panel doesn't exist any more.")
        ch = guild.get_channel(p["channel_id"])
        if ch and p.get("message_id"):
            try:
                await ch.get_partial_message(p["message_id"]).delete()
            except discord.HTTPException:
                pass
        await self.bot.db.run("DELETE FROM ticket_panels WHERE id=?", (panel_id,))

    # ---- opening -------------------------------------------------------------------------------------

    async def open_checks(self, member: discord.Member, panel_id: int, type_id: str) -> tuple[dict, dict]:
        p = self.panels.get(panel_id)
        ttype = next((t for t in p["data"]["types"] if t["id"] == type_id), None) if p else None
        if not ttype:
            raise TicketError("This ticket option was removed. Ask the team to update the panel.")
        cfg = await self.settings(member.guild.id)
        mine = await self.bot.db.all("SELECT * FROM tickets WHERE guild_id=? AND owner_id=? AND status='open'",
                                     (member.guild.id, member.id))
        same = [t for t in mine if t["type_id"] == type_id and t["panel_id"] == panel_id]
        if ttype["limit_per_user"] and len(same) >= ttype["limit_per_user"]:
            where = " ".join(f"<#{t['channel_id']}>" for t in same[:3])
            raise TicketError(f"You already have an open {ttype['label']} ticket: {where}")
        if cfg["global_limit"] and len(mine) >= cfg["global_limit"]:
            raise TicketError(f"You have {len(mine)} open tickets. Close one before opening another.")
        return p, ttype

    async def create(self, member: discord.Member, panel: dict, ttype: dict, answers: list[tuple[str, str]]) -> discord.abc.GuildChannel:
        guild = member.guild
        cfg = await self.settings(guild.id)
        key = (guild.id, member.id)
        if key in self._creating:
            raise TicketError("Your ticket is already being made.")
        self._creating.add(key)
        try:
            row = await self.bot.db.one("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM tickets WHERE guild_id=?", (guild.id,))
            number = row["n"]
            name = (ttype["name_template"].replace("{type}", slug(ttype["label"])).replace("{number}", f"{number:04d}")
                    .replace("{user}", slug(member.name)).replace("{id}", str(member.id)))[:100]
            support = [r for r in (guild.get_role(i) for i in ttype["support_role_ids"]) if r]
            if ttype["mode"] == "thread":
                parent = guild.get_channel(ttype["thread_channel_id"])
                if parent is None:
                    raise TicketError("The ticket channel was deleted. Ask the team to update the panel.")
                ch = await parent.create_thread(name=name, type=discord.ChannelType.private_thread, invitable=False,
                                                reason=f"Ticket {number} for {member}")
                await ch.add_user(member)
            else:
                category = guild.get_channel(ttype["category_id"])
                ows = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                       guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                             manage_channels=True, manage_messages=True, embed_links=True,
                                                             attach_files=True),
                       member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                           attach_files=True, embed_links=True)}
                for r in support:
                    ows[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                         attach_files=True, embed_links=True, manage_messages=True)
                ch = await guild.create_text_channel(name, category=category, overwrites=ows,
                                                     topic=f"Ticket {number} · {ttype['label']} · opened by {member} ({member.id})",
                                                     reason=f"Ticket {number} for {member}")
        except discord.HTTPException as e:
            raise TicketError(f"The ticket couldn't be made: {e.text or e}") from e
        finally:
            self._creating.discard(key)
        now = time.time()
        await self.bot.db.run(
            "INSERT INTO tickets(guild_id, number, channel_id, owner_id, panel_id, type_id, type_label, status, answers, "
            "created_at, last_activity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (guild.id, number, ch.id, member.id, panel["id"], ttype["id"], ttype["label"], "open",
             json.dumps(answers), now, now))
        self.channels.add(ch.id)
        kw = messages.build(ttype["ticket_message"], Ctx(guild, member, ch))
        mentions = [member.mention] + ([r.mention for r in support] if ttype["ping_support"] else [])
        kw["content"] = " ".join(mentions) + ("\n" + kw["content"] if kw.get("content") and kw["content"] != "​" else "")
        kw["allowed_mentions"] = discord.AllowedMentions(everyone=False, users=True, roles=support)
        embeds = [kw.pop("embed")] if kw.get("embed") else []
        if answers:
            a = discord.Embed(title="Their answers", colour=ui.COLOR)
            for q, ans in answers:
                a.add_field(name=q[:256], value=(ans or "*no answer*")[:1024], inline=False)
            embeds.append(a)
        try:
            first = await ch.send(**kw, embeds=embeds, view=ticket_buttons(cfg["claiming"]))
            await first.pin()
        except discord.HTTPException as e:
            log.warning("guild %s: ticket message failed: %s", guild.id, e)
        await self.log(guild, f"**Ticket {number} opened** ({ttype['label']}) by {member.mention}: {ch.mention}", 0x8FCFA3)
        log.info("guild %s: ticket %s opened by %s (%s)", guild.id, number, member, ttype["label"])
        return ch

    # ---- the ticket's life ----------------------------------------------------------------------------

    async def close(self, t: dict, by: discord.abc.User, reason: str | None = None) -> None:
        guild = self.bot.get_guild(t["guild_id"])
        ch = self.channel(guild, t["channel_id"]) if guild else None
        if not guild or not ch:
            await self.bot.db.run("UPDATE tickets SET status='deleted' WHERE channel_id=?", (t["channel_id"],))
            return
        if t["status"] != "open":
            raise TicketError("This ticket is already closed.")
        cfg = await self.settings(guild.id)
        ttype = self.type_of(t)
        await self.bot.db.run("UPDATE tickets SET status='closed', closed_at=?, close_reason=? WHERE channel_id=?",
                              (time.time(), reason, ch.id))
        t.update(status="closed", closed_at=time.time(), close_reason=reason)
        owner = guild.get_member(t["owner_id"])
        if cfg["save_transcript"] or cfg["dm_transcript"]:
            await self.save_transcript(ch, t, to_owner=cfg["dm_transcript"], to_channel=cfg["save_transcript"])
        if not cfg["keep_closed"]:
            await self._delete(ch, t, by, closing=True, reason=reason)
            return
        try:
            if isinstance(ch, discord.Thread):
                await ch.send(embed=self.closed_embed(by, reason), view=closed_buttons())
                if owner:
                    try:
                        await ch.remove_user(owner)
                    except discord.HTTPException:
                        pass
            else:
                if owner:
                    ow = ch.overwrites_for(owner)
                    ow.send_messages = False
                    await ch.set_permissions(owner, overwrite=ow, reason="Ticket closed")
                closed_cat = guild.get_channel((ttype or {}).get("closed_category_id") or 0)
                if closed_cat:
                    await ch.edit(category=closed_cat, sync_permissions=False, reason="Ticket closed")
                await ch.send(embed=self.closed_embed(by, reason), view=closed_buttons())
        except discord.HTTPException as e:
            log.warning("guild %s: closing ticket %s: %s", guild.id, t["number"], e)
        await self.log(guild, f"**Ticket {t['number']} closed** by {by.mention}" + (f": {reason}" if reason else ""), 0xEFBB70)

    @staticmethod
    def closed_embed(by: discord.abc.User, reason: str | None) -> discord.Embed:
        e = discord.Embed(title="Ticket closed", description=f"Closed by {by.mention}." + (f"\n**Reason:** {reason}" if reason else ""),
                          colour=0xEFBB70)
        e.set_footer(text="The team can reopen it, save a transcript, or delete it.")
        return e

    async def reopen(self, t: dict, by: discord.abc.User) -> None:
        guild = self.bot.get_guild(t["guild_id"])
        ch = self.channel(guild, t["channel_id"])
        if t["status"] != "closed":
            raise TicketError("This ticket isn't closed.")
        ttype = self.type_of(t)
        owner = guild.get_member(t["owner_id"])
        await self.bot.db.run("UPDATE tickets SET status='open', closed_at=NULL, close_reason=NULL, last_activity=? "
                              "WHERE channel_id=?", (time.time(), ch.id))
        if isinstance(ch, discord.Thread):
            await ch.edit(archived=False, locked=False)
            if owner:
                await ch.add_user(owner)
        else:
            if owner:
                ow = ch.overwrites_for(owner)
                ow.send_messages = True
                ow.view_channel = True
                await ch.set_permissions(owner, overwrite=ow, reason="Ticket reopened")
            open_cat = guild.get_channel((ttype or {}).get("category_id") or 0)
            if open_cat and ch.category_id != open_cat.id:
                await ch.edit(category=open_cat, sync_permissions=False, reason="Ticket reopened")
        await ch.send(embed=discord.Embed(description=f"Reopened by {by.mention}.", colour=0x8FCFA3))
        await self.log(guild, f"**Ticket {t['number']} reopened** by {by.mention}: {ch.mention}", 0x8FCFA3)

    async def _delete(self, ch, t: dict, by: discord.abc.User, *, closing: bool = False, reason: str | None = None) -> None:
        guild = ch.guild
        try:
            await ch.send(embed=discord.Embed(description="This ticket will be deleted in 5 seconds.", colour=0xFF8A93))
        except discord.HTTPException:
            pass
        await asyncio.sleep(5)
        try:
            await ch.delete(reason=f"Ticket {t['number']} deleted by {by}")
        except discord.HTTPException as e:
            log.warning("guild %s: deleting ticket %s: %s", guild.id, t["number"], e)
        await self.bot.db.run("UPDATE tickets SET status='deleted' WHERE channel_id=?", (ch.id,))
        self.channels.discard(ch.id)
        await self.log(guild, f"**Ticket {t['number']} {'closed and ' if closing else ''}deleted** by {by.mention}"
                              + (f": {reason}" if reason else ""), 0xFF8A93)

    async def delete(self, t: dict, by: discord.Member) -> None:
        ch = self.channel(by.guild, t["channel_id"])
        cfg = await self.settings(by.guild.id)
        if t["status"] == "open" and cfg["save_transcript"]:
            await self.save_transcript(ch, t, to_owner=False, to_channel=True)
        await self._delete(ch, t, by)

    async def claim(self, t: dict, member: discord.Member) -> str:
        ttype = self.type_of(t)
        if not self.is_support(member, ttype):
            raise TicketError("Only the team can claim tickets.")
        ch = self.channel(member.guild, t["channel_id"])
        if t["claimed_by"] == member.id:
            await self.bot.db.run("UPDATE tickets SET claimed_by=NULL WHERE channel_id=?", (ch.id,))
            await ch.send(embed=discord.Embed(description=f"{member.mention} unclaimed this ticket.", colour=ui.COLOR))
            return "Unclaimed."
        if t["claimed_by"] and not member.guild_permissions.manage_guild:
            raise TicketError(f"<@{t['claimed_by']}> already claimed this ticket.")
        await self.bot.db.run("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (member.id, ch.id))
        await ch.send(embed=discord.Embed(description=f"{member.mention} will handle this ticket.", colour=0x9EC1F0))
        await self.log(member.guild, f"**Ticket {t['number']} claimed** by {member.mention}: {ch.mention}", 0x9EC1F0)
        return "Claimed: it's yours."

    # ---- transcripts ---------------------------------------------------------------------------------

    async def transcript_file(self, ch, t: dict) -> discord.File:
        msgs = [m async for m in ch.history(limit=1000, oldest_first=True)]
        guild = ch.guild
        esc = html.escape

        def body(m: discord.Message) -> str:
            parts = []
            if m.content:
                parts.append(f'<div class="c">{esc(m.clean_content).replace(chr(10), "<br>")}</div>')
            for e in m.embeds:
                bits = "".join(f"<b>{esc(x)}</b><br>" for x in [e.title] if x) + \
                       (esc(e.description or "").replace(chr(10), "<br>")) + \
                       "".join(f'<div class="f"><b>{esc(f.name)}</b><br>{esc(f.value).replace(chr(10), "<br>")}</div>' for f in e.fields)
                parts.append(f'<div class="e">{bits}</div>')
            for a in m.attachments:
                if (a.content_type or "").startswith("image/"):
                    parts.append(f'<a href="{esc(a.url)}"><img src="{esc(a.url)}" alt="{esc(a.filename)}"></a>')
                else:
                    parts.append(f'<a href="{esc(a.url)}">{esc(a.filename)}</a>')
            return "".join(parts)

        rows = "".join(
            f'<div class="m"><img class="a" src="{esc(m.author.display_avatar.url)}" alt=""><div><div class="h">'
            f'<b>{esc(m.author.display_name)}</b>{" <span class=bot>BOT</span>" if m.author.bot else ""}'
            f' <span class="t">{m.created_at.strftime("%Y-%m-%d %H:%M UTC")}</span></div>{body(m)}</div></div>'
            for m in msgs)
        opened = dt.datetime.fromtimestamp(t["created_at"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ticket {t['number']} · {esc(guild.name)}</title><style>
body{{margin:0;background:#15101a;color:#f2e8ec;font:15px/1.5 system-ui,sans-serif}}
header{{padding:20px 24px;border-bottom:3px solid #c98fa2;background:#221a29}}h1{{margin:0 0 4px;font-size:20px}}
.meta{{color:#bfaeba;font-size:13px}}main{{padding:12px 24px;max-width:900px}}
.m{{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid #2b2132}}.a{{width:40px;height:40px;border-radius:50%}}
.h b{{color:#f2879f}}.t{{color:#a492a2;font-size:12px}}.bot{{background:#9ec1f0;color:#15101a;font-size:10px;padding:1px 4px;border-radius:3px}}
.c{{white-space:normal;overflow-wrap:anywhere}}.e{{border-left:4px solid #f2879f;background:#281f30;padding:8px 10px;margin:4px 0;border-radius:4px}}
.f{{margin-top:6px}}img:not(.a){{max-width:360px;border-radius:4px;display:block;margin-top:4px}}a{{color:#9ec1f0}}
</style></head><body><header><h1>Ticket {t['number']} · {esc(t.get('type_label') or '')}</h1>
<div class="meta">{esc(guild.name)} · #{esc(ch.name)} · opened by {esc(str(guild.get_member(t['owner_id']) or t['owner_id']))} on {opened}
 · {len(msgs)} messages{' · reason: ' + esc(t['close_reason']) if t.get('close_reason') else ''}</div></header>
<main>{rows or '<p>No messages.</p>'}</main></body></html>"""
        return discord.File(io.BytesIO(page.encode("utf-8")), filename=f"ticket-{t['number']}-{slug(ch.name)}.html")

    async def save_transcript(self, ch, t: dict, *, to_owner: bool, to_channel: bool) -> bool:
        cfg = await self.settings(ch.guild.id)
        try:
            dest = ch.guild.get_channel(cfg.get("transcript_channel_id") or 0) if to_channel else None
            summary = discord.Embed(title=f"Transcript: ticket {t['number']}", colour=ui.COLOR,
                                    description=f"{t.get('type_label') or 'Ticket'} · opened by <@{t['owner_id']}>"
                                                + (f"\n**Closed:** {t['close_reason']}" if t.get("close_reason") else ""))
            if dest:
                await dest.send(embed=summary, file=await self.transcript_file(ch, t), allowed_mentions=discord.AllowedMentions.none())
            if to_owner:
                owner = ch.guild.get_member(t["owner_id"])
                if owner:
                    try:
                        await owner.send(content=f"Here's a copy of your ticket in **{ch.guild.name}**.",
                                         file=await self.transcript_file(ch, t))
                    except discord.HTTPException:
                        pass
            return bool(dest)
        except discord.HTTPException as e:
            log.warning("guild %s: transcript failed: %s", ch.guild.id, e)
            return False

    # ---- buttons and dropdowns ----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("tk:") or not isinstance(interaction.user, discord.Member):
            return
        member = interaction.user
        try:
            if cid.startswith(("tk:o:", "tk:s:")):
                parts = cid.split(":")
                panel_id = int(parts[2])
                type_id = parts[3] if cid.startswith("tk:o:") else ((interaction.data or {}).get("values") or [""])[0]
                panel, ttype = await self.open_checks(member, panel_id, type_id)
                if ttype["questions"]:
                    return await interaction.response.send_modal(FormModal(self, member, panel, ttype))
                await interaction.response.defer(ephemeral=True, thinking=True)
                ch = await self.create(member, panel, ttype, [])
                return await interaction.followup.send(embed=ui.ok(f"Your ticket is open: {ch.mention}"), ephemeral=True)
            t = await self.ticket_here(interaction.channel)
            ttype = self.type_of(t)
            if cid == "tk:close":
                if t["status"] != "open":
                    raise TicketError("This ticket is already closed.")
                if member.id != t["owner_id"] and not self.is_support(member, ttype):
                    raise TicketError("Only the ticket's owner or the team can close it.")
                cfg = await self.settings(member.guild.id)
                if cfg["two_step_close"]:
                    v = discord.ui.View(timeout=60)
                    v.add_item(discord.ui.Button(custom_id="tk:cy", label="Close it", style=discord.ButtonStyle.danger))
                    v.add_item(discord.ui.Button(custom_id="tk:cr", label="Close with a reason", style=discord.ButtonStyle.secondary))
                    return await interaction.response.send_message(embed=ui.ok("Close this ticket?"), view=v, ephemeral=True)
                await interaction.response.defer()
                return await self.close(t, member)
            if cid in ("tk:cy", "tk:rqy"):
                if cid == "tk:rqy" and member.id != t["owner_id"]:
                    raise TicketError("Only the ticket's owner can answer this.")
                await interaction.response.defer()
                return await self.close(t, member, t.get("_reason"))
            if cid == "tk:rqn":
                if member.id != t["owner_id"]:
                    raise TicketError("Only the ticket's owner can answer this.")
                await interaction.message.edit(view=None)
                return await interaction.response.send_message(embed=ui.ok(f"{member.mention} wants to keep the ticket open."))
            if cid == "tk:cr":
                return await interaction.response.send_modal(ReasonModal(self, t))
            if cid == "tk:claim":
                return await self.reply(interaction, await self.claim(t, member))
            if not self.is_support(member, ttype):
                raise TicketError("Only the team can do that.")
            if cid == "tk:reopen":
                await interaction.response.defer()
                return await self.reopen(t, member)
            if cid == "tk:delete":
                await interaction.response.defer()
                return await self.delete(t, member)
            if cid == "tk:transcript":
                await interaction.response.defer(ephemeral=True, thinking=True)
                saved = await self.save_transcript(interaction.channel, t, to_owner=False, to_channel=True)
                if saved:
                    return await interaction.followup.send(embed=ui.ok("Transcript saved to the transcript channel."), ephemeral=True)
                return await interaction.followup.send(embed=ui.ok("No transcript channel is set, so here it is:"),
                                                       file=await self.transcript_file(interaction.channel, t), ephemeral=True)
        except TicketError as e:
            await self.reply(interaction, str(e), error=True)
        except discord.HTTPException as e:
            await self.reply(interaction, f"Discord refused: {e.text or e}", error=True)

    # ---- /ticket -------------------------------------------------------------------------------------

    async def _cmd(self, interaction: discord.Interaction, fn) -> None:
        try:
            t = await self.ticket_here(interaction.channel)
            await fn(t, self.type_of(t))
        except TicketError as e:
            await self.reply(interaction, str(e), error=True)
        except discord.HTTPException as e:
            await self.reply(interaction, f"Discord refused: {e.text or e}", error=True)

    def _team(self, interaction, ttype):
        if not self.is_support(interaction.user, ttype):
            raise TicketError("Only the team can do that.")

    @ticket.command(name="close", description="Close this ticket")
    async def t_close(self, interaction: discord.Interaction, reason: app_commands.Range[str, 0, 500] = ""):
        async def go(t, ttype):
            if interaction.user.id != t["owner_id"]:
                self._team(interaction, ttype)
            await interaction.response.send_message(embed=ui.ok("Closing."), ephemeral=True)
            await self.close(t, interaction.user, reason or None)
        await self._cmd(interaction, go)

    @ticket.command(name="closerequest", description="Ask the ticket's owner if it can be closed")
    async def t_closerequest(self, interaction: discord.Interaction, reason: app_commands.Range[str, 0, 500] = ""):
        async def go(t, ttype):
            self._team(interaction, ttype)
            v = discord.ui.View(timeout=None)
            v.add_item(discord.ui.Button(custom_id="tk:rqy", label="Close it", style=discord.ButtonStyle.danger))
            v.add_item(discord.ui.Button(custom_id="tk:rqn", label="Keep it open", style=discord.ButtonStyle.secondary))
            await interaction.response.send_message(
                content=f"<@{t['owner_id']}>",
                embed=discord.Embed(description=f"{interaction.user.mention} thinks this ticket can be closed."
                                                + (f"\n**Why:** {reason}" if reason else "") + "\nCan it?", colour=ui.COLOR),
                view=v, allowed_mentions=discord.AllowedMentions(users=True))
        await self._cmd(interaction, go)

    @ticket.command(name="reopen", description="Reopen this closed ticket")
    async def t_reopen(self, interaction: discord.Interaction):
        async def go(t, ttype):
            self._team(interaction, ttype)
            await interaction.response.send_message(embed=ui.ok("Reopening."), ephemeral=True)
            await self.reopen(t, interaction.user)
        await self._cmd(interaction, go)

    @ticket.command(name="delete", description="Delete this ticket")
    async def t_delete(self, interaction: discord.Interaction):
        async def go(t, ttype):
            self._team(interaction, ttype)
            await interaction.response.send_message(embed=ui.ok("Deleting."), ephemeral=True)
            await self.delete(t, interaction.user)
        await self._cmd(interaction, go)

    @ticket.command(name="claim", description="Claim this ticket (or unclaim it)")
    async def t_claim(self, interaction: discord.Interaction):
        async def go(t, ttype):
            await self.reply(interaction, await self.claim(t, interaction.user))
        await self._cmd(interaction, go)

    @ticket.command(name="add", description="Let a person or role into this ticket")
    async def t_add(self, interaction: discord.Interaction, who: discord.Member | discord.Role):
        async def go(t, ttype):
            self._team(interaction, ttype)
            ch = interaction.channel
            if isinstance(ch, discord.Thread):
                if not isinstance(who, discord.Member):
                    raise TicketError("Thread tickets take people, not roles.")
                await ch.add_user(who)
            else:
                ow = ch.overwrites_for(who)
                ow.view_channel = ow.send_messages = ow.read_message_history = True
                await ch.set_permissions(who, overwrite=ow, reason=f"Added by {interaction.user}")
            await interaction.response.send_message(embed=ui.ok(f"{who.mention} was added."))
        await self._cmd(interaction, go)

    @ticket.command(name="remove", description="Take a person or role out of this ticket")
    async def t_remove(self, interaction: discord.Interaction, who: discord.Member | discord.Role):
        async def go(t, ttype):
            self._team(interaction, ttype)
            if isinstance(who, discord.Member) and who.id == t["owner_id"]:
                raise TicketError("That's the ticket's owner. Close the ticket instead.")
            ch = interaction.channel
            if isinstance(ch, discord.Thread):
                if not isinstance(who, discord.Member):
                    raise TicketError("Thread tickets take people, not roles.")
                await ch.remove_user(who)
            else:
                await ch.set_permissions(who, overwrite=None, reason=f"Removed by {interaction.user}")
            await interaction.response.send_message(embed=ui.ok(f"{who.mention} was removed."))
        await self._cmd(interaction, go)

    @ticket.command(name="rename", description="Rename this ticket")
    async def t_rename(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100]):
        async def go(t, ttype):
            self._team(interaction, ttype)
            now = time.monotonic()
            recent = [x for x in self._renames[t["channel_id"]] if now - x < 600]
            if len(recent) >= 2:
                raise TicketError("Discord allows two renames per channel every 10 minutes. Try again a bit later.")
            await interaction.channel.edit(name=name)
            self._renames[t["channel_id"]] = recent + [now]
            await interaction.response.send_message(embed=ui.ok(f"Renamed to **{discord.utils.escape_markdown(name)}**."))
        await self._cmd(interaction, go)

    @ticket.command(name="transcript", description="Save a transcript of this ticket")
    async def t_transcript(self, interaction: discord.Interaction):
        async def go(t, ttype):
            self._team(interaction, ttype)
            await interaction.response.defer(ephemeral=True, thinking=True)
            if await self.save_transcript(interaction.channel, t, to_owner=False, to_channel=True):
                return await interaction.followup.send(embed=ui.ok("Transcript saved to the transcript channel."), ephemeral=True)
            await interaction.followup.send(embed=ui.ok("No transcript channel is set, so here it is:"),
                                            file=await self.transcript_file(interaction.channel, t), ephemeral=True)
        await self._cmd(interaction, go)

    @ticket.command(name="panel", description="Post a ticket panel again here (make panels on the dashboard)")
    @app_commands.default_permissions(manage_guild=True)
    async def t_panel(self, interaction: discord.Interaction, panel_id: int):
        p = self.panels.get(panel_id)
        if not p or p["guild_id"] != interaction.guild_id:
            return await self.reply(interaction, f"There's no panel {panel_id}. The dashboard's Tickets tab lists them.", error=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await self.save_panel(interaction.guild, {**p["data"], "channel_id": interaction.channel_id}, panel_id)
        except ConfigError as e:
            return await interaction.followup.send(embed=ui.error(str(e)), ephemeral=True)
        await interaction.followup.send(embed=ui.ok("Panel posted here."), ephemeral=True)

    # ---- activity, auto-close, owners leaving -------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id not in self.channels or message.author.bot:
            return
        now = time.time()
        if now - self._seen.get(message.channel.id, 0) > 60:
            self._seen[message.channel.id] = now
            await self.bot.db.run("UPDATE tickets SET last_activity=? WHERE channel_id=?", (now, message.channel.id))

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        cfg = await self.settings(payload.guild_id)
        if not cfg["close_when_owner_leaves"]:
            return
        for t in await self.bot.db.all("SELECT * FROM tickets WHERE guild_id=? AND owner_id=? AND status='open'",
                                       (payload.guild_id, payload.user.id)):
            try:
                await self.close(t, self.bot.user, "The ticket's owner left the server")
            except (TicketError, discord.HTTPException):
                pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        if channel.id in self.channels:
            self.channels.discard(channel.id)
            await self.bot.db.run("UPDATE tickets SET status='deleted' WHERE channel_id=?", (channel.id,))

    @tasks.loop(minutes=10)
    async def housekeeping(self):
        now = time.time()
        for t in await self.bot.db.all("SELECT * FROM tickets WHERE status='open'"):
            cfg = await self.settings(t["guild_id"])
            hours = cfg["auto_close_hours"]
            if hours and now - t["last_activity"] > hours * 3600:
                try:
                    await self.close(t, self.bot.user, f"No messages for {hours} hours")
                except (TicketError, discord.HTTPException):
                    pass

    @housekeeping.before_loop
    async def _ready(self):
        await self.bot.wait_until_ready()


class FormModal(discord.ui.Modal):
    def __init__(self, cog: Tickets, member: discord.Member, panel: dict, ttype: dict):
        super().__init__(title=ttype["label"][:45], timeout=900)
        self.cog, self.member, self.panel, self.ttype = cog, member, panel, ttype
        self.inputs = []
        for q in ttype["questions"][:5]:
            ti = discord.ui.TextInput(label=q["label"][:45], placeholder=q["placeholder"] or None, required=q["required"],
                                      style=discord.TextStyle.paragraph if q["long"] else discord.TextStyle.short,
                                      min_length=q["min"] or None, max_length=q["max"])
            self.inputs.append((q["label"], ti))
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ch = await self.cog.create(self.member, self.panel, self.ttype, [(q, ti.value) for q, ti in self.inputs])
        except TicketError as e:
            return await interaction.followup.send(embed=ui.error(str(e)), ephemeral=True)
        await interaction.followup.send(embed=ui.ok(f"Your ticket is open: {ch.mention}"), ephemeral=True)


class ReasonModal(discord.ui.Modal, title="Close the ticket"):
    reason = discord.ui.TextInput(label="Why is it being closed?", style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, cog: Tickets, t: dict):
        super().__init__(timeout=300)
        self.cog, self.t = cog, t

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self.cog.close(self.t, interaction.user, self.reason.value)
        except TicketError as e:
            await interaction.followup.send(embed=ui.error(str(e)), ephemeral=True)
