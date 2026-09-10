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
        # Разовая миграция под скоупы: состояние до топиков было одно на бота, теперь
        # это скоуп "0" (личка и группа без топиков). Идемпотентна — после первого
        # прогона старых ключей нет. Без неё живой инстанс терял текущий проект и все
        # указатели на сессии: `/sessions` их вернул бы, но молча и не сразу.
        # Новый ключ сессии всегда начинается со скоупа, старый — со слеша пути.
        _conn.execute("UPDATE state SET key = '0:' || key WHERE key IN ('cwd', 'live')")
        _conn.execute("UPDATE sessions SET project = '0:' || project WHERE project LIKE '/%'")
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


# Колонка `project` хранит составной ключ "<скоуп>:<путь>" — так топик форума помнит
# свою сессию в каждом проекте, а схема остаётся прежней и миграция не нужна.
def _key(scope: str, project: str) -> str:
    return f"{scope}:{project}"


def session_of(scope: str, project: str) -> str | None:
    row = conn().execute(
        "SELECT session_id FROM sessions WHERE project = ?", (_key(scope, project),)
    ).fetchone()
    return row[0] if row else None


def save_session(scope: str, project: str, session_id: str) -> None:
    conn().execute(
        "INSERT INTO sessions (project, session_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(project) DO UPDATE SET session_id = excluded.session_id, "
        "updated_at = excluded.updated_at",
        (_key(scope, project), session_id, int(time.time())),
    )


def drop_session(scope: str, project: str) -> None:
    conn().execute("DELETE FROM sessions WHERE project = ?", (_key(scope, project),))


def forget_sessions(ids: list[str]) -> int:
    """Снять указатели на удалённые сессии. Без этого `/sessions` в топике предложит
    мёртвый id, а `--resume` по нему падает с «No conversation found with session ID»."""
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn().execute(f"DELETE FROM sessions WHERE session_id IN ({marks})", tuple(ids))
    return cur.rowcount


def live_keys() -> list[tuple[str, str]]:
    """Указатели на живые «⏳»-сообщения, по одному на скоуп. Нужны после рестарта."""
    return conn().execute("SELECT key, value FROM state WHERE key LIKE '%:live'").fetchall()
