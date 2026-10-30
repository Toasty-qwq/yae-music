"""Run the bot on its own:  python -m musicbot.bot   (the panel runs it the same way)."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import discord

from .. import settings

# exit codes the panel understands (it won't auto-restart these — a restart can't fix them)
EXIT_NO_TOKEN = 2
EXIT_BAD_TOKEN = 3


def main() -> int:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    log = logging.getLogger("musicbot")

    cfg = settings.load()
    token = (cfg.get("discord_token") or "").strip()
    if not token:
        log.error("No Discord bot token set. Put it in the panel's Settings tab (or DISCORD_TOKEN).")
        return EXIT_NO_TOKEN
    if not shutil.which(cfg["ffmpeg_path"]):
        log.error("FFmpeg not found (%r). Install it or set its path in Settings — no audio without it.",
                  cfg["ffmpeg_path"])

    from .client import MusicBot

    async def run(privileged: bool) -> None:
        async with MusicBot(cfg, privileged=privileged) as bot:  # closes its HTTP session even when login fails
            await bot.start(token)

    try:
        try:
            asyncio.run(run(True))
        except discord.PrivilegedIntentsRequired:
            # Music still works without them; welcome messages, autorole, logs and member checks don't.
            log.warning("Discord refused the Server Members and Message Content intents. Turn both on: Developer "
                        "Portal → your application → Bot → Privileged Gateway Intents, then restart the bot. "
                        "Running without them for now: music works, welcome messages, autorole and logs don't.")
            asyncio.run(run(False))
    except discord.LoginFailure:
        log.error("Discord rejected the bot token. Copy a fresh one from the Developer Portal (Bot > Reset Token).")
        return EXIT_BAD_TOKEN
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
