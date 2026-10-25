"""One GuildPlayer per server: the queue, the voice connection and the play loop."""
from __future__ import annotations

import asyncio
import enum
import logging
import random
import shlex
import time
from typing import TYPE_CHECKING

import discord

from . import sources, ui
from .sources import Track

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.player")

FFMPEG_BEFORE = "-nostdin"
FFMPEG_RECONNECT = " -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn -loglevel error"
FRAME = 0.02  # discord.py reads 20 ms per frame


class LoopMode(enum.Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


class TrackedSource(discord.PCMVolumeTransformer):
    """Volume control + an exact play position (frames actually sent, so pauses don't count)."""

    def __init__(self, original: discord.AudioSource, volume: float, offset: float):
        super().__init__(original, volume)
        self.offset = offset
        self.frames = 0

    def read(self) -> bytes:
        data = super().read()
        if data:
            self.frames += 1
        return data

    @property
    def position(self) -> float:
        return self.offset + self.frames * FRAME


class GuildPlayer:
    def __init__(self, bot: "MusicBot", guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.source: TrackedSource | None = None
        self.loop = LoopMode.OFF
        self.volume = max(0, min(200, int(bot.cfg["default_volume"]))) / 100
        self.text_channel: discord.abc.Messageable | None = None
        self.np_message: discord.Message | None = None
        self.idle_since = time.monotonic()
        self.alone_since: float | None = None
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._replay: tuple[Track, float] | None = None
        self._skipped = False
        self._play_error: Exception | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._closed = False

    # ---- state -----------------------------------------------------------------------------

    @property
    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.guild_id)

    @property
    def vc(self) -> discord.VoiceClient | None:
        g = self.guild
        return g.voice_client if g else None  # type: ignore[return-value]

    @property
    def paused(self) -> bool:
        vc = self.vc
        return bool(vc and vc.is_paused())

    @property
    def position(self) -> float:
        return self.source.position if self.source else 0.0

    # ---- controls --------------------------------------------------------------------------

    def add(self, tracks: list[Track], *, front: bool = False) -> int:
        room = max(0, int(self.bot.cfg["max_queue"]) - len(self.queue))
        tracks = tracks[:room]
        if front:
            self.queue[0:0] = tracks
        else:
            self.queue.extend(tracks)
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"player-{self.guild_id}")
        return len(tracks)

    def skip(self) -> None:
        self._skipped = True
        if self.vc:
            self.vc.stop()  # -> after callback -> the loop moves on

    def skip_to(self, index: int) -> Track:
        """Drop the tracks before ``index`` (0-based) and skip to it."""
        target = self.queue[index]
        del self.queue[:index]
        self.skip()
        return target

    def seek(self, seconds: float) -> None:
        if self.current:
            self._replay = (self.current, max(0.0, seconds))
            self._skipped = True
            if self.vc:
                self.vc.stop()

    def pause(self) -> bool:
        if self.vc and self.vc.is_playing():
            self.vc.pause()
            return True
        return False

    def resume(self) -> bool:
        if self.vc and self.vc.is_paused():
            self.vc.resume()
            return True
        return False

    def set_volume(self, percent: int) -> None:
        self.volume = max(0, min(200, percent)) / 100
        if self.source:
            self.source.volume = self.volume

    def shuffle(self) -> None:
        random.shuffle(self.queue)

    def cycle_loop(self) -> LoopMode:
        order = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        self.loop = order[(order.index(self.loop) + 1) % 3]
        return self.loop

    async def stop(self, notice: str | None = None) -> None:
        """Stop, clear everything and leave the voice channel. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self.queue.clear()
        self._replay = None
        if self._task:
            self._task.cancel()
        if self._prefetch_task:
            self._prefetch_task.cancel()
        vc = self.vc
        if vc:
            vc.stop()
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                log.debug("disconnect failed", exc_info=True)
        self.current = None
        self.source = None
        await self._clear_np()
        self.bot.players.pop(self.guild_id, None)
        if notice and self.text_channel:
            try:
                await self.text_channel.send(embed=ui.ok(notice))
            except discord.HTTPException:
                pass

    # ---- the loop --------------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                if self._replay:
                    track, offset = self._replay
                    self._replay = None
                else:
                    offset = 0.0
                    self.current = None
                    while not self.queue:
                        self.idle_since = time.monotonic()
                        self._wake.clear()
                        await self._wake.wait()
                    track = self.queue.pop(0)

                self.current = track
                self._skipped = False
                try:
                    await sources.ensure_stream(track)
                except sources.ResolveError as e:
                    await self._say(ui.error(f"Skipped {ui.link_of(track)}: {e}"))
                    continue
                except Exception as e:  # noqa: BLE001
                    log.exception("resolving %s", track.title)
                    await self._say(ui.error(f"Skipped {ui.link_of(track)}: {e}"))
                    continue

                vc = self.vc
                if vc is None or not vc.is_connected():
                    log.info("guild %s: voice connection gone, stopping", self.guild_id)
                    break

                started = await self._play(vc, track, offset)
                if not started:
                    continue
                if offset == 0:
                    await self._announce(track)
                self._prefetch()
                await self._done.wait()
                played = self.source.frames * FRAME if self.source else 0
                self.source = None

                if self._replay:          # seek
                    continue
                # A stream that dies in its first seconds is usually an expired/blocked URL:
                # resolve it again once before giving up.
                if not self._skipped and played < 2 and (track.duration or 10) > 5 and not track.retried:
                    log.warning("guild %s: %r stopped after %.1fs (%s); retrying with a fresh URL",
                                self.guild_id, track.title, played, self._play_error)
                    track.retried = True
                    track.stream_url = None
                    self._replay = (track, offset)
                    continue
                if played >= 2:
                    track.retried = False
                if self.loop is LoopMode.TRACK and not self._skipped:
                    self._replay = (track, 0.0)
                    continue
                if self.loop is LoopMode.QUEUE:
                    self.queue.append(track)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("guild %s: player loop crashed", self.guild_id)
            await self._say(ui.error("The player hit an error and stopped. Use /play to start again."))
        finally:
            self.current = None
            self.source = None

    async def _play(self, vc: discord.VoiceClient, track: Track, offset: float) -> bool:
        before = FFMPEG_BEFORE
        if track.stream_url.startswith(("http://", "https://")):
            before += FFMPEG_RECONNECT
        if track.headers:
            hdr = "".join(f"{k}: {v}\r\n" for k, v in track.headers.items())
            before += " -headers " + shlex.quote(hdr)
        if offset:
            before += f" -ss {offset:.2f}"
        try:
            raw = discord.FFmpegPCMAudio(track.stream_url, executable=self.bot.cfg["ffmpeg_path"],
                                         before_options=before, options=FFMPEG_OPTIONS)
        except discord.ClientException as e:  # ffmpeg missing
            log.error("FFmpeg could not start: %s", e)
            await self._say(ui.error("FFmpeg isn't installed on the bot's machine."))
            return False
        self.source = TrackedSource(raw, self.volume, offset)
        self._done.clear()
        self._play_error = None
        loop = asyncio.get_running_loop()

        def after(err: Exception | None) -> None:
            self._play_error = err
            if err:
                log.warning("guild %s: playback error: %s", self.guild_id, err)
            loop.call_soon_threadsafe(self._done.set)

        try:
            vc.play(self.source, after=after)
        except discord.ClientException as e:
            log.warning("guild %s: play failed: %s", self.guild_id, e)
            self.source.cleanup()
            self.source = None
            return False
        log.info("guild %s: playing %r (%s)%s", self.guild_id, track.title, track.url,
                 f" from {ui.fmt_time(offset)}" if offset else "")
        return True

    def _prefetch(self) -> None:
        if not self.queue:
            return
        nxt = self.queue[0]

        async def run():
            try:
                await sources.ensure_stream(nxt)
            except Exception:  # noqa: BLE001 — it is retried (and reported) when it's its turn
                pass

        self._prefetch_task = asyncio.create_task(run())

    # ---- messages ----------------------------------------------------------------------------

    async def _say(self, embed: discord.Embed) -> None:
        if self.text_channel:
            try:
                await self.text_channel.send(embed=embed)
            except discord.HTTPException:
                pass

    async def _announce(self, track: Track) -> None:
        await self._clear_np()
        if not self.text_channel:
            return
        try:
            self.np_message = await self.text_channel.send(embed=ui.now_playing(self, track),
                                                           view=NowPlayingView(self))
        except discord.HTTPException:
            self.np_message = None

    async def _clear_np(self) -> None:
        msg, self.np_message = self.np_message, None
        if msg:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    async def refresh_np(self) -> None:
        if self.np_message and self.current:
            try:
                await self.np_message.edit(embed=ui.now_playing(self, self.current))
            except discord.HTTPException:
                pass

    # ---- reporting (panel) --------------------------------------------------------------------

    def snapshot(self) -> dict:
        g = self.guild
        vc = self.vc
        return {
            "id": str(self.guild_id),
            "name": g.name if g else str(self.guild_id),
            "channel": vc.channel.name if vc and vc.channel else None,
            "listeners": len([m for m in vc.channel.members if not m.bot]) if vc and vc.channel else 0,
            "paused": self.paused,
            "loop": self.loop.value,
            "volume": round(self.volume * 100),
            "position": self.position,
            "current": _track_json(self.current) if self.current else None,
            "queue": [_track_json(t) for t in self.queue[:25]],
            "queue_length": len(self.queue),
        }


def _track_json(t: Track) -> dict:
    return {"title": t.title, "link": t.display_link, "duration": t.duration,
            "requester": t.requester, "source": t.source, "thumbnail": t.thumbnail}


def listener_problem(member: discord.abc.User, player: GuildPlayer | None) -> str | None:
    """Why ``member`` may not control ``player`` right now (None = allowed)."""
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        return "Join a voice channel first."
    vc = player.vc if player else None
    if vc and vc.channel and member.voice.channel.id != vc.channel.id:
        return f"You need to be in {vc.channel.mention} to control the music."
    return None


class NowPlayingView(discord.ui.View):
    """Buttons under the now-playing message."""

    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        problem = listener_problem(interaction.user, self.player)
        if problem or self.player._closed:
            await interaction.response.send_message(embed=ui.error(problem or "Nothing is playing."),
                                                    ephemeral=True)
            return False
        return True

    async def _update(self, interaction: discord.Interaction) -> None:
        if self.player.current:
            await interaction.response.edit_message(embed=ui.now_playing(self.player, self.player.current),
                                                    view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, _):
        if not self.player.pause():
            self.player.resume()
        await self._update(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _):
        self.player.skip()
        await interaction.response.defer()
        log.info("guild %s: %s skipped (button)", self.player.guild_id, interaction.user)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        await self.player.stop(f"⏹️ Stopped by {interaction.user.mention}.")

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _):
        self.player.cycle_loop()
        await self._update(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, _):
        self.player.shuffle()
        await self._update(interaction)
