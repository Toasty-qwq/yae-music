"""Settings shared by the bot and the panel.

Everything lives in ``<DATA_DIR>/settings.json``. The panel writes it, the bot reads it once at
start (so a change needs a bot restart). Environment variables win over the file, which is handy
for docker-compose users who prefer env vars to the panel.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or ROOT / "data").resolve()
SETTINGS_FILE = DATA_DIR / "settings.json"
COOKIES_FILE = DATA_DIR / "cookies.txt"
LOG_DIR = DATA_DIR / "logs"

DEFAULTS: dict = {
    "discord_token": "",
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "default_volume": 50,        # percent, 0-200
    "idle_timeout": 5,           # minutes alone / with nothing queued before leaving; 0 = never
    "max_playlist": 200,         # tracks taken from one playlist link
    "max_queue": 1000,
    "autostart": True,           # panel starts the bot when it boots
    "auto_restart": True,        # panel restarts the bot if it crashes
    "update_ytdlp_on_start": True,
    "ffmpeg_path": "ffmpeg",
    # Discord sign-in for the dashboard (server admins manage their own servers)
    "public_url": "",            # e.g. https://music.example.com, where people open the panel
    "oauth_client_secret": "",   # Developer Portal → OAuth2 → Client Secret
    "application_id": "",        # filled in by itself from the running bot
    "dash_permission": "administrator",  # who may manage a server on the dashboard: administrator | manage_guild
}

# env var -> settings key
ENV_OVERRIDES = {
    "DISCORD_TOKEN": "discord_token",
    "SPOTIFY_CLIENT_ID": "spotify_client_id",
    "SPOTIFY_CLIENT_SECRET": "spotify_client_secret",
    "FFMPEG_PATH": "ffmpeg_path",
    "PUBLIC_URL": "public_url",
    "OAUTH_CLIENT_SECRET": "oauth_client_secret",
}

_lock = threading.Lock()


def _read_file() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}


def load() -> dict:
    """Defaults <- settings.json <- environment."""
    data = dict(DEFAULTS)
    data.update({k: v for k, v in _read_file().items()})
    for env, key in ENV_OVERRIDES.items():
        if os.environ.get(env):
            data[key] = os.environ[env]
    return data


def env_locked() -> set[str]:
    """Keys currently forced by an environment variable (the panel shows them read-only)."""
    return {key for env, key in ENV_OVERRIDES.items() if os.environ.get(env)}


def update(changes: dict) -> dict:
    """Merge ``changes`` into settings.json (atomic write) and return the merged file contents."""
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = _read_file()
        data.update(changes)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, SETTINGS_FILE)
        return data


def raw() -> dict:
    """settings.json as stored (no defaults, no env) — the panel's own keys live here too."""
    return _read_file()
