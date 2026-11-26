"""The dashboard's view of the bot: which servers someone may manage, a server's channels and
roles, and saving its settings. Served on the bot's local API; the panel calls it with the signed-in
person's Discord user id (``uid``), or ``owner`` for the panel owner, and the bot checks on Discord
that that person really has Administrator (or Manage Server, the owner's choice) in the server, on every request."""
from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from typing import TYPE_CHECKING

import discord
from aiohttp import web

from . import messages
from .guildconfig import ConfigError, _channel, role_problem
from .templates import PLACEHOLDERS, Ctx

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
    "manage_channels": "the VC maker and tickets",
    "move_members": "moving people into their new voice channel",
    "create_private_threads": "thread tickets",
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


PERM_NAMES = {"administrator": "Administrator", "manage_guild": "Manage Server"}


async def can_manage(guild: discord.Guild, uid: str, need: str = "administrator") -> bool:
    """``need`` is the panel's dash_permission: administrator, or manage_guild (which admins have too)."""
    if uid == "owner":
        return True
    m = await member_of(guild, uid)
    if not m:
        return False
    p = m.guild_permissions
    return guild.owner_id == m.id or p.administrator or (need == "manage_guild" and p.manage_guild)


def guild_card(g: discord.Guild) -> dict:
    return {"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None,
            "members": g.member_count}


