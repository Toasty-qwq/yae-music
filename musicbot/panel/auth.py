"""Panel password + signed session cookie.

The password hash and the cookie-signing secret live in settings.json. ``PANEL_PASSWORD`` in the
environment overrides the stored password (useful if you forget it).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from .. import settings

COOKIE = "musicbot_session"
SESSION_DAYS = 30


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _stored() -> str:
    return settings.raw().get("panel_password", "")


def needs_setup() -> bool:
    return not os.environ.get("PANEL_PASSWORD") and not _stored()


def set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    enc = lambda b: base64.b64encode(b).decode()  # noqa: E731
    settings.update({"panel_password": f"scrypt${enc(salt)}${enc(_hash(password, salt))}"})


def check_password(password: str) -> bool:
    env = os.environ.get("PANEL_PASSWORD")
    if env:
        return hmac.compare_digest(password.encode(), env.encode())
    stored = _stored()
    try:
        _, salt, digest = stored.split("$")
        return hmac.compare_digest(_hash(password, base64.b64decode(salt)), base64.b64decode(digest))
    except ValueError:
        return False


def _secret() -> bytes:
    s = settings.raw().get("panel_secret")
    if not s:
        s = secrets.token_hex(32)
        settings.update({"panel_secret": s})
    # changing the password invalidates every session
    return (s + "|" + (os.environ.get("PANEL_PASSWORD") or _stored())).encode()


def make_session() -> str:
    expiry = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def valid_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    expiry, sig = value.split(".", 1)
    if not expiry.isdigit() or int(expiry) < time.time():
        return False
    good = hmac.new(_secret(), expiry.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)
