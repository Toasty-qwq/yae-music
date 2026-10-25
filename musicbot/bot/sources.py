"""Turn whatever the user typed into Tracks, and Tracks into something FFmpeg can play.

Resolving is two-step on purpose:
  * ``search`` (on /play) is fast: playlists are read "flat" (ids + titles only) and Spotify
    tracks are just names;
  * ``ensure_stream`` (right before a track plays, or while the previous one plays) finds the
    actual audio URL. Those URLs expire after a few hours, so they are never fetched up front.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

import yt_dlp

from .. import settings
from . import spotify

log = logging.getLogger("musicbot.sources")

URL = re.compile(r"^https?://", re.I)
STREAM_TTL = 3 * 3600  # re-resolve a stream URL older than this
# words that usually mean "not the studio version" — penalised unless the Spotify title has them
OFF_VERSIONS = ("live", "cover", "karaoke", "instrumental", "remix", "nightcore", "sped up",
                "slowed", "8d", "reverb", "lyrics video", "1 hour", "10 hours", "loop")


@dataclass
class Track:
    title: str
    url: str | None = None            # page to resolve (YouTube video, SoundCloud, ...)
    search: str | None = None         # for Spotify tracks: what to look for on YouTube
    song: str | None = None           # Spotify: the song name alone
    artists: list = field(default_factory=list)  # Spotify: artist names
    matched: str | None = None        # Spotify: title of the YouTube upload actually played
    duration: float | None = None     # seconds; None = unknown / live
    thumbnail: str | None = None
    source: str = "youtube"           # youtube | spotify | other
    link: str | None = None           # link shown to users (Spotify link for Spotify tracks)
    requester: str = ""
    requester_id: int = 0
    stream_url: str | None = None
    headers: dict = field(default_factory=dict)
    resolved_at: float = 0.0
    retried: bool = False             # one fresh-URL retry after a stream dies at once

    @property
    def display_link(self) -> str | None:
        return self.link or self.url


class ResolveError(Exception):
    """A message that is safe to show to the user."""


def _ydl_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "bestaudio[acodec=opus]/bestaudio/best",
        "default_search": "ytsearch",
        "socket_timeout": 15,
        "extractor_retries": 2,
        "logger": _YtdlLogger(),
    }
    if settings.COOKIES_FILE.is_file():
        opts["cookiefile"] = str(settings.COOKIES_FILE)
    opts.update(extra)
    return opts


class _YtdlLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.warning("yt-dlp: %s", msg)


def _extract(target: str, **extra) -> dict:
    with yt_dlp.YoutubeDL(_ydl_opts(**extra)) as ydl:
        return ydl.extract_info(target, download=False)


async def _run(fn, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _friendly(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube is asking this server to sign in. Upload a cookies.txt in the panel "
                "(Maintenance tab) and try again.")
    if "private video" in low:
        return "That video is private."
    if "unavailable" in low or "not available" in low:
        return "That video is unavailable."
    if "age" in low and "confirm" in low:
        return "That video is age-restricted (a cookies.txt from a signed-in account fixes this)."
    if "unsupported url" in low:
        return "I can't play that link."
    if "http error 404" in low:
        return "That link doesn't exist."
    return msg.replace("ERROR: ", "")[:300]


# ---- /play: query -> tracks --------------------------------------------------------------------

async def search(query: str, *, spotify_client: spotify.Spotify, limit: int) -> tuple[list[Track], str | None]:
    """Returns (tracks, playlist name or None)."""
    query = query.strip()
    if not query:
        raise ResolveError("Give me a link or something to search for.")

    sp = spotify.parse(query)
    if sp:
        kind, sid = sp
        try:
            result = await spotify_client.fetch(kind, sid, limit)
        except LookupError as e:
            raise ResolveError(str(e)) from e
        except Exception as e:  # noqa: BLE001
            log.exception("Spotify %s/%s", kind, sid)
            raise ResolveError(f"Couldn't read that Spotify link ({e}).") from e
        tracks = [_spotify_track(t) for t in result.tracks]
        if not tracks:
            raise ResolveError("That Spotify link has no playable tracks.")
        return tracks, (None if kind == "track" else result.name)

    if URL.match(query):
        try:
            info = await _run(_extract, query, extract_flat="in_playlist", playlistend=limit)
        except Exception as e:  # noqa: BLE001
            raise ResolveError(_friendly(e)) from e
        if info.get("_type") == "playlist" or info.get("entries") is not None:
            tracks = [t for t in (_from_entry(e) for e in info.get("entries") or [] if e) if t]
            if not tracks:
                raise ResolveError("That playlist is empty (or all of it is private).")
            return tracks[:limit], info.get("title") or "playlist"
        return [_from_full(info)], None

    entries = await _candidates(query)
    if not entries:
        raise ResolveError(f"Nothing found for **{query}**.")
    return [_from_entry(entries[0])], None


def _spotify_track(t: spotify.SpotifyTrack) -> Track:
    artists = [a.strip() for a in t.artists.split(",") if a.strip()]
    full = f"{t.artists} - {t.title}" if artists else t.title
    return Track(title=full, search=full, song=t.title, artists=artists, duration=t.duration,
                 source="spotify", link=t.url)


def _thumb(e: dict) -> str | None:
    if e.get("thumbnail"):
        return e["thumbnail"]
    thumbs = e.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


def _from_entry(e: dict) -> Track | None:
    """A flat playlist/search entry (no stream yet)."""
    if e.get("title") in ("[Private video]", "[Deleted video]"):
        return None
    if e.get("id") and "youtube" in (e.get("ie_key") or "").lower():
        url = f"https://www.youtube.com/watch?v={e['id']}"
    else:
        url = e.get("url") or e.get("webpage_url")
    if not url:
        return None
    return Track(title=e.get("title") or url, url=url, duration=e.get("duration"),
                 thumbnail=_thumb(e), source="youtube" if "youtu" in url else "other")


def _from_full(info: dict) -> Track:
    """A fully extracted single video — it already has its stream."""
    t = Track(title=info.get("title") or "Unknown", url=info.get("webpage_url") or info.get("original_url"),
              duration=info.get("duration"), thumbnail=_thumb(info),
              source="youtube" if "youtube" in (info.get("extractor") or "") else "other")
    _apply_stream(t, info)
    return t


def _apply_stream(t: Track, info: dict) -> None:
    url = info.get("url")
    if not url:
        fmts = [f for f in info.get("requested_formats") or [] if f.get("acodec") not in (None, "none")]
        url = fmts[0]["url"] if fmts else None
    if not url:
        raise ResolveError("No audio stream found.")
    t.stream_url = url
    t.headers = dict(info.get("http_headers") or {})
    t.resolved_at = time.time()
    t.duration = info.get("duration") or t.duration
    t.thumbnail = t.thumbnail or _thumb(info)
    if t.source != "spotify":
        t.title = info.get("title") or t.title


# ---- before playing: track -> stream URL ------------------------------------------------------

async def ensure_stream(t: Track) -> None:
    if t.stream_url and time.time() - t.resolved_at < STREAM_TTL:
        return
    if not t.url and t.search:
        t.url = await _match(t)
    try:
        info = await _run(_extract, t.url)
    except Exception as e:  # noqa: BLE001
        raise ResolveError(_friendly(e)) from e
    _apply_stream(t, info)


async def _candidates(query: str) -> list[dict]:
    """Videos YouTube finds for ``query``. yt-dlp's ytsearch filters to "videos", and for some
    queries YouTube then returns nothing, so the plain results page is the fallback."""
    targets = (f"ytsearch8:{query}",
               "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
    for target in targets:
        try:
            info = await _run(_extract, target, extract_flat="in_playlist", playlistend=8)
        except Exception as e:  # noqa: BLE001
            log.warning("YouTube search %r failed: %s", target, e)
            continue
        entries = [e for e in info.get("entries") or []
                   if e and len(e.get("id") or "") == 11 and "/shorts/" not in (e.get("url") or "")]
        if entries:
            return entries
    return []


# ---- Spotify track -> YouTube upload -------------------------------------------------------------
#
# A wrong song is worse than no song, so every candidate must look like the same recording:
# same song name, one of the same artists, nearly the same length. YouTube Music is asked first
# (its song results carry artists + exact length, and are the official audio); plain YouTube
# videos second. If nothing passes, the track is skipped with a message saying so.

def _norm(text: str) -> str:
    """Letters/digits only, lower case, accents dropped ('bêtt' == 'Bétt' == 'bett')."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", text.casefold())