def routes(bot: "MusicBot") -> list[web.RouteDef]:
    async def guild_or_403(request: web.Request) -> discord.Guild:
        uid, need = request.query.get("uid", ""), request.query.get("need", "administrator")
        guild = bot.get_guild(int(request.match_info["gid"]))
        if guild is None:
            raise web.HTTPNotFound(text='{"error":"The bot isn\'t in that server."}', content_type="application/json")
        if not await can_manage(guild, uid, need):
            raise web.HTTPForbidden(text=json.dumps({"error": f"You need the {PERM_NAMES.get(need, need)} permission "
                                                              "in that server."}), content_type="application/json")
        return guild

    async def guilds(request: web.Request):
        uid, need = request.query.get("uid", ""), request.query.get("need", "administrator")
        out = [guild_card(g) for g in sorted(bot.guilds, key=lambda g: g.name.lower()) if await can_manage(g, uid, need)]
        # of the person's own servers (from their Discord sign-in), the ones the bot is in
        also = {x for x in request.query.get("also", "").split(",") if x.isdigit()}
        present = [str(g.id) for g in bot.guilds if str(g.id) in also]
        return web.json_response({"guilds": out, "present": present, "intents": intents()})

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
        voice = [{"id": str(v.id), "name": v.name, "category": v.category.name if v.category else ""}
                 for v in sorted(g.voice_channels, key=lambda c: (c.category.position if c.category else -1, c.position))]
        categories = [{"id": str(c.id), "name": c.name} for c in sorted(g.categories, key=lambda c: c.position)]
        tk = bot.get_cog("Tickets")
        panels = [_ids_to_str({k: v for k, v in p.items()}) for p in tk.panels.values() if p["guild_id"] == g.id] if tk else []
        vm = bot.get_cog("VoiceMaster")
        temp = [{"id": str(cid), "name": (g.get_channel(cid).name if g.get_channel(cid) else "?"), "owner_id": str(r["owner_id"] or ""),
                 "members": len(g.get_channel(cid).members) if g.get_channel(cid) else 0}
                for cid, r in (vm.owned.items() if vm else []) if r["guild_id"] == g.id]
        forums = []
        for f in sorted(g.forums, key=lambda c: c.position):
            p = f.permissions_for(me)
            forums.append({"id": str(f.id), "name": f.name, "can_post": p.view_channel and p.send_messages_in_threads,
                           "tags": [{"id": str(t.id), "name": t.name, "emoji": str(t.emoji) if t.emoji else "",
                                     "moderated": t.moderated} for t in f.available_tags]})
        missing = [{"perm": k, "for": v} for k, v in NEEDED.items() if not getattr(me.guild_permissions, k)]
        cfg = await bot.configs.get(g.id)
        return web.json_response({
            "guild": guild_card(g), "channels": channels, "roles": roles, "emojis": emojis, "forums": forums,
            "voice_channels": voice, "categories": categories, "ticket_panels": panels, "temp_channels": temp,
            "bitrate_limit": int(g.bitrate_limit // 1000),
            "bot_role": me.top_role.name, "missing": missing, "intents": intents(),
            "config": {k: _ids_to_str(v) for k, v in cfg.items()},
            "menus": [_ids_to_str(m) for m in await bot.db.menus(g.id)],
            "tags": [_ids_to_str(t) for t in await bot.get_cog("Tags").listing(g.id)],
            "posts": [_ids_to_str(_post(p)) for p in await bot.db.all("SELECT * FROM posts WHERE guild_id=? ORDER BY id", (g.id,))],
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

    async def save_tag(request: web.Request):
        g = await guild_or_403(request)
        body = await request.json()
        old = request.match_info.get("name")
        uid = request.query.get("uid", "")
        try:
            tag = await bot.get_cog("Tags").save(g.id, body.get("name", ""), body.get("message"),
                                                 int(uid) if uid.isdigit() else g.me.id, old_name=old)
        except ConfigError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "tag": _ids_to_str(tag)})

    async def delete_tag(request: web.Request):
        g = await guild_or_403(request)
        await bot.get_cog("Tags").delete(g.id, request.match_info["name"])
        return web.json_response({"ok": True})

    async def save_post(request: web.Request):
        """Post (or edit) a message from the dashboard's Messages tab."""
        g = await guild_or_403(request)
        body = await request.json()
        pid = request.match_info.get("pid")
        try:
            channel_id = _channel(g, body.get("channel_id"), "the message", required=True)
            msg = messages.validate(body.get("message"), "The message")
        except ConfigError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        name = str(body.get("name") or "").strip()[:80] or "Message"
        who = await member_of(g, request.query.get("uid", "")) or g.me
        ch = g.get_channel(channel_id)
        ctx = Ctx(g, who, ch)
        old = await bot.db.one("SELECT * FROM posts WHERE id=? AND guild_id=?", (int(pid), g.id)) if pid else None
        if pid and not old:
            return web.json_response({"ok": False, "error": "That message isn't on the list any more."}, status=404)
        try:
            sent = None
            if old and old["channel_id"] == channel_id and old.get("message_id"):
                try:
                    sent = await messages.edit(await ch.fetch_message(old["message_id"]), msg, ctx)
                except discord.NotFound:
                    sent = None
            elif old and old.get("message_id"):
                old_ch = g.get_channel(old["channel_id"])
                if old_ch:
                    try:
                        await old_ch.get_partial_message(old["message_id"]).delete()
                    except discord.HTTPException:
                        pass
            if sent is None:
                sent = await messages.send(ch, msg, ctx)
        except discord.HTTPException as e:
            return web.json_response({"ok": False, "error": f"Discord refused the message: {e.text or e}"}, status=400)
        if old:
            await bot.db.run("UPDATE posts SET channel_id=?, message_id=?, name=?, message=? WHERE id=?",
                             (channel_id, sent.id, name, json.dumps(msg), old["id"]))
            post_id = old["id"]
        else:
            post_id = await bot.db.run("INSERT INTO posts(guild_id, channel_id, message_id, name, message, created_at) "
                                       "VALUES(?,?,?,?,?,?)", (g.id, channel_id, sent.id, name, json.dumps(msg), time.time()))
        row = await bot.db.one("SELECT * FROM posts WHERE id=?", (post_id,))
        log.info("guild %s: dashboard message %r posted in #%s", g.id, name, ch)
        return web.json_response({"ok": True, "post": _ids_to_str(_post(row)), "jump": sent.jump_url})

    async def delete_post(request: web.Request):
        g = await guild_or_403(request)
        row = await bot.db.one("SELECT * FROM posts WHERE id=? AND guild_id=?", (int(request.match_info["pid"]), g.id))
        if row:
            ch = g.get_channel(row["channel_id"])
            if ch and row.get("message_id"):
                try:
                    await ch.get_partial_message(row["message_id"]).delete()
                except discord.HTTPException:
                    pass
            await bot.db.run("DELETE FROM posts WHERE id=?", (row["id"],))
        return web.json_response({"ok": True})

    async def upload(request: web.Request):
        g = await guild_or_403(request)
        body = await request.json()
        try:
            data = base64.b64decode(body.get("data", ""), validate=True)
            token = messages.save_media(g.id, str(body.get("filename") or "image.png"), data)
        except (binascii.Error, ValueError) as e:
            msg = str(e) if isinstance(e, ConfigError) else "That file didn't arrive in one piece. Try again."
            return web.json_response({"ok": False, "error": msg}, status=400)
        log.info("guild %s: uploaded %s (%d KB)", g.id, token, len(data) // 1024)
        return web.json_response({"ok": True, "token": token})

    async def media(request: web.Request):
        g = await guild_or_403(request)
        path = messages.media_path(g.id, request.match_info["name"])
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    async def save_panel(request: web.Request):
        g = await guild_or_403(request)
        pid = int(request.match_info["pid"]) if "pid" in request.match_info else None
        try:
            row = await bot.get_cog("Tickets").save_panel(g, await request.json(), pid)
        except ConfigError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True, "panel": _ids_to_str(row)})

    async def delete_panel(request: web.Request):
        g = await guild_or_403(request)
        try:
            await bot.get_cog("Tickets").delete_panel(g, int(request.match_info["pid"]))
        except ConfigError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def ticket_list(request: web.Request):
        g = await guild_or_403(request)
        status = request.query.get("status", "open")
        rows = await bot.db.all("SELECT * FROM tickets WHERE guild_id=? AND status=? ORDER BY number DESC LIMIT 100",
                                (g.id, status if status in ("open", "closed", "deleted") else "open"))
        for r in rows:
            m = g.get_member(r["owner_id"])
            r["owner_name"] = str(m) if m else str(r["owner_id"])
            r.pop("answers", None)
        return web.json_response({"tickets": _ids_to_str(rows)})

    return [
        web.get("/dash/guilds", guilds),
        web.get("/dash/guild/{gid}", guild_detail),
        web.post("/dash/guild/{gid}/config", save_config),
        web.post("/dash/guild/{gid}/menus", save_menu),
        web.put("/dash/guild/{gid}/menus/{mid}", save_menu),
        web.delete("/dash/guild/{gid}/menus/{mid}", delete_menu),
        web.get("/dash/guild/{gid}/cases", cases),
        web.post("/dash/guild/{gid}/test", test),
        web.post("/dash/guild/{gid}/tags", save_tag),
        web.put("/dash/guild/{gid}/tags/{name}", save_tag),
        web.delete("/dash/guild/{gid}/tags/{name}", delete_tag),
        web.post("/dash/guild/{gid}/posts", save_post),
        web.put("/dash/guild/{gid}/posts/{pid}", save_post),
        web.delete("/dash/guild/{gid}/posts/{pid}", delete_post),
        web.post("/dash/guild/{gid}/media", upload),
        web.get("/dash/guild/{gid}/media/{name}", media),
        web.post("/dash/guild/{gid}/ticketpanels", save_panel),
        web.put("/dash/guild/{gid}/ticketpanels/{pid}", save_panel),
        web.delete("/dash/guild/{gid}/ticketpanels/{pid}", delete_panel),
        web.get("/dash/guild/{gid}/tickets", ticket_list),
    ]


def _post(row: dict) -> dict:
    return {**row, "message": json.loads(row["message"])}


def _ids_to_str(value):
    """Discord ids are bigger than JavaScript numbers can hold exactly: send every one as a string."""
    if isinstance(value, dict):
        return {k: _ids_to_str(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ids_to_str(v) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and value > 2**40:
        return str(value)
    return value
