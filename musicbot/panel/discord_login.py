"""Sign in with Discord (OAuth2, ``identify`` scope only).

The dashboard never asks Discord for the person's server list: after sign-in it only knows who
they are, and the bot checks on every request whether that person has Manage Server in a server.

Setup (owner, once): Developer Portal → the bot's application → OAuth2 → copy the Client Secret into
the panel's Settings, and add ``<public address>/auth/discord/callback`` under Redirects.
"""
from __future__ import annotations

import logging
import secrets
import urllib.parse

import aiohttp
from aiohttp import web

from .. import settings
from . import auth

log = logging.getLogger("musicbot.panel.discord")

API = "https://discord.com/api/v10"
STATE_COOKIE = "musicbot_oauth_state"


def config() -> dict:
    cfg = settings.load()
    public = (cfg.get("public_url") or "").strip().rstrip("/")
    return {
        "client_id": str(cfg.get("application_id") or "").strip(),
        "secret": (cfg.get("oauth_client_secret") or "").strip(),
        "public_url": public,
        "redirect_uri": f"{public}/auth/discord/callback" if public else "",
    }


def configured() -> bool:
    c = config()
    return bool(c["client_id"] and c["secret"] and c["public_url"])


def _https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _fail(code: str) -> web.Response:
    raise web.HTTPFound(f"/?login_error={code}")


async def start(request: web.Request) -> web.Response:
    if not configured():
        _fail("not_configured")
    c = config()
    state = secrets.token_urlsafe(24)
    url = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "response_type": "code", "redirect_uri": c["redirect_uri"],
        "scope": "identify", "state": state, "prompt": "none"})
    resp = web.HTTPFound(url)
    # Lax, not Strict: the browser has to send it when Discord sends the person back here.
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="Lax", secure=_https(request))
    raise resp


async def callback(request: web.Request) -> web.Response:
    q = request.query
    if q.get("error"):
        _fail("cancelled")
    state = request.cookies.get(STATE_COOKIE, "")
    if not state or not secrets.compare_digest(state, q.get("state", "")):
        _fail("state")
    c = config()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(f"{API}/oauth2/token", data={
                    "client_id": c["client_id"], "client_secret": c["secret"], "grant_type": "authorization_code",
                    "code": q.get("code", ""), "redirect_uri": c["redirect_uri"]}) as r:
                token = await r.json(content_type=None)
                if r.status != 200:
                    log.warning("Discord sign-in: token exchange failed: HTTP %s %s", r.status, token)
                    _fail("redirect" if "redirect_uri" in str(token) else "secret")
            async with s.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {token['access_token']}"}) as r:
                me = await r.json(content_type=None)
                if r.status != 200:
                    _fail("discord")
    except aiohttp.ClientError:
        _fail("discord")
    avatar = (f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64"
              if me.get("avatar") else None)
    user = {"id": me["id"], "name": me.get("global_name") or me.get("username") or "you", "avatar": avatar}
    log.info("Discord sign-in: %s (%s)", user["name"], user["id"])
    resp = web.HTTPFound("/#servers")
    resp.set_cookie(auth.COOKIE, auth.make_session("user", user), max_age=auth.USER_SESSION_DAYS * 86400,
                    httponly=True, samesite="Lax", secure=_https(request))
    resp.del_cookie(STATE_COOKIE)
    raise resp
