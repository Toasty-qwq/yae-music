"""The dashboard's view of the bot: which servers someone may manage, a server's channels and
roles, and saving its settings. Served on the bot's local API; the panel calls it with the signed-in
person's Discord user id (``uid``), or ``owner`` for the panel owner, and the bot checks on Discord
that that person really has Manage Server in the server, on every request."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from aiohttp import web

from .guildconfig import ConfigError, role_problem
from .templates import PLACEHOLDERS

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.dashboard")

NEEDED = {  # permission -> what it is for, in words
    "manage_roles": "autorole and reaction roles",
    "kick_members": "/kick",
    "ban_members": "/ban and /unban",
    "moderate_members": "/timeout",
    "manage_messages": "/purge, and tidying reaction menus",
    "view_audit_log": "naming who did a ban or kick in the logs",
    "add_reactions": "reaction-role menus",
    "embed_links": "every message the bot sends",
}


async def member_of(guild: discord.Guild, uid: str) -> discord.Member | None:
    if not uid.isdigit():
        return None
    m = guild.get_member(int(uid))
    if m is None:
        try:
            m = await guild.fetch_member(int(uid))
        except discord.HTTPException:
            return None
    return m


async def can_manage(guild: discord.Guild, uid: str) -> bool:
    if uid == "owner":
        return True
    m = await member_of(guild, uid)
    return bool(m and (m.guild_permissions.manage_guild or guild.owner_id == m.id))


def guild_card(g: discord.Guild) -> dict:
    return {"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None,
            "members": g.member_count}


def routes(bot: "MusicBot") -> list[web.RouteDef]:
    async def guild_or_403(request: web.Request) -> discord.Guild:
        uid = request.query.get("uid", "")
        guild = bot.get_guild(int(request.match_info["gid"]))
        if guild is None:
            raise web.HTTPNotFound(text='{"error":"The bot isn\'t in that server."}', content_type="application/json")
        if not await can_manage(guild, uid):
            raise web.HTTPForbidden(text='{"error":"You need the Manage Server permission in that server."}',
                                    content_type="application/json")
        return guild

    async def guilds(request: web.Request):
        uid = request.query.get("uid", "")
        out = [guild_card(g) for g in sorted(bot.guilds, key=lambda g: g.name.lower()) if await can_manage(g, uid)]
        return web.json_response({"guilds": out, "intents": intents()})

    def intents() -> dict:
        return {"members": bot.intents.members, "message_content": bot.intents.message_content}

    async def guild_detail(request: web.Request):
        g = await guild_or_403(request)
        me = g.me
        channels = []
        for ch in sorted(g.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
            p = ch.permissions_for(me)
            channels.append({"id": str(ch.id), "name": ch.name, "category": ch.category.name if ch.category else "",
                             "can_send": p.view_channel and p.send_messages and p.embed_links})
        roles = [{"id": str(r.id), "name": r.name, "color": str(r.colour) if r.colour.value else None,
                  "problem": role_problem(g, r)}
                 for r in reversed(g.roles) if not r.is_default()]
        emojis = [{"id": str(e.id), "name": e.name, "text": str(e), "url": str(e.url)} for e in g.emojis]
        missing = [{"perm": k, "for": v} for k, v in NEEDED.items() if not getattr(me.guild_permissions, k)]
        cfg = await bot.configs.get(g.id)
        return web.json_response({
            "guild": guild_card(g), "channels": channels, "roles": roles, "emojis": emojis,
            "bot_role": me.top_role.name, "missing": missing, "intents": intents(),
            "config": {k: _ids_to_str(v) for k, v in cfg.items()},
            "menus": [_ids_to_str(m) for m in await bot.db.menus(g.id)],
            "placeholders": PLACEHOLDERS,
        })

    async def save_config(request: web.Request):
        g = await guild_or_403(request)
        body = await request.json()
        try:
            clean = await bot.configs.save_section(g, body.get("section", ""), body.get("data") or {})
        except ConfigError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        log.info("guild %s: %s settings saved from the dashboard (by %s)", g.id, body.get("section"),
                 request.query.get("uid"))
        return web.json_response({"ok": True, "data": _ids_to_str(clean)})

    async def save_menu(request: web.Request):
        g = await guild_or_403(request)
        mid = int(request.match_info["mid"]) if "mid" in request.match_info else None
        try:
            menu = await bot.get_cog("ReactionRoles").save(g, await request.json(), mid)
        except ConfigError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "menu": _ids_to_str(menu)})

    async def delete_menu(request: web.Request):
        g = await guild_or_403(request)
        try:
            await bot.get_cog("ReactionRoles").delete(g, int(request.match_info["mid"]))
        except ConfigError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def cases(request: web.Request):
        g = await guild_or_403(request)
        offset = max(0, int(request.query.get("offset", 0)))
        user = request.query.get("user", "")
        rows, total = await bot.db.cases(g.id, user_id=int(user) if user.isdigit() else None, limit=25, offset=offset)
        return web.json_response({"cases": [_ids_to_str(c) for c in rows], "total": total})

    async def test(request: web.Request):
        g = await guild_or_403(request)
        section = (await request.json()).get("section")
        who = await member_of(g, request.query.get("uid", "")) or g.me
        cog = bot.get_cog("Welcome")
        if section == "welcome":
            problem = await cog.send_welcome(who, test=True)
        elif section == "farewell":
            problem = await cog.send_farewell(who, g, test=True)
        else:
            return web.json_response({"ok": False, "error": "Nothing to test there."}, status=400)
        if problem:
            return web.json_response({"ok": False, "error": problem}, status=400)
        return web.json_response({"ok": True})

    return [
        web.get("/dash/guilds", guilds),
        web.get("/dash/guild/{gid}", guild_detail),
        web.post("/dash/guild/{gid}/config", save_config),
        web.post("/dash/guild/{gid}/menus", save_menu),
        web.put("/dash/guild/{gid}/menus/{mid}", save_menu),
        web.delete("/dash/guild/{gid}/menus/{mid}", delete_menu),
        web.get("/dash/guild/{gid}/cases", cases),
        web.post("/dash/guild/{gid}/test", test),
    ]


def _ids_to_str(value):
    """Discord ids are bigger than JavaScript numbers can hold exactly: send every one as a string."""
    if isinstance(value, dict):
        return {k: _ids_to_str(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ids_to_str(v) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and value > 2**40:
        return str(value)
    return value
