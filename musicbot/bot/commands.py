"""The slash commands."""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import sources, ui
from .player import GuildPlayer, LoopMode, listener_problem

if TYPE_CHECKING:
    from .client import MusicBot

log = logging.getLogger("musicbot.commands")

PAGE = 10


class Music(commands.Cog):
    def __init__(self, bot: "MusicBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        self.idle_check.start()

    async def cog_unload(self) -> None:
        self.idle_check.cancel()

    # ---- helpers --------------------------------------------------------------------------

    def player(self, guild: discord.Guild) -> GuildPlayer:
        p = self.bot.players.get(guild.id)
        if p is None:
            p = self.bot.players[guild.id] = GuildPlayer(self.bot, guild.id)
        return p

    async def fail(self, interaction: discord.Interaction, text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=ui.error(text), ephemeral=True)
        else:
            await interaction.response.send_message(embed=ui.error(text), ephemeral=True)

    async def controlling(self, interaction: discord.Interaction, *, need_track: bool = True) -> GuildPlayer | None:
        """The guild's player if the caller may control it, else answer with why not."""
        p = self.bot.players.get(interaction.guild_id or 0)
        if p is None or (need_track and p.current is None):
            await self.fail(interaction, "Nothing is playing.")
            return None
        problem = listener_problem(interaction.user, p)
        if problem:
            await self.fail(interaction, problem)
            return None
        return p

    async def _enqueue(self, interaction: discord.Interaction, query: str, *, front: bool) -> None:
        member = interaction.user
        existing = self.bot.players.get(interaction.guild_id or 0)
        idle = existing is None or (existing.current is None and not existing.queue)
        # an idle bot follows whoever plays next; a busy one only takes songs from its channel
        problem = listener_problem(member, None if idle else existing)
        if problem:
            return await self.fail(interaction, problem)
        assert isinstance(member, discord.Member) and member.voice and member.voice.channel
        channel = member.voice.channel
        me = interaction.guild.me  # type: ignore[union-attr]
        perms = channel.permissions_for(me)
        if not perms.connect or not perms.speak:
            return await self.fail(interaction, f"I'm not allowed to join and speak in {channel.mention}.")

        await interaction.response.defer(thinking=True)
        try:
            tracks, playlist = await sources.search(query, spotify_client=self.bot.spotify,
                                                    limit=int(self.bot.cfg["max_playlist"]))
            if not playlist:
                # one song: find its audio now, so "can't play this" is the answer to /play
                # instead of a "Starting…" followed by a "Skipped" message
                await sources.ensure_stream(tracks[0])
        except sources.ResolveError as e:
            return await self.fail(interaction, str(e))

        guild = interaction.guild
        assert guild is not None
        vc = guild.voice_client
        try:
            if vc is None:
                await channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != channel:  # idle in another channel (listener_problem allowed it)
                await vc.move_to(channel)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            log.exception("connecting to %s", channel)
            return await self.fail(interaction, f"Couldn't join {channel.mention}: {e}")

        p = self.player(guild)
        p.text_channel = interaction.channel  # type: ignore[assignment]
        for t in tracks:
            t.requester = member.display_name
            t.requester_id = member.id
        was_idle = p.current is None
        added = p.add(tracks, front=front)
        if added == 0:
            return await self.fail(interaction, "The queue is full.")
        log.info("guild %s: %s queued %d track(s) from %r", guild.id, member, added, query)

        if playlist:
            text = f"📃 Added **{added}** tracks from **{discord.utils.escape_markdown(playlist)}**"
            if added < len(tracks):
                text += f" (queue full — {len(tracks) - added} left out)"
            e = ui.ok(text)
        else:
            t = tracks[0]
            if was_idle and not front:
                e = ui.ok(f"🎶 Starting {ui.link_of(t)} `{ui.fmt_time(t.duration)}`")
            else:
                pos = 1 if front else len(p.queue)
                e = ui.ok(f"➕ Queued {ui.link_of(t)} `{ui.fmt_time(t.duration)}` — #{pos} in queue")
            if t.thumbnail:
                e.set_thumbnail(url=t.thumbnail)
        await interaction.followup.send(embed=e)

    # ---- commands ---------------------------------------------------------------------------

    @app_commands.command(description="Play a song or playlist (YouTube / Spotify link, or search words)")
    @app_commands.describe(query="A YouTube or Spotify link (song, album, playlist), or what to search for")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await self._enqueue(interaction, query, front=False)

    @app_commands.command(description="Put a song (or playlist) at the front of the queue")
    @app_commands.describe(query="A YouTube or Spotify link, or what to search for")
    @app_commands.guild_only()
    async def playnext(self, interaction: discord.Interaction, query: str):
        await self._enqueue(interaction, query, front=True)

    @app_commands.command(description="Skip the current song")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if p := await self.controlling(interaction):
            t = p.current
            p.skip()
            await interaction.response.send_message(embed=ui.ok(f"⏭️ Skipped {ui.link_of(t)}"))

    @app_commands.command(description="Skip to a position in the queue")
    @app_commands.describe(position="Queue position (see /queue)")
    @app_commands.guild_only()
    async def skipto(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]):
        if p := await self.controlling(interaction):
            if position > len(p.queue):
                return await self.fail(interaction, f"The queue only has {len(p.queue)} songs.")
            t = p.skip_to(position - 1)
            await interaction.response.send_message(embed=ui.ok(f"⏭️ Skipping to {ui.link_of(t)}"))

    @app_commands.command(description="Stop the music, clear the queue and leave the voice channel")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        p = self.bot.players.get(interaction.guild_id or 0)
        if p is None and interaction.guild and interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect(force=True)
            return await interaction.response.send_message(embed=ui.ok("👋 Left the voice channel."))
        if p := await self.controlling(interaction, need_track=False):
            await interaction.response.send_message(embed=ui.ok("⏹️ Stopped and cleared the queue."))
            await p.stop()

    @app_commands.command(description="Pause the music")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if p := await self.controlling(interaction):
            if not p.pause():
                return await self.fail(interaction, "Already paused. Use /resume.")
            await interaction.response.send_message(embed=ui.ok("⏸️ Paused."))

    @app_commands.command(description="Resume the music")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if p := await self.controlling(interaction):
            if not p.resume():
                return await self.fail(interaction, "It isn't paused.")
            await interaction.response.send_message(embed=ui.ok("▶️ Resumed."))

    @app_commands.command(name="nowplaying", description="Show the current song")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        p = self.bot.players.get(interaction.guild_id or 0)
        if p is None or p.current is None:
            return await self.fail(interaction, "Nothing is playing.")
        await interaction.response.send_message(embed=ui.now_playing(p, p.current, with_progress=True))

    @app_commands.command(description="Show the queue")
    @app_commands.describe(page="Page number")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
        p = self.bot.players.get(interaction.guild_id or 0)
        if p is None or (p.current is None and not p.queue):
            return await self.fail(interaction, "The queue is empty.")
        pages = max(1, math.ceil(len(p.queue) / PAGE))
        page = min(page, pages)
        lines = []
        if p.current:
            lines.append(f"**Now:** {ui.link_of(p.current, 60)} "
                         f"`{ui.fmt_time(p.position)} / {ui.fmt_time(p.current.duration)}`\n")
        start = (page - 1) * PAGE
        for i, t in enumerate(p.queue[start:start + PAGE], start=start + 1):
            lines.append(f"`{i}.` {ui.link_of(t, 60)} `{ui.fmt_time(t.duration)}` · {t.requester}")
        if not p.queue:
            lines.append("*Nothing queued after this.*")
        total = sum(t.duration or 0 for t in p.queue)
        e = discord.Embed(title="Queue", description="\n".join(lines), color=ui.COLOR)
        e.set_footer(text=f"Page {page}/{pages} · {len(p.queue)} songs · {ui.fmt_time(total)} total · "
                          + ui.footer(p).split(" · ", 1)[1])
        await interaction.response.send_message(embed=e)

    @app_commands.command(description="Set the volume (0-200%)")
    @app_commands.describe(level="Volume in percent; leave empty to see it")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 200] | None = None):
        if p := await self.controlling(interaction, need_track=False):
            if level is None:
                return await interaction.response.send_message(
                    embed=ui.ok(f"🔊 Volume is **{round(p.volume * 100)}%**"))
            p.set_volume(level)
            await interaction.response.send_message(embed=ui.ok(f"🔊 Volume set to **{level}%**"))
            await p.refresh_np()

    @app_commands.command(description="Loop the current song, the whole queue, or turn looping off")
    @app_commands.choices(mode=[
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="song", value="track"),
        app_commands.Choice(name="queue", value="queue"),
    ])
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if p := await self.controlling(interaction, need_track=False):
            p.loop = LoopMode(mode.value)
            text = {"off": "➡️ Looping is off.", "track": "🔂 Looping this song.",
                    "queue": "🔁 Looping the queue."}[mode.value]
            await interaction.response.send_message(embed=ui.ok(text))
            await p.refresh_np()

    @app_commands.command(description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        if p := await self.controlling(interaction, need_track=False):
            if len(p.queue) < 2:
                return await self.fail(interaction, "Not enough songs in the queue to shuffle.")
            p.shuffle()
            await interaction.response.send_message(embed=ui.ok(f"🔀 Shuffled {len(p.queue)} songs."))

    @app_commands.command(description="Remove a song from the queue")
    @app_commands.describe(position="Queue position (see /queue)")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]):
        if p := await self.controlling(interaction, need_track=False):
            if position > len(p.queue):
                return await self.fail(interaction, f"The queue only has {len(p.queue)} songs.")
            t = p.queue.pop(position - 1)
            await interaction.response.send_message(embed=ui.ok(f"🗑️ Removed {ui.link_of(t)}"))

    @app_commands.command(description="Move a song to another place in the queue")
    @app_commands.describe(source="Position of the song now", target="Where it should go")
    @app_commands.guild_only()
    async def move(self, interaction: discord.Interaction, source: app_commands.Range[int, 1],
                   target: app_commands.Range[int, 1]):
        if p := await self.controlling(interaction, need_track=False):
            n = len(p.queue)
            if source > n or target > n:
                return await self.fail(interaction, f"The queue only has {n} songs.")
            t = p.queue.pop(source - 1)
            p.queue.insert(target - 1, t)
            await interaction.response.send_message(embed=ui.ok(f"↕️ Moved {ui.link_of(t)} to #{target}"))

    @app_commands.command(description="Clear the queue (the current song keeps playing)")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction):
        if p := await self.controlling(interaction, need_track=False):
            n = len(p.queue)
            p.queue.clear()
            await interaction.response.send_message(embed=ui.ok(f"🧹 Cleared {n} songs from the queue."))

    @app_commands.command(description="Jump to a time in the current song")
    @app_commands.describe(time="e.g. 1:30 or 90")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, time: str):
        if p := await self.controlling(interaction):
            secs = ui.parse_time(time)
            if secs is None:
                return await self.fail(interaction, "Use a time like `1:30` or `90`.")
            if p.current.duration is None:
                return await self.fail(interaction, "Can't seek in a live stream.")
            if secs >= p.current.duration:
                return await self.fail(interaction, f"The song is only {ui.fmt_time(p.current.duration)} long.")
            p.seek(secs)
            await interaction.response.send_message(embed=ui.ok(f"⏩ Jumped to `{ui.fmt_time(secs)}`"))

    @app_commands.command(description="Show the music commands")
    async def help(self, interaction: discord.Interaction):
        e = discord.Embed(title="Commands", color=ui.COLOR,
                          description="Every command, by area. The dashboard's Commands page explains each one.")
        areas = (("Music", "Music"), ("Welcome", "Greetings"), ("Moderation", "Moderation"), ("Tags", "Tags"),
                 ("Community", "Suggestions"), ("Utility", "Reminders and info"))
        for cog_name, title in areas:
            cog = self.bot.get_cog(cog_name)
            if not cog:
                continue
            names = []
            for c in cog.get_app_commands():
                subs = getattr(c, "commands", None)
                names += [f"`/{c.name} {s.name}`" for s in subs] if subs else [f"`/{c.name}`"]
            e.add_field(name=title, value=" ".join(names)[:1024], inline=False)
        e.set_footer(text="Welcome messages, role menus, logs and more are set up on the bot's dashboard.")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ---- leaving when idle / alone -------------------------------------------------------------

    @tasks.loop(seconds=20)
    async def idle_check(self):
        timeout = float(self.bot.cfg["idle_timeout"]) * 60
        if timeout <= 0:
            return
        now = time.monotonic()
        for p in list(self.bot.players.values()):
            vc = p.vc
            if vc is None or vc.channel is None:
                continue
            humans = [m for m in vc.channel.members if not m.bot]
            if humans:
                p.alone_since = None
            elif p.alone_since is None:
                p.alone_since = now
            if p.alone_since and now - p.alone_since >= timeout:
                log.info("guild %s: alone in voice, leaving", p.guild_id)
                await p.stop("👋 Left because everyone else left the voice channel.")
            elif p.current is None and not p.queue and now - p.idle_since >= timeout:
                log.info("guild %s: idle, leaving", p.guild_id)
                await p.stop("👋 Left because nothing was playing.")

    @idle_check.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        if self.bot.user and member.id == self.bot.user.id and before.channel and after.channel is None:
            p = self.bot.players.get(member.guild.id)
            if p:  # kicked / disconnected by someone
                log.info("guild %s: disconnected from voice", member.guild.id)
                await p.stop()

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError) -> None:
        log.exception("command /%s failed", interaction.command.name if interaction.command else "?",
                      exc_info=error)
        try:
            await self.fail(interaction, "Something went wrong running that command.")
        except discord.HTTPException:
            pass
