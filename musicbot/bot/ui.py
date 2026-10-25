"""Embeds and text formatting shared by the commands and the player."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .player import GuildPlayer
    from .sources import Track

COLOR = 0xF2879F        # Shrine Fox "rose" (its dark-theme value: Discord is usually dark)
ERROR_COLOR = 0xFF8A93  # Shrine Fox "danger", dark-theme value


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "live"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def parse_time(text: str) -> float | None:
    """'90', '1:30', '1:02:03' -> seconds."""
    try:
        parts = [float(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def title_of(t: "Track", limit: int = 80) -> str:
    title = t.title if len(t.title) <= limit else t.title[: limit - 1] + "…"
    return discord.utils.escape_markdown(title.replace("[", "(").replace("]", ")"))


def link_of(t: "Track", limit: int = 80) -> str:
    link = t.display_link
    return f"[{title_of(t, limit)}]({link})" if link else f"**{title_of(t, limit)}**"


def progress_bar(pos: float, dur: float | None, width: int = 18) -> str:
    if not dur:
        return f"`{fmt_time(pos)}` 🔴 live"
    filled = min(width, int(width * pos / dur))
    return f"`{fmt_time(pos)}` {'▬' * filled}🔘{'▬' * (width - filled)} `{fmt_time(dur)}`"


def now_playing(player: "GuildPlayer", t: "Track", *, with_progress: bool = False) -> discord.Embed:
    e = discord.Embed(title="Now playing", description=link_of(t, 120), color=COLOR)
    if with_progress:
        e.description += "\n\n" + progress_bar(player.position, t.duration)
    else:
        e.add_field(name="Length", value=fmt_time(t.duration))
    if t.requester:
        e.add_field(name="Requested by", value=t.requester)
    if t.matched and t.url:
        matched = discord.utils.escape_markdown(t.matched.replace("[", "(").replace("]", ")"))[:100]
        e.add_field(name="Playing from YouTube", value=f"[{matched}]({t.url})", inline=False)
    if player.queue:
        e.add_field(name="Up next", value=link_of(player.queue[0], 60), inline=False)
    if t.thumbnail:
        e.set_thumbnail(url=t.thumbnail)
    e.set_footer(text=footer(player) + (" · via Spotify → YouTube" if t.source == "spotify" else ""))
    return e


def footer(player: "GuildPlayer") -> str:
    loop = {"off": "", "track": " · 🔂 looping song", "queue": " · 🔁 looping queue"}[player.loop.value]
    paused = " · ⏸ paused" if player.paused else ""
    return f"{len(player.queue)} in queue · volume {round(player.volume * 100)}%{loop}{paused}"


def ok(text: str) -> discord.Embed:
    return discord.Embed(description=text, color=COLOR)


def error(text: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {text}", color=ERROR_COLOR)
