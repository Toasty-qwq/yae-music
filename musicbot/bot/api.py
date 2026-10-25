"""A tiny HTTP API on 127.0.0.1 so the panel can see and control the bot.

Only started when the panel launched the bot (it passes BOT_API_PORT and a random BOT_API_TOKEN).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from typing import TYPE_CHECKING

from aiohttp import web

from .player import LoopMode

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.api")
STARTED = time.time()


class ControlApi:
    def __init__(self, runner: web.AppRunner):
        self.runner = runner

    async def close(self) -> None:
        await self.runner.cleanup()


async def start(bot: "MusicBot") -> ControlApi | None:
    port = os.environ.get("BOT_API_PORT")
    token = os.environ.get("BOT_API_TOKEN", "")
    if not port or not token:
        return None

    background: set[asyncio.Task] = set()  # keeps the shutdown task referenced

    @web.middleware
    async def auth(request: web.Request, handler):
        if not hmac.compare_digest(request.headers.get("X-Token", ""), token):
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def state(_):
        user = bot.user
        return web.json_response({
            "ready": bot.is_ready(),
            "user": str(user) if user else None,
            "id": str(user.id) if user else None,
            "avatar": user.display_avatar.url if user else None,
            "guilds": len(bot.guilds),
            "latency_ms": round(bot.latency * 1000) if bot.is_ready() and bot.latency == bot.latency else None,
            "started": STARTED,
            "players": [p.snapshot() for p in bot.players.values()],
        })

    async def control(request: web.Request):
        body = await request.json()
        action = body.get("action")
        if action == "shutdown":
            async def close_soon():
                await asyncio.sleep(0.1)  # let this response go out first
                await bot.close()
            background.add(asyncio.create_task(close_soon()))
            return web.json_response({"ok": True})
        try:
            p = bot.players[int(body.get("guild", 0))]
        except (KeyError, ValueError):
            return web.json_response({"ok": False, "error": "That server has no player any more."}, status=404)
        value = body.get("value")
        if action == "skip":
            p.skip()
        elif action == "toggle":
            if not p.pause():
                p.resume()
        elif action == "stop":
            await p.stop("⏹️ Stopped from the control panel.")
        elif action == "shuffle":
            p.shuffle()
        elif action == "clear":
            p.queue.clear()
        elif action == "volume":
            p.set_volume(int(value))
        elif action == "loop":
            p.loop = LoopMode(value)
        elif action == "remove":
            p.queue.pop(int(value))
        else:
            return web.json_response({"ok": False, "error": f"unknown action {action!r}"}, status=400)
        log.info("guild %s: %s from the panel%s", p.guild_id, action, f" ({value})" if value is not None else "")
        if action in ("toggle", "volume", "loop", "shuffle", "clear", "remove"):
            await p.refresh_np()
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/state", state)
    app.router.add_post("/control", control)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", int(port)).start()
    log.debug("control API on 127.0.0.1:%s", port)
    return ControlApi(runner)

