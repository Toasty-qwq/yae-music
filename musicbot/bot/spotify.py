"""Read track lists from Spotify links.

Spotify never gives out audio, so a Spotify link becomes a list of (title, artists, duration)
that is then matched on YouTube.

Two ways to read a link:
  * the public embed page (no account needed, max 100 tracks per playlist) — always available;
  * the Web API with client credentials (optional; full playlists). Since Spotify's 2026
    Development-Mode changes this needs the app owner to have Premium, so it is only tried when
    the owner filled in a client id/secret, and any failure falls back to the embed page.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("musicbot.spotify")

LINK = re.compile(
    r"(?:https?://)?(?:open|play)\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?"
    r"(track|album|playlist|artist)/([A-Za-z0-9]{22})"
    r"|spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})"
)
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


@dataclass
class SpotifyTrack:
    title: str
    artists: str
    duration: float | None  # seconds
    url: str | None


@dataclass
class SpotifyList:
    kind: str
    name: str
    tracks: list[SpotifyTrack]


def parse(text: str) -> tuple[str, str] | None:
    m = LINK.search(text.strip())
    if not m:
        return None
    return (m.group(1) or m.group(3)), (m.group(2) or m.group(4))


class Spotify:
    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._token: str | None = None
        self._token_expires = 0.0

    async def fetch(self, kind: str, sid: str, limit: int) -> SpotifyList:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            if self.client_id and self.client_secret:
                try:
                    return await self._api(session, kind, sid, limit)
                except Exception as e:  # noqa: BLE001 — any API trouble -> embed page
                    log.warning("Spotify API failed for %s/%s (%s); using the public embed page", kind, sid, e)
            return await self._embed(session, kind, sid, limit)

    # ---- embed page -------------------------------------------------------------------------

    async def _embed(self, session: aiohttp.ClientSession, kind: str, sid: str, limit: int) -> SpotifyList:
        async with session.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers={"User-Agent": UA}) as r:
            if r.status == 404:
                raise LookupError("That Spotify link doesn't exist (or the playlist is private).")
            r.raise_for_status()
            html = await r.text()
        m = NEXT_DATA.search(html)
        if not m:
            raise LookupError("Spotify changed its embed page; this link can't be read right now.")
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        name = entity.get("name") or entity.get("title") or "Spotify"
        if kind == "track":
            artists = ", ".join(a["name"] for a in entity.get("artists") or [])
            tracks = [SpotifyTrack(name, artists, _secs(entity.get("duration")),
                                   f"https://open.spotify.com/track/{sid}")]
        else:
            tracks = [
                SpotifyTrack(t.get("title", ""), (t.get("subtitle") or "").replace("\xa0", " "),
                             _secs(t.get("duration")), _uri_to_url(t.get("uri")))
                for t in entity.get("trackList") or []
                if t.get("title")
            ]
        return SpotifyList(kind, name, tracks[:limit])

    # ---- Web API ------------------------------------------------------------------------------

    async def _auth(self, session: aiohttp.ClientSession) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        async with session.post("https://accounts.spotify.com/api/token",
                                data={"grant_type": "client_credentials"},
                                headers={"Authorization": f"Basic {basic}"}) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"token request: HTTP {r.status} {body.get('error_description') or body}")
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 3600))
        return self._token

    async def _get(self, session: aiohttp.ClientSession, url: str) -> dict:
        token = await self._auth(session)
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status} for {url.split('?')[0]}")
            return await r.json()

    async def _api(self, session: aiohttp.ClientSession, kind: str, sid: str, limit: int) -> SpotifyList:
        base = "https://api.spotify.com/v1"
        if kind == "track":
            t = await self._get(session, f"{base}/tracks/{sid}")
            return SpotifyList(kind, t["name"], [_api_track(t)])
        if kind == "artist":
            # top-tracks was removed for Development-Mode apps in 2026; the embed page has them.
            raise RuntimeError("artist links use the embed page")
        if kind == "album":
            meta = await self._get(session, f"{base}/albums/{sid}")
            name, url = meta["name"], f"{base}/albums/{sid}/tracks?limit=50"
            pick = lambda it: it  # noqa: E731
        else:
            meta = await self._get(session, f"{base}/playlists/{sid}?fields=name")
            name = meta["name"]
            url = f"{base}/playlists/{sid}/items?limit=50"
            try:
                await self._get(session, url)
            except RuntimeError:  # older apps still on the pre-2026 path
                url = f"{base}/playlists/{sid}/tracks?limit=50"
            pick = lambda it: it.get("item") or it.get("track")  # noqa: E731
        tracks: list[SpotifyTrack] = []
        while url and len(tracks) < limit:
            page = await self._get(session, url)
            for it in page.get("items") or []:
                t = pick(it)
                if t and t.get("name") and t.get("type", "track") == "track":
                    tracks.append(_api_track(t))
            url = page.get("next")
        return SpotifyList(kind, name, tracks[:limit])


def _api_track(t: dict) -> SpotifyTrack:
    return SpotifyTrack(
        t["name"],
        ", ".join(a["name"] for a in t.get("artists") or []),
        _secs(t.get("duration_ms")),
        (t.get("external_urls") or {}).get("spotify"),
    )


def _secs(ms) -> float | None:
    try:
        return float(ms) / 1000 if ms else None
    except (TypeError, ValueError):
        return None


def _uri_to_url(uri: str | None) -> str | None:
    if uri and uri.startswith("spotify:track:"):
        return "https://open.spotify.com/track/" + uri.rsplit(":", 1)[1]
    return None
