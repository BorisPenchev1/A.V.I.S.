"""SQLite foundation for AVIS: users and an app-event log.

This is the persistent store the home server grows into. It is intentionally
standalone — it imports nothing from :mod:`security` or :mod:`server`, so those
layers can build on it without an import cycle. SQLite is used for zero-setup on
a home box; the same schema migrates cleanly to Postgres later.

Tables
------
users   family/admin accounts (scrypt hash + salt live here, never plaintext)
events  an append log of app-level activity (logins, registrations, messages)
        that the dashboards summarize
meta    schema bookkeeping
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("AVIS_DB", str(PROJECT_ROOT / "avis.db")))
SCHEMA_VERSION = 1

_LOCK = threading.Lock()
_INITIALIZED = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they do not exist. Safe to call repeatedly."""
    global _INITIALIZED
    with _LOCK:
        if _INITIALIZED:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username   TEXT PRIMARY KEY,
                    role       TEXT NOT NULL,
                    salt       TEXT NOT NULL,
                    hash       TEXT NOT NULL,
                    created    TEXT NOT NULL,
                    last_seen  REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       REAL NOT NULL,
                    kind     TEXT NOT NULL,
                    username TEXT,
                    role     TEXT,
                    device   TEXT,
                    source   TEXT,
                    detail   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        _INITIALIZED = True


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user(username: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, role, salt, hash, created, last_seen FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def upsert_user(username: str, role: str, salt: str, hash_: str, created: str) -> None:
    init_db()
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO users(username, role, salt, hash, created)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET role=excluded.role,
                salt=excluded.salt, hash=excluded.hash
            """,
            (username, role, salt, hash_, created),
        )


def set_role(username: str, role: str) -> bool:
    init_db()
    with _LOCK, _connect() as conn:
        cur = conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
        return cur.rowcount > 0


def delete_user(username: str) -> bool:
    init_db()
    with _LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        return cur.rowcount > 0


def touch_user(username: str) -> None:
    init_db()
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE users SET last_seen = ? WHERE username = ?", (time.time(), username))


def list_users() -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, role, created, last_seen FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def count_users() -> int:
    init_db()
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def log_event(
    kind: str,
    *,
    username: str | None = None,
    role: str | None = None,
    device: str | None = None,
    source: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one activity event. Best-effort; never raises to the caller."""
    try:
        init_db()
        with _LOCK, _connect() as conn:
            conn.execute(
                "INSERT INTO events(ts, kind, username, role, device, source, detail) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (time.time(), kind, username, role, device, source,
                 json.dumps(detail or {}, default=str)),
            )
    except sqlite3.Error:
        pass


def recent_events(limit: int = 50, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT ts, kind, username, role, device, source, detail FROM events"
    params: list[Any] = []
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        query += f" WHERE kind IN ({placeholders})"
        params.extend(kinds)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item["detail"] = json.loads(item.get("detail") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["detail"] = {}
        result.append(item)
    return result


def count_events(kind: str, since_ts: float | None = None) -> int:
    init_db()
    query = "SELECT COUNT(*) FROM events WHERE kind = ?"
    params: list[Any] = [kind]
    if since_ts is not None:
        query += " AND ts >= ?"
        params.append(since_ts)
    with _connect() as conn:
        return int(conn.execute(query, params).fetchone()[0])


def count_events_for_user(kind: str, username: str, since_ts: float | None = None) -> int:
    """Like :func:`count_events` but scoped to a single account."""
    init_db()
    query = "SELECT COUNT(*) FROM events WHERE kind = ? AND username = ?"
    params: list[Any] = [kind, (username or "").strip().casefold()]
    if since_ts is not None:
        query += " AND ts >= ?"
        params.append(since_ts)
    with _connect() as conn:
        return int(conn.execute(query, params).fetchone()[0])


def recent_events_for_user(
    username: str, limit: int = 40, kinds: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    """Most recent activity for one account, newest first."""
    init_db()
    query = "SELECT ts, kind, username, role, device, source, detail FROM events WHERE username = ?"
    params: list[Any] = [(username or "").strip().casefold()]
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        query += f" AND kind IN ({placeholders})"
        params.extend(kinds)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item["detail"] = json.loads(item.get("detail") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["detail"] = {}
        result.append(item)
    return result


def user_devices(username: str) -> list[dict[str, Any]]:
    """Distinct devices/sources a user has been seen on, most recent first.

    Derived from the event log: each row groups a ``(source, device)`` pair with
    how many events came from it and when it was last active.
    """
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(source, 'unknown')  AS source,
                   COALESCE(device, '')          AS device,
                   COUNT(*)                       AS events,
                   MAX(ts)                        AS last_seen,
                   MIN(ts)                        AS first_seen
            FROM events
            WHERE username = ?
            GROUP BY source, device
            ORDER BY last_seen DESC
            """,
            ((username or "").strip().casefold(),),
        ).fetchall()
    return [dict(r) for r in rows]
