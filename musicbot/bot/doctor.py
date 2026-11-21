"""What the panel's check-up asks the running bot: permissions, roles it hands out, voice connections."""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

import discord

from .dashboard import NEEDED
from .guildconfig import role_problem

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.doctor")


async def _given_roles(bot: "MusicBot", guild: discord.Guild) -> list[tuple[str, int]]:
    """(what gives it, role id) for every role the bot is set up to hand out in ``guild``."""
    cfg = await bot.configs.get(guild.id)
    out = []
    if cfg["autorole"]["enabled"]:
        out += [("Autorole", r) for r in cfg["autorole"]["role_ids"] + cfg["autorole"]["bot_role_ids"]]
    if cfg["forumroles"]["enabled"]:
        out += [("Forum roles", r["role_id"]) for r in cfg["forumroles"]["rules"]]
    vm = cfg["voicemaster"]
    if vm["enabled"] and vm.get("temp_role_id"):
        out.append(("VC maker", vm["temp_role_id"]))
    for menu in await bot.db.all("SELECT title, roles FROM rr_menus WHERE guild_id=?", (guild.id,)):
        out += [(f"Role menu \"{menu['title']}\"", r["role_id"]) for r in json.loads(menu["roles"])]
    return out


async def report(bot: "MusicBot") -> dict:
    guilds = []
    for g in bot.guilds:
        me = g.me
        missing = [{"perm": k, "for": v} for k, v in NEEDED.items() if not getattr(me.guild_permissions, k)]
        roles = []
        for what, rid in await _given_roles(bot, g):
            role = g.get_role(int(rid))
            problem = "a role it gives was deleted" if role is None else role_problem(g, role)
            if problem:
                roles.append(f"{what}: {problem}")
        if missing or roles:
            guilds.append({"id": str(g.id), "name": g.name, "missing": missing, "roles": sorted(set(roles))})
    return {"ready": bot.is_ready(), "guilds": guilds, "voice": _stuck_voice(bot)}


def _stuck_voice(bot: "MusicBot") -> dict:
    """Voice connections with no player behind them, and players whose connection is gone."""
    orphans = [g for g in bot.guilds if g.voice_client and g.id not in bot.players]
    return {"orphans": len(orphans), "dead": len(_dead(bot))}


def _dead(bot: "MusicBot") -> list:
    """Players without a voice connection (a minute old at least: /play makes one just before joining)."""
    return [p for p in bot.players.values()
            if (not p.vc or not p.vc.is_connected()) and time.monotonic() - p.idle_since > 60]


async def fix_voice(bot: "MusicBot") -> int:
    fixed = 0
    for g in bot.guilds:
        if g.voice_client and g.id not in bot.players:
            try:
                await g.voice_client.disconnect(force=True)
            except Exception:  # noqa: BLE001
                log.debug("disconnect failed", exc_info=True)
            fixed += 1
    for p in _dead(bot):
        await p.stop()
        fixed += 1
    if fixed:
        log.info("Check-up: cleared %d stuck voice connection(s).", fixed)
    return fixed