def _core_title(song: str) -> str:
    """'KICK BACK (feat. X) - 2022 Remaster' -> 'kickback'."""
    song = re.sub(r"\s*[(\[](?:feat|ft|with|prod)\.?\s[^)\]]*[)\]]", "", song, flags=re.I)
    song = re.split(r"\s+-\s+", song)[0]
    return _norm(song) or song.strip().casefold()


def _ytmusic_songs(query: str) -> list[dict]:
    from ytmusicapi import YTMusic  # imported here: only Spotify matching needs it
    return YTMusic().search(query, filter="songs", limit=10)


async def _match(t: Track) -> str:
    song = _core_title(t.song or t.search)
    artists = [_norm(a) for a in t.artists if _norm(a)]
    wanted = t.search.lower()
    # right title + length but another artist name: only trusted after the audio check (step 3)
    maybe: dict[str, tuple[float, str]] = {}

    def length_ok(seconds, tolerance):
        return not t.duration or not seconds or abs(seconds - t.duration) <= tolerance

    def length_gap(seconds):
        return abs(seconds - t.duration) if t.duration and seconds else 30

    def take(video_id: str, matched: str, how: str) -> str:
        t.matched = matched
        log.info("matched %r -> %s %r", t.search, how, matched)
        return f"https://www.youtube.com/watch?v={video_id}"

    # 1) YouTube Music songs by one of the artists
    try:
        songs = await _run(_ytmusic_songs, f"{' '.join(t.artists)} {t.song or ''}".strip() or t.search)
    except Exception as e:  # noqa: BLE001
        log.warning("YouTube Music search failed for %r: %s", t.search, e)
        songs = []
    good = []
    for i, r in enumerate(songs):
        names = [_norm(a.get("name") or "") for a in r.get("artists") or []]
        if not r.get("videoId") or not song or song not in _norm(r.get("title") or ""):
            continue
        if not length_ok(r.get("duration_seconds"), 10):
            continue
        by = ", ".join(a["name"] for a in r.get("artists") or [])
        label = f"{r.get('title')} — {by}" if by else str(r.get("title"))
        if not artists or any(a and a in names for a in artists):
            good.append((length_gap(r.get("duration_seconds")) + i, r, label))
        else:
            maybe.setdefault(r["videoId"], (length_gap(r.get("duration_seconds")) + i, label))
    if good:
        _, best, label = min(good, key=lambda x: x[0])
        return take(best["videoId"], label, "YouTube Music")

    # 2) YouTube videos that name the artist and the song
    for query in dict.fromkeys([t.search, f"{t.artists[0]} {t.song}" if t.artists and t.song else t.search]):
        good = []
        for i, e in enumerate(await _candidates(query)):
            title = (e.get("title") or "")
            if song not in _norm(title) or not length_ok(e.get("duration"), 15):
                continue
            score = i * 4 + length_gap(e.get("duration"))
            score += sum(60 for w in OFF_VERSIONS if w in title.lower() and w not in wanted)
            where = _norm(title + " " + (e.get("channel") or e.get("uploader") or ""))
            if not artists or any(a in where for a in artists):
                good.append((score, e))
            elif e.get("duration") and length_ok(e["duration"], 10):
                by = e.get("channel") or e.get("uploader")
                maybe.setdefault(e["id"], (score, f"{title} — {by}" if by else title))
        if good:
            best = min(good, key=lambda x: x[0])[1]
            t.thumbnail = t.thumbnail or _thumb(best)
            return take(best["id"], best.get("title"), "YouTube video")

    # 3) same title + length under another artist name: compare the actual audio with
    #    Spotify's 30-second preview (meme songs and re-releases often change the artist)
    if maybe:
        found = await _verify_by_audio(t, maybe)
        if found:
            return take(*found, "audio-checked")

    log.warning("no YouTube upload of %r found", t.search)
    raise ResolveError(f"**{t.search}** doesn't seem to be on YouTube (it may be a Spotify-only "
                       "release), so I can't play it.")


async def _verify_by_audio(t: Track, maybe: dict[str, tuple[float, str]]) -> tuple[str, str] | None:
    from . import verify  # numpy is only loaded when this is needed
    preview = await verify.spotify_preview(t.link)
    if not preview:
        log.info("%r: no Spotify preview, can't audio-check %d candidate(s)", t.search, len(maybe))
        return None
    try:
        reference = await verify.preview_features(preview)
    except Exception as e:  # noqa: BLE001
        log.warning("%r: couldn't read the Spotify preview: %s", t.search, e)
        return None
    for video_id, (_, label) in sorted(maybe.items(), key=lambda kv: kv[1][0])[:3]:
        try:
            info = await _run(_extract, f"https://www.youtube.com/watch?v={video_id}")
            url = info.get("url")
            if not url:
                continue
            score = await verify.compare(reference, url, info.get("http_headers") or {})
        except Exception as e:  # noqa: BLE001
            log.debug("audio check of %s failed: %s", video_id, e)
            continue
        log.info("%r vs %r: audio similarity %.2f", t.search, label, score)
        if score >= verify.SAME_RECORDING:
            return video_id, label
    return None
