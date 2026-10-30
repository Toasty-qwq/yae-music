"""Tags: saved messages anyone can post with /tag show <name> (rules, links, FAQ answers...).
Simple text tags can be made with /tag create; tags with embeds and images are made on the dashboard."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from . import messages, ui
from .errors import ConfigError
from .templates import Ctx

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.tags")
NAME = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,31}$")
MAX_TAGS = 300


def clean_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not NAME.match(n):
        raise ConfigError("A tag name is 1-32 letters, numbers, spaces, - or _.")
    return n


class Tags(commands.Cog):
    tag = app_commands.Group(name="tag", description="Saved messages you can post by name", guild_only=True)

    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    # ---- storage (also used by the dashboard) ----------------------------------------------

    async def get(self, guild_id: int, name: str) -> dict | None:
        row = await self.bot.db.one("SELECT * FROM tags WHERE guild_id=? AND name=?", (guild_id, name))
        if row:
            row["message"] = json.loads(row["message"])
        return row

    async def listing(self, guild_id: int) -> list[dict]:
        rows = await self.bot.db.all("SELECT * FROM tags WHERE guild_id=? ORDER BY name", (guild_id,))
        for r in rows:
            r["message"] = json.loads(r["message"])
        return rows

    async def save(self, guild_id: int, name: str, message: dict, owner_id: int, *, old_name: str | None = None) -> dict:
        name = clean_name(name)
        message = messages.validate(message, f"The tag {name!r}")
        existing = await self.get(guild_id, name)
        if old_name and old_name != name:
            if existing:
                raise ConfigError(f"There's already a tag called {name!r}.")
            await self.bot.db.run("UPDATE tags SET name=? WHERE guild_id=? AND name=?", (name, guild_id, old_name))
            existing = await self.get(guild_id, name)
        if existing:
            await self.bot.db.run("UPDATE tags SET message=? WHERE guild_id=? AND name=?",
                                  (json.dumps(message), guild_id, name))
        else:
            count = (await self.bot.db.one("SELECT COUNT(*) AS n FROM tags WHERE guild_id=?", (guild_id,)))["n"]
            if count >= MAX_TAGS:
                raise ConfigError(f"This server already has {MAX_TAGS} tags. Delete some first.")
            await self.bot.db.run("INSERT INTO tags(guild_id, name, message, owner_id, created_at) VALUES(?,?,?,?,?)",
                                  (guild_id, name, json.dumps(message), owner_id, time.time()))
        return await self.get(guild_id, name)

    async def delete(self, guild_id: int, name: str) -> bool:
        if not await self.get(guild_id, name):
            return False
        await self.bot.db.run("DELETE FROM tags WHERE guild_id=? AND name=?", (guild_id, name))
        return True

    # ---- commands ----------------------------------------------------------------------------

    async def names(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.bot.db.all("SELECT name FROM tags WHERE guild_id=? AND name LIKE ? ORDER BY uses DESC, name LIMIT 25",
                                     (interaction.guild_id, f"%{current.lower()}%"))
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

    @tag.command(name="show", description="Post a tag")
    @app_commands.describe(name="Which tag")
    @app_commands.autocomplete(name=names)
    async def show(self, interaction: discord.Interaction, name: str):
        t = await self.get(interaction.guild_id, name.strip().lower())
        if not t:
            return await interaction.response.send_message(embed=ui.error(f"There's no tag called {name!r}. /tag list shows them all."),
                                                           ephemeral=True)
        await self.bot.db.run("UPDATE tags SET uses=uses+1 WHERE guild_id=? AND name=?", (interaction.guild_id, t["name"]))
        kw = messages.build(t["message"], Ctx(interaction.guild, interaction.user, interaction.channel))
        await interaction.response.send_message(**{k: v for k, v in kw.items() if v is not None})

    @tag.command(name="list", description="Every tag in this server")
    async def list_cmd(self, interaction: discord.Interaction):
        rows = await self.listing(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(embed=ui.ok("This server has no tags yet. Make one with /tag create."),
                                                           ephemeral=True)
        text = ", ".join(f"`{r['name']}`" for r in rows)
        await interaction.response.send_message(embed=discord.Embed(title=f"{len(rows)} tags", description=text[:4000],
                                                                    colour=ui.COLOR), ephemeral=True)

    @tag.command(name="info", description="Who made a tag and how often it's used")
    @app_commands.autocomplete(name=names)
    async def info(self, interaction: discord.Interaction, name: str):
        t = await self.get(interaction.guild_id, name.strip().lower())
        if not t:
            return await interaction.response.send_message(embed=ui.error(f"There's no tag called {name!r}."), ephemeral=True)
        e = discord.Embed(title=f"Tag: {t['name']}", colour=ui.COLOR)
        e.add_field(name="Made by", value=f"<@{t['owner_id']}>")
        e.add_field(name="Used", value=f"{t['uses']} times")
        e.add_field(name="Made", value=f"<t:{int(t['created_at'])}:R>")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @tag.command(name="create", description="Make a text tag (tags with embeds are made on the dashboard)")
    @app_commands.describe(name="Its name", text="What it posts (placeholders like {user} work)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def create(self, interaction: discord.Interaction, name: str, text: str):
        try:
            if await self.get(interaction.guild_id, clean_name(name)):
                raise ConfigError(f"There's already a tag called {clean_name(name)!r}. Use /tag edit to change it.")
            t = await self.save(interaction.guild_id, name, messages.message(text), interaction.user.id)
        except ConfigError as e:
            return await interaction.response.send_message(embed=ui.error(str(e)), ephemeral=True)
        await interaction.response.send_message(embed=ui.ok(f"Tag `{t['name']}` made. Post it with /tag show."), ephemeral=True)

    @tag.command(name="edit", description="Change a tag's text")
    @app_commands.autocomplete(name=names)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def edit(self, interaction: discord.Interaction, name: str, text: str):
        t = await self.get(interaction.guild_id, name.strip().lower())
        if not t:
            return await interaction.response.send_message(embed=ui.error(f"There's no tag called {name!r}."), ephemeral=True)
        msg = t["message"]
        msg["content"] = text
        try:
            await self.save(interaction.guild_id, t["name"], msg, t["owner_id"])
        except ConfigError as e:
            return await interaction.response.send_message(embed=ui.error(str(e)), ephemeral=True)
        await interaction.response.send_message(embed=ui.ok(f"Tag `{t['name']}` changed."), ephemeral=True)

    @tag.command(name="delete", description="Delete a tag")
    @app_commands.autocomplete(name=names)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def delete_cmd(self, interaction: discord.Interaction, name: str):
        ok = await self.delete(interaction.guild_id, name.strip().lower())
        await interaction.response.send_message(
            embed=ui.ok(f"Tag `{name}` deleted.") if ok else ui.error(f"There's no tag called {name!r}."), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        text = ("You need the Manage Messages permission to change tags."
                if isinstance(error, app_commands.MissingPermissions) else "Something went wrong with that tag.")
        if not isinstance(error, app_commands.MissingPermissions):
            log.exception("tag command failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(embed=ui.error(text), ephemeral=True)
