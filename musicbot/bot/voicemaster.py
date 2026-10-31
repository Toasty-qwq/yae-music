"""The VC maker (like VoiceMaster): join a generator voice channel and get your own temporary one.

Generators ("setups") come in five kinds:
  default     the member's own saved name and limit (or "{username}'s channel")
  sequence    "Gaming 1", "Gaming 2", ... with a fixed limit
  predefined  a name template: {username}, {seq}, {animal}, {color}, {tree}
  clone       copies the generator's name, limit and bitrate
  dynamic     no generator to join: one empty room is always waiting in the category

The owner controls their channel with the panel posted in its chat, or /voice commands. Admins can
switch any control off for members. Channels are deleted when the last person leaves.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import settings
from . import messages, ui
from .templates import Ctx

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.voicemaster")

ANIMALS = ["Fox", "Owl", "Otter", "Panda", "Koala", "Tiger", "Wolf", "Lynx", "Raven", "Seal", "Badger", "Heron"]
COLORS = ["Crimson", "Amber", "Jade", "Azure", "Violet", "Coral", "Ivory", "Onyx", "Sakura", "Teal", "Plum", "Gold"]
TREES = ["Oak", "Maple", "Cedar", "Birch", "Willow", "Pine", "Cherry", "Elm", "Ash", "Cypress", "Aspen", "Fir"]
REGIONS = ["brazil", "hongkong", "india", "japan", "rotterdam", "singapore", "south-korea", "southafrica",
           "sydney", "us-central", "us-east", "us-south", "us-west"]
FEATURE_WORD = {
    "name": "renaming", "limit": "user limits", "status": "channel statuses", "lock": "locking",
    "claim": "claiming", "reject": "rejecting people", "permit": "permitting people", "ghost": "hiding channels",
    "lfm": "Looking for members posts", "text": "text channels", "bitrate": "bitrate", "invite": "invites",
    "transfer": "transferring", "nsfw": "the age-restricted switch", "region": "regions",
}
SETTINGS_OPTIONS = [  # (value, label, description, feature)
    ("name", "Rename", "Give the channel a new name", "name"),
    ("limit", "User limit", "How many people can join", "limit"),
    ("status", "Status", "The line shown under the channel", "status"),
    ("bitrate", "Bitrate", "Sound quality", "bitrate"),
    ("region", "Region", "Where the voice server is", "region"),
    ("nsfw", "Age-restricted on/off", "Mark the channel age-restricted", "nsfw"),
    ("text", "Text channel", "A text channel that goes with it", "text"),
    ("lfm", "Looking for members", "Ask for people to join", "lfm"),
    ("save", "Save my settings", "Use these settings for your next channels", None),
]
PERMISSION_OPTIONS = [
    ("lock", "Lock", "Nobody new can join", "lock"),
    ("unlock", "Unlock", "Anyone can join again", "lock"),
    ("ghost", "Hide", "Hide the channel from the list", "ghost"),
    ("unghost", "Show", "Show the channel again", "ghost"),
    ("permit", "Permit", "Let someone in, even when locked", "permit"),
    ("reject", "Reject", "Keep someone out (and remove them)", "reject"),
    ("invite", "Invite", "Send someone an invite by DM", "invite"),
    ("claim", "Claim", "Take over a channel whose owner left", "claim"),
    ("transfer", "Transfer", "Give the channel to someone else", "transfer"),
]


class VMError(Exception):
    """A message for the person who tried."""


class VoiceMaster(commands.Cog):
    voice = app_commands.Group(name="voice", description="Control your own temporary voice channel", guild_only=True)

    def __init__(self, bot: "MusicBot"):
        self.bot = bot
        self.owned: dict[int, dict] = {}            # channel id -> row
        self._creating: dict[int, float] = {}         # member id -> when (no double channels)
        self._renames: dict[int, list[float]] = defaultdict(list)
        self._mutex = asyncio.Lock()  # one join/leave at a time, so channels aren't made twice

    async def cog_load(self) -> None:
        for r in await self.bot.db.all("SELECT * FROM vm_channels"):
            self.owned[r["channel_id"]] = r

    # ---- helpers ---------------------------------------------------------------------------------

    async def cfg(self, guild_id: int) -> dict:
        return await self.bot.configs.section(guild_id, "voicemaster")

    def setup_of(self, cfg: dict, setup_id: str) -> dict | None:
        return next((s for s in cfg["setups"] if s["id"] == setup_id), None)

    async def log(self, guild: discord.Guild, cfg: dict, text: str) -> None:
        ch = guild.get_channel(cfg.get("log_channel_id") or 0)
        if ch:
            try:
                await ch.send(embed=discord.Embed(description=text[:4000], colour=ui.COLOR),
                              allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def profile(self, user_id: int) -> dict:
        r = await self.bot.db.one("SELECT data FROM vm_profiles WHERE user_id=?", (user_id,))
        return json.loads(r["data"]) if r else {}

    def channel_for(self, member: discord.Member, where: discord.abc.GuildChannel | None) -> discord.VoiceChannel:
        """The temporary channel an action is about: the panel's own channel, or where the member is."""
        if isinstance(where, discord.VoiceChannel) and where.id in self.owned:
            return where
        vc = member.voice.channel if member.voice else None
        if isinstance(vc, discord.VoiceChannel) and vc.id in self.owned:
            return vc
        raise VMError("Join your temporary voice channel first, then use this.")

    async def check(self, member: discord.Member, channel: discord.VoiceChannel, feature: str | None,
                    *, owner_only: bool = True) -> dict:
        cfg = await self.cfg(member.guild.id)
        admin = member.guild_permissions.manage_channels
        if feature and not cfg["features"].get(feature, True) and not admin:
            raise VMError(f"This server has switched off {FEATURE_WORD.get(feature, feature)}.")
        row = self.owned.get(channel.id)
        if owner_only and row and row["owner_id"] != member.id and not admin:
            raise VMError("Only the channel's owner can do that. If they left, use Claim.")
        return cfg

    async def set_owner(self, channel: discord.VoiceChannel, member: discord.Member | None) -> None:
        row = self.owned[channel.id]
        cfg = await self.cfg(channel.guild.id)
        old = channel.guild.get_member(row["owner_id"] or 0)
        row["owner_id"] = member.id if member else None
        await self.bot.db.run("UPDATE vm_channels SET owner_id=? WHERE channel_id=?", (row["owner_id"], channel.id))
        role = channel.guild.get_role(cfg.get("temp_role_id") or 0)
        try:
            if old and old != member and (cfg["owner_manage_channel"] or cfg["owner_move_members"]):
                ow = channel.overwrites_for(old)
                ow.manage_channels = ow.move_members = None
                await channel.set_permissions(old, overwrite=None if ow.is_empty() else ow)
            if member:
                ow = channel.overwrites_for(member)
                ow.connect = ow.view_channel = True
                if cfg["owner_manage_channel"]:
                    ow.manage_channels = True
                if cfg["owner_move_members"]:
                    ow.move_members = True
                await channel.set_permissions(member, overwrite=ow, reason="Temporary channel owner")
            if role and old and old != member:
                await old.remove_roles(role, reason="No longer owns a temporary channel")
            if role and member:
                await member.add_roles(role, reason="Owns a temporary channel")
        except discord.HTTPException as e:
            log.debug("owner perms: %s", e)

    # ---- creating and cleaning up ----------------------------------------------------------------

    def _name(self, setup: dict, member: discord.Member | None, prof: dict, seq: int,
              generator: discord.VoiceChannel | None) -> str:
        mode = setup["mode"]
        uname = member.display_name if member else "Room"
        if mode == "default":
            name = (prof.get("name") if setup["editable"] else None) or setup["name"]
        elif mode == "clone" and generator:
            name = generator.name
        elif mode in ("sequence", "dynamic"):
            name = setup["name"] if "{seq}" in setup["name"] else f"{setup['name']} {{seq}}"
        else:
            name = setup["name"]
        name = (name.replace("{username}", uname).replace("{user}", uname).replace("{seq}", str(seq))
                .replace("{animal}", random.choice(ANIMALS)).replace("{animals}", random.choice(ANIMALS))
                .replace("{color}", random.choice(COLORS)).replace("{colors}", random.choice(COLORS))
                .replace("{tree}", random.choice(TREES)).replace("{trees}", random.choice(TREES)))
        return name[:100] or "Voice"

    def _seq(self, guild: discord.Guild, setup: dict) -> int:
        used = set()
        for cid, r in self.owned.items():
            ch = guild.get_channel(cid)
            if r["setup_id"] == setup["id"] and ch:
                used.add(r.get("seq") or 0)
                for part in ch.name.split():
                    if part.isdigit():
                        used.add(int(part))
        n = 1
        while n in used:
            n += 1
        return n

    async def create(self, member: discord.Member | None, setup: dict, cfg: dict) -> discord.VoiceChannel | None:
        guild = (member.guild if member else None) or self.bot.get_guild(cfg["_guild"])
        category = guild.get_channel(setup["category_id"])
        generator = guild.get_channel(setup["channel_id"] or 0)
        prof = await self.profile(member.id) if member else {}
        seq = self._seq(guild, setup)
        source = generator if setup["permissions_from"] == "generator" and generator else category
        overwrites = dict(source.overwrites) if source else {}
        limit = setup["limit"]
        bitrate = setup["bitrate"]
        if setup["mode"] == "clone" and generator:
            limit, bitrate = generator.user_limit, generator.bitrate // 1000
        elif setup["mode"] == "default" and setup["editable"] and prof:
            limit = int(prof.get("limit", limit) or 0)
            bitrate = int(prof.get("bitrate", bitrate) or bitrate)
        bitrate = max(8, min(bitrate, int(guild.bitrate_limit // 1000)))
        try:
            vc = await guild.create_voice_channel(
                self._name(setup, member, prof, seq, generator), category=category, overwrites=overwrites,
                user_limit=limit, bitrate=bitrate * 1000,
                rtc_region=(prof.get("region") if setup["editable"] and setup["mode"] == "default" else None),
                reason=f"VC maker: {member or 'ready room'}")
        except discord.HTTPException as e:
            log.warning("guild %s: making a temporary channel failed: %s", guild.id, e)
            return None
        row = {"channel_id": vc.id, "guild_id": guild.id, "owner_id": member.id if member else None,
               "setup_id": setup["id"], "text_channel_id": None, "created_at": time.time(), "seq": seq}
        self.owned[vc.id] = row
        await self.bot.db.run("INSERT OR REPLACE INTO vm_channels(channel_id, guild_id, owner_id, setup_id, created_at) "
                              "VALUES(?,?,?,?,?)", (vc.id, guild.id, row["owner_id"], setup["id"], row["created_at"]))
        if member:
            await self.claim_new(vc, member, setup, cfg, prof)
        return vc

    async def claim_new(self, vc: discord.VoiceChannel, member: discord.Member, setup: dict, cfg: dict, prof: dict) -> None:
        """Make ``member`` the owner of a fresh channel and set it up the way they like it."""
        await self.set_owner(vc, member)
        if setup["editable"] and setup["mode"] == "default" and prof:
            try:
                if prof.get("locked"):
                    await self._lock(vc, True)
                if prof.get("ghost"):
                    await self._ghost(vc, True)
                if prof.get("nsfw"):
                    await vc.edit(nsfw=True)
                if prof.get("status"):
                    await vc.edit(status=prof["status"][:500])
            except discord.HTTPException:
                pass
        if cfg["auto_text"]:
            try:
                await self.make_text(vc, member, cfg)
            except (discord.HTTPException, VMError):
                pass
        if cfg["interface"]:
            await self.send_interface(vc, member, cfg)
        await self.log(vc.guild, cfg, f"{member.mention} made {vc.mention}.")

    async def remove(self, channel: discord.VoiceChannel, reason: str = "Empty") -> None:
        row = self.owned.pop(channel.id, None)
        if not row:
            return
        await self.bot.db.run("DELETE FROM vm_channels WHERE channel_id=?", (channel.id,))
        cfg = await self.cfg(channel.guild.id)
        owner = channel.guild.get_member(row["owner_id"] or 0)
        role = channel.guild.get_role(cfg.get("temp_role_id") or 0)
        if role and owner and not any(r["owner_id"] == owner.id for r in self.owned.values()):
            try:
                await owner.remove_roles(role, reason="Temporary channel closed")
            except discord.HTTPException:
                pass
        text = channel.guild.get_channel(row.get("text_channel_id") or 0)
        for ch in (text, channel):
            if ch:
                try:
                    await ch.delete(reason=f"VC maker: {reason}")
                except discord.HTTPException:
                    pass

    async def keep_dynamic(self, guild: discord.Guild, cfg: dict) -> None:
        """Dynamic generators: exactly one empty room waiting in each one's category."""
        for setup in cfg["setups"]:
            if setup["mode"] != "dynamic":
                continue
            rooms = [guild.get_channel(c) for c, r in self.owned.items() if r["setup_id"] == setup["id"]]
            empty = [r for r in rooms if r is not None and not r.members]
            for extra in empty[1:]:
                await self.remove(extra, "Spare ready room")
            if not empty:
                await self.create(None, setup, {**cfg, "_guild": guild.id})

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel:
            return
        guild = member.guild
        cfg = await self.cfg(guild.id)
        if not cfg["enabled"] and not self.owned:
            return
        async with self._mutex:
            # someone joined a generator: make them a channel (or send them back to theirs)
            if cfg["enabled"] and after.channel and not member.bot:
                setup = next((s for s in cfg["setups"] if s["channel_id"] == after.channel.id), None)
                if setup:
                    mine = next((guild.get_channel(c) for c, r in self.owned.items()
                                 if r["owner_id"] == member.id and r["guild_id"] == guild.id and guild.get_channel(c)), None)
                    if time.monotonic() - self._creating.get(member.id, 0) < 5:
                        return
                    self._creating[member.id] = time.monotonic()
                    vc = mine or await self.create(member, setup, cfg)
                    if vc:
                        try:
                            await member.move_to(vc, reason="VC maker")
                        except discord.HTTPException:
                            if not mine and not vc.members:
                                await self.remove(vc, "Couldn't move its owner in")
                    return
                # someone walked into an empty dynamic room: it's theirs now
                row = self.owned.get(after.channel.id)
                if row and row["owner_id"] is None:
                    setup = self.setup_of(cfg, row["setup_id"])
                    if setup:
                        await self.claim_new(after.channel, member, setup, cfg, await self.profile(member.id))
                        await self.keep_dynamic(guild, cfg)
            # someone left a temporary channel
            if before.channel and before.channel.id in self.owned:
                ch = before.channel
                row = self.owned[ch.id]
                if not [m for m in ch.members if not m.bot]:
                    await self.remove(ch)
                    await self.keep_dynamic(guild, cfg)
                elif row["owner_id"] == member.id:
                    role = guild.get_role(cfg.get("temp_role_id") or 0)
                    if role:
                        try:
                            await member.remove_roles(role, reason="Left their temporary channel")
                        except discord.HTTPException:
                            pass
                    await self.log(guild, cfg, f"{member.mention} left their channel {ch.mention}; anyone in it can claim it.")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if channel.id in self.owned:
            await self.remove(channel, "Deleted by hand")

    @commands.Cog.listener()
    async def on_ready(self):
        """Tidy up after downtime: empty channels go, dynamic rooms are topped up."""
        for cid, row in list(self.owned.items()):
            ch = self.bot.get_channel(cid)
            if ch is None:
                self.owned.pop(cid, None)
                await self.bot.db.run("DELETE FROM vm_channels WHERE channel_id=?", (cid,))
            elif not [m for m in ch.members if not m.bot] and row["owner_id"] is not None:
                await self.remove(ch, "Empty after a restart")
        for guild in self.bot.guilds:
            cfg = await self.cfg(guild.id)
            if cfg["enabled"]:
                await self.keep_dynamic(guild, cfg)

    # ---- the actions (panel and /voice share these) -----------------------------------------------

    async def _lock(self, vc: discord.VoiceChannel, on: bool) -> None:
        everyone = vc.guild.default_role
        ow = vc.overwrites_for(everyone)
        ow.connect = False if on else None
        await vc.set_permissions(everyone, overwrite=ow, reason="VC maker: lock" if on else "VC maker: unlock")
        if on:
            for m in vc.members:
                mo = vc.overwrites_for(m)
                if mo.connect is not True:
                    mo.connect = True
                    await vc.set_permissions(m, overwrite=mo)

    async def _ghost(self, vc: discord.VoiceChannel, on: bool) -> None:
        everyone = vc.guild.default_role
        ow = vc.overwrites_for(everyone)
        ow.view_channel = False if on else None
        await vc.set_permissions(everyone, overwrite=ow, reason="VC maker: hide" if on else "VC maker: show")
        if on:
            for m in vc.members:
                mo = vc.overwrites_for(m)
                mo.view_channel = True
                await vc.set_permissions(m, overwrite=mo)

    async def rename(self, vc: discord.VoiceChannel, name: str) -> str:
        now = time.monotonic()
        recent = [t for t in self._renames[vc.id] if now - t < 600]
        if len(recent) >= 2:
            wait = int(600 - (now - recent[0])) // 60 + 1
            raise VMError(f"Discord allows two renames per channel every 10 minutes. Try again in about {wait} minutes.")
        await vc.edit(name=name[:100], reason="VC maker: rename")
        self._renames[vc.id] = recent + [now]
        return f"Renamed to **{discord.utils.escape_markdown(name[:100])}**."

    async def make_text(self, vc: discord.VoiceChannel, owner: discord.Member, cfg: dict) -> discord.TextChannel:
        row = self.owned[vc.id]
        if vc.guild.get_channel(row.get("text_channel_id") or 0):
            raise VMError("This channel already has a text channel.")
        ows = {vc.guild.default_role: discord.PermissionOverwrite(view_channel=False),
               vc.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
        for m in vc.members:
            ows[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        access = vc.guild.get_role(cfg.get("text_access_role_id") or 0)
        if access:
            ows[access] = discord.PermissionOverwrite(view_channel=True)
        text = await vc.guild.create_text_channel(f"{vc.name}-text"[:100], category=vc.category, overwrites=ows,
                                                  reason="VC maker: text channel")
        row["text_channel_id"] = text.id
        await self.bot.db.run("UPDATE vm_channels SET text_channel_id=? WHERE channel_id=?", (text.id, vc.id))
        try:
            await messages.send(text, cfg["text_message"], Ctx(vc.guild, owner, text))
        except discord.HTTPException:
            pass
        return text

    async def do(self, member: discord.Member, vc: discord.VoiceChannel, action: str, value=None) -> str:
        """Run one control. Returns what happened, in words; raises VMError for refusals."""
        feature = {"unlock": "lock", "unghost": "ghost", "save": None}.get(action, action)
        cfg = await self.check(member, vc, feature, owner_only=action != "claim")
        row = self.owned[vc.id]
        if action == "name":
            return await self.rename(vc, value)
        if action == "limit":
            await vc.edit(user_limit=value)
            return f"User limit set to {value or 'unlimited'}."
        if action == "status":
            await vc.edit(status=(value or None) and value[:500])
            return "Status updated." if value else "Status cleared."
        if action == "bitrate":
            kbps = min(int(value), int(vc.guild.bitrate_limit // 1000))
            await vc.edit(bitrate=kbps * 1000)
            return f"Bitrate set to {kbps} kbps."
        if action == "region":
            await vc.edit(rtc_region=None if value in (None, "", "automatic") else value)
            return f"Region set to {value or 'automatic'}."
        if action == "nsfw":
            was = vc.nsfw
            await vc.edit(nsfw=not was)
            return "No longer age-restricted." if was else "Marked age-restricted."
        if action in ("lock", "unlock"):
            await self._lock(vc, action == "lock")
            return "Locked: nobody new can join." if action == "lock" else "Unlocked: anyone can join."
        if action in ("ghost", "unghost"):
            await self._ghost(vc, action == "ghost")
            return "Hidden from the channel list." if action == "ghost" else "Visible again."
        if action == "permit":
            ow = vc.overwrites_for(value)
            ow.connect = ow.view_channel = True
            await vc.set_permissions(value, overwrite=ow, reason="VC maker: permit")
            return f"{value.mention} can join now."
        if action == "reject":
            if value == member or (isinstance(value, discord.Member) and value.id == row["owner_id"]):
                raise VMError("You can't reject yourself.")
            ow = vc.overwrites_for(value)
            ow.connect = False
            ow.view_channel = False
            await vc.set_permissions(value, overwrite=ow, reason="VC maker: reject")
            targets = [value] if isinstance(value, discord.Member) else [m for m in vc.members if value in m.roles]
            for m in targets:
                if m in vc.members and m.id != row["owner_id"]:
                    try:
                        await m.move_to(None, reason="VC maker: rejected")
                    except discord.HTTPException:
                        pass
            return f"{value.mention} is kept out."
        if action == "claim":
            if row["owner_id"] == member.id:
                raise VMError("It's already yours.")
            owner = vc.guild.get_member(row["owner_id"] or 0)
            if owner and owner in vc.members and not member.guild_permissions.manage_channels:
                raise VMError(f"{owner.mention} is still here. Claim works once the owner leaves.")
            await self.set_owner(vc, member)
            await self.log(vc.guild, cfg, f"{member.mention} claimed {vc.mention}.")
            return "The channel is yours now."
        if action == "transfer":
            if not isinstance(value, discord.Member) or value.bot:
                raise VMError("Pick a person to give it to.")
            if value not in vc.members:
                raise VMError(f"{value.mention} has to be in the channel to take it over.")
            await self.set_owner(vc, value)
            await self.log(vc.guild, cfg, f"{member.mention} gave {vc.mention} to {value.mention}.")
            return f"{value.mention} owns the channel now."
        if action == "invite":
            target, note = value
            inv = await vc.create_invite(max_age=86400, max_uses=1, unique=True, reason="VC maker: invite")
            try:
                await target.send(f"{member.display_name} invites you to their voice channel **{vc.name}** in "
                                  f"**{vc.guild.name}**: {inv.url}" + (f"\n> {note[:300]}" if note else ""))
            except discord.HTTPException:
                raise VMError(f"{target.mention}'s DMs are closed. Send them this link yourself: {inv.url}") from None
            ow = vc.overwrites_for(target)
            ow.connect = ow.view_channel = True
            await vc.set_permissions(target, overwrite=ow)
            return f"Invite sent to {target.mention}."
        if action == "text":
            text = await self.make_text(vc, member, cfg)
            return f"Made {text.mention}. It goes away with the voice channel."
        if action == "lfm":
            ch = vc.guild.get_channel(cfg.get("lfm_channel_id") or 0)
            if ch is None:
                raise VMError("This server has no Looking for members channel set.")
            inv = await vc.create_invite(max_age=3600, reason="VC maker: looking for members")
            free = f"{len(vc.members)}/{vc.user_limit}" if vc.user_limit else f"{len(vc.members)} in it"
            e = discord.Embed(title=f"{member.display_name} is looking for members", colour=ui.COLOR,
                              description=(value[:500] + "\n\n" if value else "") + f"**{vc.name}** · {free}\n[Join the channel]({inv.url})")
            e.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=e)
            return f"Posted in {ch.mention}."
        if action == "save":
            prof = {"name": vc.name, "limit": vc.user_limit, "bitrate": vc.bitrate // 1000, "region": vc.rtc_region,
                    "nsfw": vc.nsfw, "locked": vc.overwrites_for(vc.guild.default_role).connect is False,
                    "ghost": vc.overwrites_for(vc.guild.default_role).view_channel is False,
                    "status": getattr(vc, "status", None) or ""}
            await self.bot.db.run("INSERT OR REPLACE INTO vm_profiles(user_id, data) VALUES(?,?)", (member.id, json.dumps(prof)))
            return "Saved. Your next channels start with this name, limit, bitrate, region and lock."
        if action == "load":
            prof = await self.profile(member.id)
            if not prof:
                raise VMError("You haven't saved any settings yet. Set up your channel, then pick Save my settings.")
            done = []
            if prof.get("name") and prof["name"] != vc.name:
                try:
                    done.append(await self.rename(vc, prof["name"]))
                except VMError as e:
                    done.append(str(e))
            await vc.edit(user_limit=int(prof.get("limit") or 0),
                          bitrate=min(int(prof.get("bitrate") or 64), int(vc.guild.bitrate_limit // 1000)) * 1000)
            await self._lock(vc, bool(prof.get("locked")))
            await self._ghost(vc, bool(prof.get("ghost")))
            return "Your saved settings are on. " + " ".join(done)
        raise VMError("That control doesn't exist.")

    # ---- the panel in each channel's chat -------------------------------------------------------

    def interface_embed(self, vc: discord.VoiceChannel | None, owner: discord.Member | None) -> discord.Embed:
        e = discord.Embed(title="Channel controls", colour=ui.COLOR,
                          description="Change your channel with the menus below, or with the /voice commands.")
        if owner:
            e.set_author(name=f"{owner.display_name}'s channel", icon_url=owner.display_avatar.url)
        e.set_footer(text="Only the owner can use most controls. If they leave, anyone inside can claim it.")
        return e

    def interface_view(self, cfg: dict) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        feats = cfg["features"]
        opts1 = [discord.SelectOption(label=label, value=v, description=d) for v, label, d, f in SETTINGS_OPTIONS
                 if f is None or feats.get(f, True)]
        opts2 = [discord.SelectOption(label=label, value=v, description=d) for v, label, d, f in PERMISSION_OPTIONS
                 if feats.get(f, True)]
        if opts1:
            view.add_item(discord.ui.Select(custom_id="vm:set", placeholder="Channel settings", options=opts1))
        if opts2:
            view.add_item(discord.ui.Select(custom_id="vm:perm", placeholder="Channel permissions", options=opts2))
        view.add_item(discord.ui.Button(custom_id="vm:load", label="Load my settings", style=discord.ButtonStyle.secondary))
        public = (settings.load().get("public_url") or "").strip()
        if public.startswith("https://"):
            view.add_item(discord.ui.Button(label="Dashboard", url=public, style=discord.ButtonStyle.link))
        return view

    async def send_interface(self, vc: discord.VoiceChannel, owner: discord.Member | None, cfg: dict,
                             channel: discord.abc.Messageable | None = None) -> None:
        try:
            await (channel or vc).send(content=owner.mention if owner and cfg["interface_ping"] else None,
                                       embed=self.interface_embed(vc, owner), view=self.interface_view(cfg))
        except discord.HTTPException as e:
            log.debug("interface: %s", e)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("vm:") or not isinstance(interaction.user, discord.Member):
            return
        member = interaction.user
        try:
            vc = self.channel_for(member, interaction.channel)
            if cid == "vm:load":
                return await self.reply(interaction, await self.do(member, vc, "load"))
            choice = ((interaction.data or {}).get("values") or [""])[0]
            await self.route(interaction, member, vc, choice)
        except VMError as e:
            await self.reply(interaction, str(e), error=True)
        except discord.HTTPException as e:
            await self.reply(interaction, f"Discord refused: {e.text or e}", error=True)

    async def reply(self, interaction: discord.Interaction, text: str, *, error: bool = False) -> None:
        embed = ui.error(text) if error else ui.ok(text)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def route(self, interaction: discord.Interaction, member: discord.Member, vc: discord.VoiceChannel, choice: str):
        """Panel choices: ask for what's missing (a modal or a picker), then act."""
        feature = {"unlock": "lock", "unghost": "ghost", "save": None}.get(choice, choice)
        await self.check(member, vc, feature, owner_only=choice != "claim")
        if choice in ("name", "limit", "status", "lfm"):
            return await interaction.response.send_modal(AskModal(self, member, vc, choice))
        if choice == "bitrate":
            top = int(vc.guild.bitrate_limit // 1000)
            opts = [discord.SelectOption(label=f"{k} kbps", value=str(k), default=k == vc.bitrate // 1000)
                    for k in (32, 64, 96, 128, 256, 384) if k <= top]
            return await interaction.response.send_message(view=PickView(self, member, vc, "bitrate", options=opts), ephemeral=True)
        if choice == "region":
            opts = [discord.SelectOption(label="Automatic", value="automatic")] + \
                   [discord.SelectOption(label=r, value=r) for r in REGIONS]
            return await interaction.response.send_message(view=PickView(self, member, vc, "region", options=opts), ephemeral=True)
        if choice in ("permit", "reject"):
            return await interaction.response.send_message(view=PickView(self, member, vc, choice, mentionable=True), ephemeral=True)
        if choice in ("invite", "transfer"):
            return await interaction.response.send_message(view=PickView(self, member, vc, choice, users=True), ephemeral=True)
        await self.reply(interaction, await self.do(member, vc, choice))

    # ---- /voice ------------------------------------------------------------------------------------

    async def run(self, interaction: discord.Interaction, action: str, value=None) -> None:
        try:
            vc = self.channel_for(interaction.user, None)
            await self.reply(interaction, await self.do(interaction.user, vc, action, value))
        except VMError as e:
            await self.reply(interaction, str(e), error=True)
        except discord.HTTPException as e:
            await self.reply(interaction, f"Discord refused: {e.text or e}", error=True)

    @voice.command(name="name", description="Rename your channel")
    async def v_name(self, i: discord.Interaction, name: app_commands.Range[str, 1, 100]):
        await self.run(i, "name", name)

    @voice.command(name="limit", description="How many people can join (0 = no limit)")
    async def v_limit(self, i: discord.Interaction, limit: app_commands.Range[int, 0, 99]):
        await self.run(i, "limit", limit)

    @voice.command(name="lock", description="Nobody new can join")
    async def v_lock(self, i: discord.Interaction):
        await self.run(i, "lock")

    @voice.command(name="unlock", description="Anyone can join again")
    async def v_unlock(self, i: discord.Interaction):
        await self.run(i, "unlock")

    @voice.command(name="claim", description="Take over a channel whose owner left")
    async def v_claim(self, i: discord.Interaction):
        await self.run(i, "claim")

    @voice.command(name="permit", description="Let a person or role in, even when locked")
    async def v_permit(self, i: discord.Interaction, mention: discord.Member | discord.Role):
        await self.run(i, "permit", mention)

    @voice.command(name="reject", description="Keep a person or role out (and remove them)")
    async def v_reject(self, i: discord.Interaction, mention: discord.Member | discord.Role):
        await self.run(i, "reject", mention)

    @voice.command(name="status", description="The line shown under your channel (empty clears it)")
    async def v_status(self, i: discord.Interaction, status: app_commands.Range[str, 0, 500] = ""):
        await self.run(i, "status", status)

    @voice.command(name="lfm", description="Post that you're looking for members")
    async def v_lfm(self, i: discord.Interaction, message: app_commands.Range[str, 0, 500] = ""):
        await self.run(i, "lfm", message)

    @voice.command(name="text", description="Make a text channel that goes with your voice channel")
    async def v_text(self, i: discord.Interaction):
        await self.run(i, "text")

    @voice.command(name="bitrate", description="Sound quality")
    @app_commands.choices(bitrate=[app_commands.Choice(name=f"{k} kbps", value=k) for k in (32, 64, 96, 128, 256, 384)])
    async def v_bitrate(self, i: discord.Interaction, bitrate: int):
        await self.run(i, "bitrate", bitrate)

    @voice.command(name="ghost", description="Hide your channel from the channel list")
    async def v_ghost(self, i: discord.Interaction):
        await self.run(i, "ghost")

    @voice.command(name="unghost", description="Show your channel again")
    async def v_unghost(self, i: discord.Interaction):
        await self.run(i, "unghost")

    @voice.command(name="invite", description="Send someone an invite to your channel by DM")
    async def v_invite(self, i: discord.Interaction, member: discord.Member, message: app_commands.Range[str, 0, 300] = ""):
        await self.run(i, "invite", (member, message))

    @voice.command(name="nsfw", description="Turn age-restricted on or off")
    async def v_nsfw(self, i: discord.Interaction):
        await self.run(i, "nsfw")

    @voice.command(name="transfer", description="Give your channel to someone in it")
    async def v_transfer(self, i: discord.Interaction, member: discord.Member):
        await self.run(i, "transfer", member)

    @voice.command(name="region", description="Where the voice server is")
    @app_commands.choices(region=[app_commands.Choice(name="automatic", value="automatic")] +
                          [app_commands.Choice(name=r, value=r) for r in REGIONS])
    async def v_region(self, i: discord.Interaction, region: str):
        await self.run(i, "region", region)

    @voice.command(name="save", description="Save your channel's settings for your next channels")
    async def v_save(self, i: discord.Interaction):
        await self.run(i, "save")

    @voice.command(name="load", description="Put your saved settings on this channel")
    async def v_load(self, i: discord.Interaction):
        await self.run(i, "load")

    @voice.command(name="panel", description="Post the channel controls here")
    @app_commands.default_permissions(manage_channels=True)
    async def v_panel(self, i: discord.Interaction):
        cfg = await self.cfg(i.guild_id)
        await i.response.send_message(embed=self.interface_embed(None, None), view=self.interface_view(cfg))


class AskModal(discord.ui.Modal):
    def __init__(self, cog: VoiceMaster, member: discord.Member, vc: discord.VoiceChannel, action: str):
        titles = {"name": "Rename your channel", "limit": "User limit", "status": "Channel status",
                  "lfm": "Looking for members"}
        super().__init__(title=titles[action], timeout=300)
        self.cog, self.member, self.vc, self.action = cog, member, vc, action
        if action == "name":
            self.field = discord.ui.TextInput(label="New name", default=vc.name, max_length=100)
        elif action == "limit":
            self.field = discord.ui.TextInput(label="How many people (0 = no limit)", default=str(vc.user_limit), max_length=2)
        elif action == "status":
            self.field = discord.ui.TextInput(label="Status (empty clears it)", required=False, max_length=500)
        else:
            self.field = discord.ui.TextInput(label="A message (optional)", required=False, max_length=500,
                                              style=discord.TextStyle.paragraph)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.field.value.strip()
        if self.action == "limit":
            if not value.isdigit() or int(value) > 99:
                return await self.cog.reply(interaction, "The limit is a number from 0 to 99.", error=True)
            value = int(value)
        try:
            await self.cog.reply(interaction, await self.cog.do(self.member, self.vc, self.action, value))
        except VMError as e:
            await self.cog.reply(interaction, str(e), error=True)
        except discord.HTTPException as e:
            await self.cog.reply(interaction, f"Discord refused: {e.text or e}", error=True)


class PickView(discord.ui.View):
    """An ephemeral picker: a list of choices, people, or people-and-roles."""

    def __init__(self, cog: VoiceMaster, member, vc, action, *, options=None, users=False, mentionable=False):
        super().__init__(timeout=120)
        self.cog, self.member, self.vc, self.action = cog, member, vc, action
        if options:
            item = discord.ui.Select(placeholder="Pick one", options=options)
        elif mentionable:
            item = discord.ui.MentionableSelect(placeholder="Pick a person or a role")
        else:
            item = discord.ui.UserSelect(placeholder="Pick a person")
        item.callback = self.picked
        self.item = item
        self.add_item(item)

    async def picked(self, interaction: discord.Interaction):
        v = self.item.values[0]
        if isinstance(v, discord.User) and not isinstance(v, discord.Member):
            v = self.vc.guild.get_member(v.id) or v
        value = (v, "") if self.action == "invite" else v
        try:
            text = await self.cog.do(self.member, self.vc, self.action, value)
            await interaction.response.edit_message(embed=ui.ok(text), view=None)
        except VMError as e:
            await interaction.response.edit_message(embed=ui.error(str(e)), view=None)
        except discord.HTTPException as e:
            await interaction.response.edit_message(embed=ui.error(f"Discord refused: {e.text or e}"), view=None)
