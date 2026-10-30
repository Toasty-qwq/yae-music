"""One message format for everything the bot posts for a server: text, plus an optional embed
built field by field (author, title, description, up to 25 fields, image, thumbnail, footer,
timestamp, colour). Image fields take a link, a placeholder like {user.avatar}, or a file
uploaded on the dashboard (stored as ``upload:<name>`` and attached when sent, so GIFs work)."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
from pathlib import Path

import discord

from .. import settings
from .errors import HEX, ConfigError
from .templates import SAFE_MENTIONS, TOKEN, Ctx, color, render

EMBED = {
    "enabled": False, "color": "#f2879f",
    "author_name": "", "author_icon": "", "author_url": "",
    "title": "", "url": "", "description": "",
    "thumbnail": "", "image": "",
    "footer": "", "footer_icon": "", "timestamp": False,
    "fields": [],
}
URL_KEYS = ("author_icon", "author_url", "url", "thumbnail", "image", "footer_icon")
IMAGE_KEYS = ("author_icon", "thumbnail", "image", "footer_icon")
LIMITS = {"author_name": 256, "title": 256, "description": 4096, "footer": 2048}
MEDIA_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_MEDIA = 8 * 1024 * 1024


def message(content: str = "", **embed) -> dict:
    """A message dict; any embed keys given switch the embed on."""
    e = copy.deepcopy(EMBED)
    e.update(embed)
    if embed:
        e["enabled"] = embed.get("enabled", True)
    return {"content": content, "embed": e}


def upgrade(old) -> dict:
    """Older settings kept the text as a plain string."""
    if isinstance(old, dict) and "embed" in old:
        e = copy.deepcopy(EMBED)
        e.update({k: v for k, v in (old.get("embed") or {}).items() if k in EMBED})
        return {"content": str(old.get("content") or ""), "embed": e}
    return message(str(old or ""))


# ---- validation (what the dashboard sends) ----------------------------------------------------

def _url_ok(v: str) -> bool:
    return (not v or v.startswith(("https://", "http://")) or v.startswith("upload:")
            or bool(TOKEN.fullmatch(v.strip())))


def validate(data, label: str = "The message") -> dict:
    if not isinstance(data, dict):
        raise ConfigError(f"{label} is missing.")
    content = str(data.get("content") or "")
    if len(content) > 2000:
        raise ConfigError(f"{label}: the text above the embed is {len(content)} characters; Discord allows 2000.")
    src = data.get("embed") or {}
    e = copy.deepcopy(EMBED)
    e["enabled"] = bool(src.get("enabled"))
    e["timestamp"] = bool(src.get("timestamp"))
    c = str(src.get("color") or EMBED["color"])
    if not HEX.match(c):
        raise ConfigError(f"{label}: {c!r} isn't a colour. Use the #rrggbb form.")
    e["color"] = c.lower()
    for k in ("author_name", "title", "description", "footer"):
        v = str(src.get(k) or "")
        if len(v) > LIMITS[k]:
            raise ConfigError(f"{label}: the {k.replace('_', ' ')} is {len(v)} characters; Discord allows {LIMITS[k]}.")
        e[k] = v
    for k in URL_KEYS:
        v = str(src.get(k) or "").strip()
        if not _url_ok(v):
            raise ConfigError(f"{label}: the {k.replace('_', ' ')} must be a link starting with https://, "
                              "an uploaded file, or a placeholder like {user.avatar}.")
        e[k] = v
    fields = src.get("fields") or []
    if len(fields) > 25:
        raise ConfigError(f"{label}: an embed holds at most 25 fields.")
    for f in fields:
        name, value = str(f.get("name") or "").strip(), str(f.get("value") or "").strip()
        if not name or not value:
            raise ConfigError(f"{label}: every field needs a name and a value (or remove the empty one).")
        if len(name) > 256 or len(value) > 1024:
            raise ConfigError(f"{label}: a field name holds 256 characters and a value 1024.")
        e["fields"].append({"name": name, "value": value, "inline": bool(f.get("inline"))})
    total = sum(len(e[k]) for k in LIMITS) + sum(len(f["name"]) + len(f["value"]) for f in e["fields"])
    if total > 6000:
        raise ConfigError(f"{label}: the embed has {total} characters in all; Discord allows 6000.")
    visible = any(e[k] for k in ("author_name", "title", "description", "thumbnail", "image", "footer")) or e["fields"]
    if e["enabled"] and not visible:
        raise ConfigError(f"{label}: the embed is empty. Give it a title, a description, a field or an image, "
                          "or switch the embed off.")
    if not content.strip() and not e["enabled"]:
        raise ConfigError(f"{label}: write some text, or switch the embed on.")
    return {"content": content, "embed": e}


# ---- uploaded media --------------------------------------------------------------------------

def media_dir(guild_id: int) -> Path:
    return settings.DATA_DIR / "media" / str(int(guild_id))


def save_media(guild_id: int, filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in MEDIA_TYPES:
        raise ConfigError("Upload a PNG, JPG, GIF or WEBP picture.")
    if len(data) > MAX_MEDIA:
        raise ConfigError(f"That file is {len(data) // 1024 // 1024} MB; uploads can be up to 8 MB.")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(filename).stem).strip("-")[:40] or "image"
    name = f"{stem}-{hashlib.sha1(data).hexdigest()[:8]}{ext}"
    d = media_dir(guild_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return f"upload:{name}"


def media_path(guild_id: int, token_or_name: str) -> Path | None:
    name = token_or_name.removeprefix("upload:")
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-z]{3,4}", name):
        return None
    p = media_dir(guild_id) / name
    return p if p.is_file() else None


# ---- sending ---------------------------------------------------------------------------------

def build(msg: dict, ctx: Ctx) -> dict:
    """Keyword arguments for ``channel.send`` / ``message.edit``: content, embed, files."""
    msg = upgrade(msg)
    content = render(msg["content"], ctx)[:2000] or None
    files: dict[str, discord.File] = {}

    def media(value: str) -> str | None:
        v = render(value, ctx, url=True).strip()
        if v.startswith("upload:"):
            path = media_path(ctx.guild.id, v)
            if not path:
                return None
            if path.name not in files:
                files[path.name] = discord.File(path, filename=path.name)
            return f"attachment://{path.name}"
        return v if v.startswith(("https://", "http://")) else None

    embed = None
    e = msg["embed"]
    if e.get("enabled"):
        embed = discord.Embed(colour=color(e["color"]))
        embed.title = render(e["title"], ctx)[:256] or None
        embed.url = media(e["url"]) if e["url"] and embed.title else None
        embed.description = render(e["description"], ctx)[:4096] or None
        if e["author_name"]:
            embed.set_author(name=render(e["author_name"], ctx)[:256], url=media(e["author_url"]) if e["author_url"] else None,
                             icon_url=media(e["author_icon"]) if e["author_icon"] else None)
        for f in e["fields"][:25]:
            embed.add_field(name=render(f["name"], ctx)[:256] or "​", value=render(f["value"], ctx)[:1024] or "​",
                            inline=f.get("inline", False))
        if e["thumbnail"] and (u := media(e["thumbnail"])):
            embed.set_thumbnail(url=u)
        if e["image"] and (u := media(e["image"])):
            embed.set_image(url=u)
        if e["footer"] or e["footer_icon"]:
            embed.set_footer(text=render(e["footer"], ctx)[:2048] or None,
                             icon_url=media(e["footer_icon"]) if e["footer_icon"] else None)
        if e.get("timestamp"):
            embed.timestamp = dt.datetime.now(dt.timezone.utc)
        if not (embed.title or embed.description or embed.fields or embed.image or embed.thumbnail
                or embed.author or embed.footer):
            embed = None  # every part was a placeholder that came out empty
    if content is None and embed is None:
        content = "​"
    out = {"content": content, "embed": embed, "allowed_mentions": SAFE_MENTIONS}
    if files:
        out["files"] = list(files.values())
    return out


async def send(channel: discord.abc.Messageable, msg: dict, ctx: Ctx, **extra) -> discord.Message:
    return await channel.send(**build(msg, ctx), **extra)


async def edit(message: discord.Message, msg: dict, ctx: Ctx) -> discord.Message:
    kw = build(msg, ctx)
    files = kw.pop("files", [])
    return await message.edit(**kw, attachments=files)
