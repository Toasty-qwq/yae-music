"""The bot's own database (``<DATA_DIR>/bot.db``): per-server settings, reaction-role menus and
moderation cases. Only the bot process opens it; the panel goes through the bot's API."""
from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

from .. import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id   INTEGER PRIMARY KEY,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rr_menus (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    kind        TEXT NOT NULL,          -- buttons | dropdown | reactions
    mode        TEXT NOT NULL,          -- normal | unique | verify
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    color       TEXT NOT NULL,
    roles       TEXT NOT NULL,          -- JSON [{role_id, label, emoji, description}]
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rr_by_guild ON rr_menus(guild_id);
CREATE TABLE IF NOT EXISTS cases (
    guild_id       INTEGER NOT NULL,
    case_no        INTEGER NOT NULL,
    action         TEXT NOT NULL,       -- ban | unban | kick | timeout | untimeout | warn
    user_id        INTEGER NOT NULL,
    user_name      TEXT NOT NULL,
    mod_id         INTEGER NOT NULL,
    mod_name       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    expires_at     REAL,                -- temporary bans / timeouts
    active         INTEGER NOT NULL DEFAULT 1,
    log_channel_id INTEGER,
    log_message_id INTEGER,
    PRIMARY KEY (guild_id, case_no)
);
CREATE INDEX IF NOT EXISTS cases_by_user ON cases(guild_id, user_id);
CREATE TABLE IF NOT EXISTS tags (
    guild_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    message    TEXT NOT NULL,           -- JSON message (content + embed)
    owner_id   INTEGER NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (guild_id, name)
);
CREATE TABLE IF NOT EXISTS posts (       -- messages posted from the dashboard (editable later)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    name        TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS starboard (
    guild_id        INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    star_message_id INTEGER,
    stars           INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);
CREATE TABLE IF NOT EXISTS suggestions (
    guild_id   INTEGER NOT NULL,
    number     INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    content    TEXT NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    status     TEXT NOT NULL DEFAULT 'open',
    mod_id     INTEGER,
    reason     TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (guild_id, number)
);
CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    guild_id   INTEGER,
    channel_id INTEGER,
    due        REAL NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(due);
CREATE TABLE IF NOT EXISTS forum_grants (  -- forum posts whose tag already gave their author a role
    guild_id   INTEGER NOT NULL,
    thread_id  INTEGER NOT NULL,
    tag_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    granted_at REAL NOT NULL,
    PRIMARY KEY (guild_id, thread_id, tag_id)
);
CREATE TABLE IF NOT EXISTS vm_channels (   -- temporary voice channels made by the VC maker
    channel_id      INTEGER PRIMARY KEY,
    guild_id        INTEGER NOT NULL,
    owner_id        INTEGER,
    setup_id        TEXT NOT NULL,
    text_channel_id INTEGER,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vm_profiles (   -- someone's saved channel settings, used on every server
    user_id INTEGER PRIMARY KEY,
    data    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_panels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    data       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    guild_id      INTEGER NOT NULL,
    number        INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL UNIQUE,
    owner_id      INTEGER NOT NULL,
    panel_id      INTEGER,
    type_id       TEXT,
    type_label    TEXT,
    status        TEXT NOT NULL DEFAULT 'open',  -- open | closed | deleted
    claimed_by    INTEGER,
    answers       TEXT,
    created_at    REAL NOT NULL,
    closed_at     REAL,
    close_reason  TEXT,
    last_activity REAL NOT NULL,
    PRIMARY KEY (guild_id, number)
);
CREATE INDEX IF NOT EXISTS tickets_owner ON tickets(guild_id, owner_id, status);
CREATE TABLE IF NOT EXISTS bot_state (     -- small things the bot keeps, like its presence
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_roles (  -- roles to give back when someone rejoins
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    role_ids TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path=None) -> "Database":
        path = path or settings.DATA_DIR / "bot.db"
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ---- small helpers for the simpler features ------------------------------------------------

    async def run(self, sql: str, args: tuple | list = ()) -> int:
        """Execute and commit; returns the new row id (for inserts)."""
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.lastrowid

    async def one(self, sql: str, args: tuple | list = ()) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def all(self, sql: str, args: tuple | list = ()) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---- guild config ----------------------------------------------------------------------

    async def guild_config(self, guild_id: int) -> dict:
        async with self.conn.execute("SELECT data FROM guild_config WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
        return json.loads(row["data"]) if row else {}

    async def save_guild_config(self, guild_id: int, data: dict) -> None:
        await self.conn.execute(
            "INSERT INTO guild_config(guild_id, data) VALUES(?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
            (guild_id, json.dumps(data)))
        await self.conn.commit()

    # ---- reaction-role menus ----------------------------------------------------------------

    @staticmethod
    def _menu(row) -> dict:
        d = dict(row)
        d["roles"] = json.loads(d["roles"])
        return d

    async def menus(self, guild_id: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM rr_menus", ()
        if guild_id is not None:
            sql, args = sql + " WHERE guild_id=?", (guild_id,)
        async with self.conn.execute(sql + " ORDER BY id", args) as cur:
            return [self._menu(r) for r in await cur.fetchall()]

    async def menu(self, menu_id: int) -> dict | None:
        async with self.conn.execute("SELECT * FROM rr_menus WHERE id=?", (menu_id,)) as cur:
            row = await cur.fetchone()
        return self._menu(row) if row else None

    async def save_menu(self, m: dict) -> int:
        cols = ("guild_id", "channel_id", "message_id", "kind", "mode", "title", "description", "color")
        vals = [m.get(c) for c in cols] + [json.dumps(m["roles"])]
        if m.get("id"):
            await self.conn.execute(
                f"UPDATE rr_menus SET {', '.join(c + '=?' for c in cols)}, roles=? WHERE id=?", (*vals, m["id"]))
            menu_id = m["id"]
        else:
            cur = await self.conn.execute(
                f"INSERT INTO rr_menus({', '.join(cols)}, roles, created_at) VALUES({', '.join('?' * (len(cols) + 2))})",
                (*vals, time.time()))
            menu_id = cur.lastrowid
        await self.conn.commit()
        return menu_id

    async def delete_menu(self, menu_id: int) -> None:
        await self.conn.execute("DELETE FROM rr_menus WHERE id=?", (menu_id,))
        await self.conn.commit()

    # ---- cases -------------------------------------------------------------------------------

    async def add_case(self, guild_id: int, **c: Any) -> dict:
        async with self.conn.execute("SELECT COALESCE(MAX(case_no), 0) + 1 FROM cases WHERE guild_id=?",
                                     (guild_id,)) as cur:
            case_no = (await cur.fetchone())[0]
        row = {"guild_id": guild_id, "case_no": case_no, "created_at": time.time(), "expires_at": None,
               "active": 1, "log_channel_id": None, "log_message_id": None, **c}
        cols = list(row)
        await self.conn.execute(f"INSERT INTO cases({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                                [row[k] for k in cols])
        await self.conn.commit()
        return row

    async def case(self, guild_id: int, case_no: int) -> dict | None:
        async with self.conn.execute("SELECT * FROM cases WHERE guild_id=? AND case_no=?", (guild_id, case_no)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_case(self, guild_id: int, case_no: int, **changes: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in changes)
        await self.conn.execute(f"UPDATE cases SET {sets} WHERE guild_id=? AND case_no=?",
                                (*changes.values(), guild_id, case_no))
        await self.conn.commit()

    async def cases(self, guild_id: int, *, user_id: int | None = None, action: str | None = None,
                    limit: int = 25, offset: int = 0) -> tuple[list[dict], int]:
        where, args = ["guild_id=?"], [guild_id]
        if user_id is not None:
            where.append("user_id=?")
            args.append(user_id)
        if action:
            where.append("action=?")
            args.append(action)
        clause = " AND ".join(where)
        async with self.conn.execute(f"SELECT COUNT(*) FROM cases WHERE {clause}", args) as cur:
            total = (await cur.fetchone())[0]
        async with self.conn.execute(f"SELECT * FROM cases WHERE {clause} ORDER BY case_no DESC LIMIT ? OFFSET ?",
                                     (*args, limit, offset)) as cur:
            return [dict(r) for r in await cur.fetchall()], total

    async def due_tempbans(self, now: float) -> list[dict]:
        async with self.conn.execute(
                "SELECT * FROM cases WHERE action='ban' AND active=1 AND expires_at IS NOT NULL AND expires_at<=?",
                (now,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def deactivate_bans(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute("UPDATE cases SET active=0 WHERE guild_id=? AND user_id=? AND action='ban'",
                                (guild_id, user_id))
        await self.conn.commit()
