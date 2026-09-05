"""Состояние бота: SQLite в /data/bot.db. Две таблицы, схема через IF NOT EXISTS."""

import os
import sqlite3
import time

DB_PATH = os.environ.get("BOT_DB", "/data/bot.db")

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, isolation_level=None)
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                project    TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
    return _conn


def get(key: str, default: str | None = None) -> str | None:
    row = conn().execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def put(key: str, value: str | None) -> None:
    if value is None:
        conn().execute("DELETE FROM state WHERE key = ?", (key,))
    else:
        conn().execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def session_of(project: str) -> str | None:
    row = conn().execute(
        "SELECT session_id FROM sessions WHERE project = ?", (project,)
    ).fetchone()
    return row[0] if row else None


def save_session(project: str, session_id: str) -> None:
    conn().execute(
        "INSERT INTO sessions (project, session_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(project) DO UPDATE SET session_id = excluded.session_id, "
        "updated_at = excluded.updated_at",
        (project, session_id, int(time.time())),
    )


def drop_session(project: str) -> None:
    conn().execute("DELETE FROM sessions WHERE project = ?", (project,))
