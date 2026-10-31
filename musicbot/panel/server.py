"""The web panel: one page (static/index.html) + a JSON API."""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import web

from .. import settings
from . import auth, discord_login
from .supervisor import Supervisor

log = logging.getLogger("musicbot.panel")
STATIC = Path(__file__).parent / "static"
OPEN_PATHS = {"/", "/healthz", "/api/auth", "/api/login", "/api/setup", "/favicon.ico"}
ANY_SESSION = ("/api/dash/", "/api/logout")  # Discord sign-ins reach only these; everything else is the owner's
SECRET_KEYS = {"discord_token", "spotify_client_secret", "oauth_client_secret"}
INT_KEYS = {"default_volume": (0, 200), "idle_timeout": (0, 1440), "max_playlist": (1, 5000),
            "max_queue": (1, 10000)}
BOOL_KEYS = {"autostart", "auto_restart", "update_ytdlp_on_start"}
STR_KEYS = {"discord_token", "spotify_client_id", "spotify_client_secret", "ffmpeg_path", "public_url",
            "oauth_client_secret"}

sup = Supervisor()
_update_lock = asyncio.Lock()
_ffmpeg_version: str | None = None


# ---- middleware ----------------------------------------------------------------------------------

@web.middleware
async def guard(request: web.Request, handler):
    path = request.path
    if path.startswith("/api/") and path not in OPEN_PATHS:
        s = auth.session(request.cookies.get(auth.COOKIE))
        if s is None:
            return web.json_response({"error": "login required"}, status=401)
        if s["k"] != "owner" and not path.startswith(ANY_SESSION):
            return web.json_response({"error": "Only the panel's owner can open that."}, status=403)
        request["session"] = s
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-Requested-With") != "musicbot":
        return web.json_response({"error": "bad request"}, status=400)  # blocks cross-site form posts
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("panel %s %s", request.method, path)
        return web.json_response({"error": str(e)}, status=500)


def _login_response(data: dict, request: web.Request | None = None) -> web.Response:
    resp = web.json_response(data)
    secure = bool(request and discord_login._https(request))
    resp.set_cookie(auth.COOKIE, auth.make_session("owner"), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="Strict", secure=secure)
    return resp


# ---- pages / auth -----------------------------------------------------------------------------

def _asset_version() -> str:
    """Changes whenever a static file does, so browsers fetch the new css/js after an update."""
    return str(int(max(f.stat().st_mtime for f in STATIC.rglob("*") if f.is_file())))


async def index(_):
    html = (STATIC / "index.html").read_text("utf-8").replace("__V__", _asset_version())
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def dashboard_js(_):
    return web.FileResponse(STATIC / "dashboard.js", headers={"Cache-Control": "no-store"})


async def favicon(_):
    return web.FileResponse(STATIC / "ds" / "icon.png")


async def healthz(_):
    return web.Response(text="ok")


async def auth_state(request):
    s = auth.session(request.cookies.get(auth.COOKIE))
    return web.json_response({"setup": auth.needs_setup(), "logged_in": s is not None,
                              "kind": s["k"] if s else None, "user": (s or {}).get("u"),
                              "discord_login": discord_login.configured()})


async def setup(request):
    if not auth.needs_setup():
        return web.json_response({"error": "A password is already set."}, status=400)
    pw = (await request.json()).get("password", "")
    if len(pw) < 6:
        return web.json_response({"error": "Use at least 6 characters."}, status=400)
    auth.set_password(pw)
    sup.panel_log("Panel password created.")
    return _login_response({"ok": True}, request)


async def login(request):
    pw = (await request.json()).get("password", "")
    if not auth.check_password(pw):
        await asyncio.sleep(1.0)  # slow down guessing
        sup.panel_log(f"Failed panel login from {request.remote}.", "warn")
        return web.json_response({"error": "Wrong password."}, status=401)
    return _login_response({"ok": True}, request)


