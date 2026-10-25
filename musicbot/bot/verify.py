"""Is this YouTube upload the same recording as that Spotify track?

Used when a YouTube upload has the right title and length but a different artist name (meme songs
and re-releases often do). Spotify's embed page has a 30-second preview of the track; we look for
that clip inside the YouTube audio. Same recording -> ~0.9+; a different song with the same name
-> ~0.6 or less.

The comparison: for every 46 ms of audio, the shape of the spectrum (how loudness changes from
one frequency band to the next, which survives different codecs and volume), then the best
average match of the preview against every position in the full song.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess

import aiohttp
import numpy as np

from .. import settings

log = logging.getLogger("musicbot.verify")

SAME_RECORDING = 0.8
SR = 11025
WIN, HOP = 2048, 512
EDGES = np.geomspace(150, 4000, 34)
SPOTIFY_TRACK = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]{22})")
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"


async def spotify_preview(link: str | None) -> str | None:
    """URL of the track's 30 s preview MP3 (from the public embed page), if Spotify has one."""
    m = SPOTIFY_TRACK.search(link or "")
    if not m:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(f"https://open.spotify.com/embed/track/{m.group(1)}", headers={"User-Agent": UA}) as r:
                html = await r.text()
        entity = json.loads(NEXT_DATA.search(html).group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        return (entity.get("audioPreview") or {}).get("url")
    except Exception as e:  # noqa: BLE001
        log.debug("no preview for %s: %s", link, e)
        return None


def decode(url: str, headers: dict | None = None, max_seconds: int = 900) -> np.ndarray:
    cmd = [settings.load()["ffmpeg_path"], "-nostdin", "-loglevel", "error"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd += ["-i", url, "-t", str(max_seconds), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=180).stdout
    return np.frombuffer(out, np.int16).astype(np.float32)


def features(x: np.ndarray) -> np.ndarray:
    if len(x) < WIN:
        return np.zeros((0, len(EDGES) - 2), np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, WIN)[::HOP]
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    masks = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(EDGES[:-1], EDGES[1:])]
    window = np.hanning(WIN).astype(np.float32)
    out = []
    for i in range(0, len(frames), 1000):  # chunks keep memory small
        spec = np.abs(np.fft.rfft(frames[i:i + 1000] * window, axis=1))
        bands = np.log1p(np.stack([spec[:, m].sum(1) for m in masks], 1))
        d = np.diff(bands, axis=1)
        d -= d.mean(1, keepdims=True)
        d /= np.linalg.norm(d, axis=1, keepdims=True) + 1e-9
        out.append(d.astype(np.float32))
    return np.concatenate(out)


def similarity(preview: np.ndarray, full: np.ndarray) -> float:
    """Best mean cosine similarity of the preview's frames against any position in ``full``."""
    if len(preview) == 0 or len(full) < len(preview):
        return 0.0
    sims = sum(np.correlate(full[:, k], preview[:, k], "valid") for k in range(preview.shape[1]))
    return float(np.max(sims) / len(preview))


async def compare(preview_features: np.ndarray, stream_url: str, headers: dict) -> float:
    def work():
        return similarity(preview_features, features(decode(stream_url, headers)))
    return await asyncio.to_thread(work)


async def preview_features(preview_url: str) -> np.ndarray:
    return await asyncio.to_thread(lambda: features(decode(preview_url)))
