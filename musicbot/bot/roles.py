"""Reaction roles: menus of buttons, a dropdown, or emoji reactions that hand out roles.

Modes: ``normal`` (click to get, click again to lose), ``unique`` (one role from the menu at a
time), ``verify`` (get only; it is never taken away by the menu).

Buttons and dropdowns are answered from ``on_interaction`` by their custom id
(``rr:<menu>:<role>`` / ``rr:<menu>``), so menus keep working after a restart without
re-registering anything.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from . import templates
from .guildconfig import ConfigError, HEX, role_problem

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.roles")
KINDS = ("buttons", "dropdown", "reactions")
MODES = ("normal", "unique", "verify")


def _emoji_key(e) -> str:
    """Comparable form of an emoji: '<:name:id>' for custom ones, the character(s) otherwise."""
    s = str(e).strip()
    if s.startswith("<") and s.endswith(">"):
        return s.split(":")[-1].rstrip(">")  # the id is what identifies a custom emoji
    return s.replace("️", "")


def _parse_emoji(bot: "MusicBot", value: str) -> discord.PartialEmoji | None:
    value = (value or "").strip()
    if not value:
        return None
    e = discord.PartialEmoji.from_str(value)
    if e.id is None and (re.search(r"[A-Za-z]", value) or len(value) > 16):
        raise ConfigError(f"{value!r} isn't an emoji. Paste the emoji itself (like a game controller), "
                          "or click one of the server's own emoji under the table.")
    if e.id and bot.get_emoji(e.id) is None:
        raise ConfigError(f"The bot can't use the emoji {value}: it must be from a server the bot is in.")
    return e


def validate_menu(bot: "MusicBot", guild: discord.Guild, d: dict) -> dict:
    kind = d.get("kind")
    mode = d.get("mode", "normal")
    if kind not in KINDS:
        raise ConfigError("Pick buttons, a dropdown or reactions.")
    if mode not in MODES:
        raise ConfigError("Pick how the roles behave.")
    try:
        ch = guild.get_channel(int(d.get("channel_id") or 0))
    except (TypeError, ValueError):
        ch = None
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        raise ConfigError("Pick the channel the menu goes in.")
    p = ch.permissions_for(guild.me)
    if not (p.view_channel and p.send_messages and p.embed_links):
        raise ConfigError(f"The bot can't post in #{ch.name}: it needs View Channel, Send Messages and Embed Links.")
    if kind == "reactions" and not (p.add_reactions and p.read_message_history):
        raise ConfigError(f"Reaction menus need Add Reactions and Read Message History in #{ch.name}.")
    title = str(d.get("title") or "").strip()[:256] or "Pick your roles"
    description = str(d.get("description") or "").strip()
    if len(description) > 3500:
        raise ConfigError("The description is too long: keep it under 3500 characters.")
    color = str(d.get("color") or "#f2879f")
    if not HEX.match(color):
        raise ConfigError("The colour must look like #f2879f.")
    roles, seen_emoji = [], set()
    for item in d.get("roles") or []:
        try:
            role = guild.get_role(int(item.get("role_id")))
        except (TypeError, ValueError):
            role = None
        if role is None:
            raise ConfigError("One of the roles no longer exists. Remove it from the menu.")
        problem = role_problem(guild, role)
        if problem:
            raise ConfigError(problem)
        emoji = _parse_emoji(bot, item.get("emoji") or "")
        label = str(item.get("label") or "").strip()[:80] or role.name[:80]
        if kind == "reactions":
            if emoji is None:
                raise ConfigError(f"Reaction menus need an emoji for every role (@{role.name} has none).")
            if _emoji_key(emoji) in seen_emoji:
                raise ConfigError("Each role needs its own emoji.")
            seen_emoji.add(_emoji_key(emoji))
        roles.append({"role_id": role.id, "label": label, "emoji": str(emoji) if emoji else "",
                      "description": str(item.get("description") or "").strip()[:100]})
    if not roles:
        raise ConfigError("Add at least one role.")
    if len(roles) > (20 if kind == "reactions" else 25):
        raise ConfigError("A menu holds at most 25 roles (20 for reactions).")
    if len({r["role_id"] for r in roles}) != len(roles):
        raise ConfigError("Each role can only be in the menu once.")
    return {"guild_id": guild.id, "channel_id": ch.id, "kind": kind, "mode": mode, "title": title,
            "description": description, "color": color, "roles": roles}


def build_message(menu: dict, guild: discord.Guild) -> tuple[discord.Embed, discord.ui.View | None]:
    desc = menu["description"]
    if menu["kind"] == "reactions":
        lines = [f"{r['emoji']}  {guild.get_role(r['role_id']).mention if guild.get_role(r['role_id']) else r['label']}"
                 for r in menu["roles"]]
        desc = (desc + "\n\n" if desc else "") + "\n".join(lines)
    note = {"unique": "You can have one of these at a time.",
            "verify": "Roles from this menu stay once you have them."}.get(menu["mode"])
    e = discord.Embed(title=menu["title"], description=desc[:4096] or None, colour=templates.color(menu["color"]))
    if note:
        e.set_footer(text=note)
    if menu["kind"] == "reactions":
        return e, None
    view = discord.ui.View(timeout=None)
    if menu["kind"] == "buttons":
        for r in menu["roles"]:
            view.add_item(discord.ui.Button(label=r["label"], emoji=r["emoji"] or None,
                                            style=discord.ButtonStyle.secondary,
                                            custom_id=f"rr:{menu['id']}:{r['role_id']}"))
    else:
        options = [discord.SelectOption(label=r["label"], value=str(r["role_id"]), emoji=r["emoji"] or None,
                                        description=r["description"] or None) for r in menu["roles"]]
        view.add_item(discord.ui.Select(custom_id=f"rr:{menu['id']}", placeholder="Pick your roles",
                                        min_values=0, max_values=1 if menu["mode"] == "unique" else len(options),
                                        options=options))
    return e, view


class ReactionRoles(commands.Cog):
    def __init__(self, bot: "MusicBot"):
        self.bot = bot
        self.menus: dict[int, dict] = {}       # id -> menu
        self.by_message: dict[int, dict] = {}  # message id -> menu (for reactions)

    async def cog_load(self) -> None:
        for m in await self.bot.db.menus():
            self._index(m)

    def _index(self, m: dict) -> None:
        old = self.menus.get(m["id"])
        if old and old.get("message_id"):
            self.by_message.pop(old["message_id"], None)
        self.menus[m["id"]] = m
        if m.get("message_id"):
            self.by_message[m["message_id"]] = m

    def _forget(self, menu_id: int) -> None:
        m = self.menus.pop(menu_id, None)
        if m and m.get("message_id"):
            self.by_message.pop(m["message_id"], None)

    # ---- dashboard -----------------------------------------------------------------------------

    async def save(self, guild: discord.Guild, data: dict, menu_id: int | None = None) -> dict:
        menu = validate_menu(self.bot, guild, data)
        old = self.menus.get(menu_id) if menu_id else None
        if menu_id and (old is None or old["guild_id"] != guild.id):
            raise ConfigError("That menu doesn't exist any more.")
        menu["id"] = menu_id or await self.bot.db.save_menu({**menu, "message_id": None})
        menu["message_id"] = None
        message = None
        if old and old.get("message_id") and old["channel_id"] == menu["channel_id"]:
            try:
                message = await guild.get_channel(old["channel_id"]).fetch_message(old["message_id"])
            except (discord.HTTPException, AttributeError):
                message = None
        elif old and old.get("message_id"):
            await self._delete_message(guild, old)
        embed, view = build_message(menu, guild)
        channel = guild.get_channel(menu["channel_id"])
        try:
            if message:
                await message.edit(embed=embed, view=view)
            else:
                message = await channel.send(embed=embed, view=view)
        except discord.HTTPException as e:
            if not menu_id:
                await self.bot.db.delete_menu(menu["id"])
            raise ConfigError(f"Discord refused the menu: {e.text or e}") from e
        menu["message_id"] = message.id
        if menu["kind"] == "reactions":
            await self._sync_reactions(message, menu)
        elif old and old["kind"] == "reactions":
            try:
                await message.clear_reactions()
            except discord.HTTPException:
                pass
        await self.bot.db.save_menu(menu)
        self._index(menu)
        log.info("guild %s: reaction-role menu %s saved (%s, %d roles)", guild.id, menu["id"], menu["kind"],
                 len(menu["roles"]))
        return menu

    async def delete(self, guild: discord.Guild, menu_id: int) -> None:
        m = self.menus.get(menu_id)
        if not m or m["guild_id"] != guild.id:
            raise ConfigError("That menu doesn't exist any more.")
        await self._delete_message(guild, m)
        await self.bot.db.delete_menu(menu_id)
        self._forget(menu_id)

    async def _delete_message(self, guild: discord.Guild, m: dict) -> None:
        ch = guild.get_channel(m["channel_id"])
        if ch and m.get("message_id"):
            try:
                await ch.get_partial_message(m["message_id"]).delete()
            except discord.HTTPException:
                pass

    async def _sync_reactions(self, message: discord.Message, menu: dict) -> None:
        wanted = [r["emoji"] for r in menu["roles"]]
        keys = {_emoji_key(e) for e in wanted}
        for reaction in message.reactions:
            if reaction.me and _emoji_key(reaction.emoji) not in keys:
                try:
                    await message.remove_reaction(reaction.emoji, message.guild.me)
                except discord.HTTPException:
                    pass
        have = {_emoji_key(r.emoji) for r in message.reactions if r.me}
        for e in wanted:
            if _emoji_key(e) not in have:
                try:
                    await message.add_reaction(e)
                except discord.HTTPException as ex:
                    log.warning("menu %s: can't react with %s: %s", menu["id"], e, ex)

    # ---- handing out roles ---------------------------------------------------------------------

    async def _apply(self, member: discord.Member, menu: dict, add: set[int], remove: set[int]) -> tuple[list, list]:
        guild = member.guild
        if menu["mode"] == "verify":
            remove = set()
        add_roles = [r for r in (guild.get_role(i) for i in add) if r and r not in member.roles and not role_problem(guild, r)]
        rem_roles = [r for r in (guild.get_role(i) for i in remove) if r and r in member.roles and not role_problem(guild, r)]
        if add_roles:
            await member.add_roles(*add_roles, reason=f"Reaction roles (menu {menu['id']})")
        if rem_roles:
            await member.remove_roles(*rem_roles, reason=f"Reaction roles (menu {menu['id']})")
        return add_roles, rem_roles

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("rr:"):
            return
        parts = cid.split(":")
        menu = self.menus.get(int(parts[1])) if parts[1].isdigit() else None
        member = interaction.user
        if menu is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("This menu was removed.", ephemeral=True)
        menu_roles = {r["role_id"] for r in menu["roles"]}
        have = {r.id for r in member.roles} & menu_roles
        if len(parts) == 3:  # a button
            role_id = int(parts[2])
            if role_id in have:
                add, remove = set(), {role_id}
            else:
                add, remove = {role_id}, (have - {role_id} if menu["mode"] == "unique" else set())
        else:  # the dropdown
            chosen = {int(v) for v in (interaction.data or {}).get("values", [])} & menu_roles
            add, remove = chosen - have, (menu_roles - chosen) & have
        try:
            added, removed = await self._apply(member, menu, add, remove)
        except discord.HTTPException as e:
            log.warning("menu %s: role change for %s failed: %s", menu["id"], member, e)
            return await interaction.response.send_message(
                "The bot couldn't change your roles. Ask a moderator to check the bot's role position.", ephemeral=True)
        bits = []
        if added:
            bits.append("Added " + ", ".join(r.mention for r in added))
        if removed:
            bits.append("Removed " + ", ".join(r.mention for r in removed))
        if not bits:
            bits.append("You keep the roles from this menu." if menu["mode"] == "verify" and remove
                        else "Nothing changed.")
        await interaction.response.send_message(". ".join(bits) + ".", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        menu = self.by_message.get(payload.message_id)
        if not menu or not payload.guild_id or (self.bot.user and payload.user_id == self.bot.user.id):
            return
        member = payload.member
        role_id = next((r["role_id"] for r in menu["roles"] if _emoji_key(r["emoji"]) == _emoji_key(payload.emoji)), None)
        if member is None or member.bot or role_id is None:
            return
        others = ({r["role_id"] for r in menu["roles"]} - {role_id}) if menu["mode"] == "unique" else set()
        try:
            await self._apply(member, menu, {role_id}, others)
        except discord.HTTPException as e:
            log.warning("menu %s: reaction role for %s failed: %s", menu["id"], member, e)
            return
        if others:  # take back their other reactions so the menu shows what they have
            ch = member.guild.get_channel(payload.channel_id)
            msg = ch.get_partial_message(payload.message_id) if ch else None
            for r in menu["roles"]:
                if r["role_id"] in others and msg:
                    try:
                        await msg.remove_reaction(r["emoji"], member)
                    except discord.HTTPException:
                        pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        menu = self.by_message.get(payload.message_id)
        if not menu or not payload.guild_id or menu["mode"] == "verify":
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        role_id = next((r["role_id"] for r in menu["roles"] if _emoji_key(r["emoji"]) == _emoji_key(payload.emoji)), None)
        if member is None or member.bot or role_id is None:
            return
        try:
            await self._apply(member, menu, set(), {role_id})
        except discord.HTTPException as e:
            log.warning("menu %s: removing reaction role from %s failed: %s", menu["id"], member, e)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        menu = self.by_message.get(payload.message_id)
        if menu:  # someone deleted the menu message in Discord
            menu["message_id"] = None
            await self.bot.db.save_menu(menu)
            self._index(menu)
            self.by_message.pop(payload.message_id, None)