async def logout(_):
    resp = web.json_response({"ok": True})
    resp.del_cookie(auth.COOKIE)
    return resp


async def change_password(request):
    body = await request.json()
    if os.environ.get("PANEL_PASSWORD"):
        return web.json_response({"error": "The password is set by PANEL_PASSWORD in the container's "
                                           "environment; change it there."}, status=400)
    if not auth.check_password(body.get("current", "")):
        return web.json_response({"error": "The current password is wrong."}, status=400)
    if len(body.get("new", "")) < 6:
        return web.json_response({"error": "Use at least 6 characters."}, status=400)
    auth.set_password(body["new"])
    sup.panel_log("Panel password changed.")
    return _login_response({"ok": True}, request)


# ---- bot ---------------------------------------------------------------------------------------

async def status(_):
    data = {"process": sup.status(), "bot": None, "has_token": bool(settings.load()["discord_token"]),
            "updating": _update_lock.locked(), "now": time.time()}
    if sup.running:
        try:
            data["bot"] = await sup.bot_request("GET", "/state", timeout=2)
        except Exception:  # noqa: BLE001 — still starting up
            pass
    if data["bot"] and data["bot"].get("id"):
        from ..bot.client import invite_url
        data["bot"]["invite"] = invite_url(int(data["bot"]["id"]))
        if settings.raw().get("application_id") != data["bot"]["id"]:
            settings.update({"application_id": data["bot"]["id"]})  # the Client ID Discord sign-in needs
    return web.json_response(data)


async def bot_action(request):
    action = request.match_info["action"]
    if action == "start":
        if not settings.load()["discord_token"]:
            return web.json_response({"error": "Set the Discord bot token in Settings first."}, status=400)
        await sup.start()
    elif action == "stop":
        await sup.stop()
    elif action == "restart":
        await sup.restart()
    else:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True, "process": sup.status()})


async def control(request):
    body = await request.json()
    try:
        result = await sup.bot_request("POST", "/control", body)
    except Exception as e:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(e) or "The bot didn't answer."}, status=502)
    return web.json_response(result)


# ---- logs --------------------------------------------------------------------------------------

async def logs(_):
    return web.json_response(list(sup.lines)[-1000:])


async def logs_stream(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    last = request.headers.get("Last-Event-ID")
    last_seq = int(last) if last and last.isdigit() else None
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    sup.subscribers.add(q)
    try:
        backlog = [e for e in sup.lines if last_seq is None or e["seq"] > last_seq][-1000:]
        if last_seq is None:
            await resp.write(b"event: reset\ndata: {}\n\n")
        for e in backlog:
            await resp.write(f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n".encode())
        while True:
            try:
                e = await asyncio.wait_for(q.get(), 15)
                await resp.write(f"id: {e['seq']}\ndata: {json.dumps(e)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        sup.subscribers.discard(q)
    return resp


async def logs_download(_):
    if not sup.log_path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(sup.log_path, headers={
        "Content-Disposition": f'attachment; filename="musicbot-{time.strftime("%Y%m%d-%H%M")}.log"'})


async def logs_clear(_):
    sup.clear()
    return web.json_response({"ok": True})


# ---- settings ------------------------------------------------------------------------------------

def _mask(value: str) -> dict:
    return {"set": bool(value), "hint": ("…" + value[-4:]) if len(value) > 8 else ""}


async def get_settings(_):
    cfg = settings.load()
    out = {k: (_mask(cfg[k]) if k in SECRET_KEYS else cfg[k]) for k in settings.DEFAULTS}
    out["env_locked"] = sorted(settings.env_locked())
    out["has_cookies"] = settings.COOKIES_FILE.is_file()
    out["data_dir"] = str(settings.DATA_DIR)
    out["oauth"] = {"redirect_uri": discord_login.config()["redirect_uri"], "configured": discord_login.configured(),
                    "client_id": cfg.get("application_id") or ""}
    return web.json_response(out)


async def put_settings(request):
    body = await request.json()
    changes, locked = {}, settings.env_locked()
    for k, v in body.items():
        if k in locked:
            continue
        if k in INT_KEYS:
            lo, hi = INT_KEYS[k]
            try:
                v = int(v)
            except (TypeError, ValueError):
                return web.json_response({"error": f"{k} must be a whole number."}, status=400)
            if not lo <= v <= hi:
                return web.json_response({"error": f"{k} must be between {lo} and {hi}."}, status=400)
            changes[k] = v
        elif k in BOOL_KEYS:
            changes[k] = bool(v)
        elif k in STR_KEYS:
            v = str(v).strip()
            if k in SECRET_KEYS and not v:
                continue  # empty secret field = keep the stored one
            if k == "public_url" and v and not re.match(r"^https?://[^/\s]+$", v.rstrip("/")):
                return web.json_response({"error": "The public address must look like https://music.example.com "
                                                   "(no path after the name)."}, status=400)
            if k == "public_url":
                v = v.rstrip("/")
            changes[k] = v
    if body.get("clear_spotify"):
        changes["spotify_client_id"] = ""
        changes["spotify_client_secret"] = ""
    settings.update(changes)
    shown = sorted(k for k in changes)
    sup.panel_log(f"Settings saved ({', '.join(shown) or 'no changes'}).")
    return web.json_response({"ok": True, "restart_needed": sup.running and bool(changes)})


# ---- maintenance --------------------------------------------------------------------------------

def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _ffmpeg() -> str:
    global _ffmpeg_version
    if _ffmpeg_version is None:
        exe = shutil.which(settings.load()["ffmpeg_path"])
        if not exe:
            _ffmpeg_version = "not found"
        else:
            try:
                out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10).stdout
                _ffmpeg_version = out.split("\n", 1)[0].replace("ffmpeg version ", "").split(" Copyright")[0]
            except (OSError, subprocess.SubprocessError):
                _ffmpeg_version = "error"
    return _ffmpeg_version


async def versions(_):
    return web.json_response({
        "python": platform.python_version(),
        "discord.py": _version("discord.py"),
        "davey (voice encryption)": _version("davey"),
        "yt-dlp": _version("yt-dlp"),
        "yt-dlp-ejs": _version("yt-dlp-ejs"),
        "deno": _version("deno"),
        "ffmpeg": await asyncio.to_thread(_ffmpeg),
        "docker": os.path.exists("/.dockerenv"),
    })


async def update_ytdlp() -> tuple[bool, str]:
    async with _update_lock:
        before = _version("yt-dlp")
        sup.panel_log(f"Updating yt-dlp (now {before})…")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check",
            "--root-user-action=ignore", "yt-dlp[default,deno]",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        for line in out.decode("utf-8", "replace").splitlines():
            if line.strip() and not line.startswith("Requirement already satisfied"):
                sup.panel_log("pip: " + line.strip(), "warn" if "WARNING" in line else "info")
        after = _version("yt-dlp")
        if proc.returncode != 0:
            sup.panel_log(f"yt-dlp update failed (pip exit {proc.returncode}).", "error")
            return False, before or "?"
        if after != before:
            sup.panel_log(f"yt-dlp updated {before} → {after}."
                          + (" Restart the bot to use it." if sup.running else ""))
        else:
            sup.panel_log(f"yt-dlp is up to date ({after}).")
        return True, after or "?"


async def update_ytdlp_route(_):
    if _update_lock.locked():
        return web.json_response({"error": "An update is already running."}, status=409)
    before = _version("yt-dlp")
    ok, version = await update_ytdlp()
    return web.json_response({"ok": ok, "version": version, "changed": ok and version != before,
                              "running": sup.running}, status=200 if ok else 500)


async def upload_cookies(request):
    text = (await request.json()).get("text", "")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines or not all(len(ln.split("\t")) >= 7 for ln in lines[:20]):
        return web.json_response({"error": "That isn't a Netscape cookies.txt file (export it with a "
                                           "'Get cookies.txt' browser extension)."}, status=400)
    if not any("youtube.com" in ln for ln in lines):
        return web.json_response({"error": "That file has no youtube.com cookies."}, status=400)
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.COOKIES_FILE.write_text(text if text.endswith("\n") else text + "\n", "utf-8")
    sup.panel_log("YouTube cookies.txt uploaded.")
    return web.json_response({"ok": True})


async def delete_cookies(_):
    settings.COOKIES_FILE.unlink(missing_ok=True)
    sup.panel_log("YouTube cookies.txt removed.")
    return web.json_response({"ok": True})


# ---- the bot's own profile (owner only) ----------------------------------------------------------

async def profile(request: web.Request):
    body = await request.read() if request.method == "POST" else None
    status_code, content_type, data = await sup.bot_proxy(request.method, "/profile", {}, body, timeout=60)
    if request.method == "POST" and status_code == 200:
        sup.panel_log("Bot profile changed from the panel.")
    return web.Response(body=data, status=status_code, content_type=content_type)


# ---- dashboard (owner and Discord sign-ins) -------------------------------------------------------

async def dash_proxy(request: web.Request):
    s = request["session"]
    uid = "owner" if s["k"] == "owner" else s["u"]["id"]
    query = {k: v for k, v in request.query.items() if k != "uid"}
    query["uid"] = uid
    body = await request.read() if request.can_read_body else None
    status_code, content_type, data = await sup.bot_proxy(request.method, "/dash/" + request.match_info["tail"],
                                                          query, body)
    return web.Response(body=data, status=status_code, content_type=content_type)


# ---- app ---------------------------------------------------------------------------------------

async def _boot(app: web.Application) -> None:
    async def run():
        cfg = settings.load()
        if cfg.get("update_ytdlp_on_start"):
            try:
                await update_ytdlp()
            except Exception as e:  # noqa: BLE001
                sup.panel_log(f"yt-dlp update failed: {e}", "warn")
        if cfg.get("autostart") and cfg.get("discord_token"):
            await sup.start()
        elif not cfg.get("discord_token"):
            sup.panel_log("No Discord bot token yet — open the panel's Settings tab to add it.", "warn")
    app["boot"] = asyncio.create_task(run())


async def _shutdown(app: web.Application) -> None:
    app["boot"].cancel()
    await sup.stop()


def make_app() -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=16 * 1024 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/favicon.ico", favicon)
    r.add_get("/dashboard.js", dashboard_js)
    r.add_static("/ds/", STATIC / "ds")  # the Shrine Fox design system: css, mascots, backdrops
    r.add_get("/healthz", healthz)
    r.add_get("/api/auth", auth_state)
    r.add_get("/auth/discord", discord_login.start)
    r.add_get("/auth/discord/callback", discord_login.callback)
    r.add_route("*", "/api/dash/{tail:.+}", dash_proxy)
    r.add_get("/api/profile", profile)
    r.add_post("/api/profile", profile)
    r.add_post("/api/setup", setup)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_post("/api/password", change_password)
    r.add_get("/api/status", status)
    r.add_post("/api/bot/{action}", bot_action)
    r.add_post("/api/control", control)
    r.add_get("/api/logs", logs)
    r.add_get("/api/logs/stream", logs_stream)
    r.add_get("/api/logs/download", logs_download)
    r.add_post("/api/logs/clear", logs_clear)
    r.add_get("/api/settings", get_settings)
    r.add_post("/api/settings", put_settings)
    r.add_get("/api/versions", versions)
    r.add_post("/api/maintenance/update-ytdlp", update_ytdlp_route)
    r.add_post("/api/cookies", upload_cookies)
    r.add_delete("/api/cookies", delete_cookies)
    app.on_startup.append(_boot)
    app.on_shutdown.append(_shutdown)
    return app
