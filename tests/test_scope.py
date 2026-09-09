"""Топик форума = свой проект и своя сессия. Тут проверяется именно разделение:
промах в ключе означал бы, что две сессии молча пишут друг в друга."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TG_BOT_TOKEN", "x")  # app читает env на импорте

import app
import store


def msg(thread=None, topic=False):
    return SimpleNamespace(message_thread_id=thread, is_topic_message=topic)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store, "_conn", None)
    return store


def test_scope_topic_vs_plain_reply():
    assert app.scope(msg(thread=42, topic=True)) == "42"
    assert app.scope(msg()) == "0"
    # Обычный реплай в личке тоже приносит message_thread_id — состояние по нему
    # расползлось бы на скоуп за сообщение. Спасает только is_topic_message.
    assert app.scope(msg(thread=777, topic=False)) == "0"


def test_sessions_isolated_per_scope(db):
    db.save_session("10", "/projects/a", "sess-10")
    db.save_session("20", "/projects/a", "sess-20")
    assert db.session_of("10", "/projects/a") == "sess-10"
    assert db.session_of("20", "/projects/a") == "sess-20"

    db.drop_session("10", "/projects/a")
    assert db.session_of("10", "/projects/a") is None
    assert db.session_of("20", "/projects/a") == "sess-20"


def test_same_scope_remembers_session_per_project(db):
    db.save_session("10", "/projects/a", "sess-a")
    db.save_session("10", "/projects/b", "sess-b")
    assert db.session_of("10", "/projects/a") == "sess-a"


def test_live_keys_finds_every_scope(db):
    db.put("10:live", "1:100")
    db.put("20:live", "1:200")
    db.put("10:cwd", "/projects/a")  # не :live — в выборку попасть не должен
    assert dict(db.live_keys()) == {"10:live": "1:100", "20:live": "1:200"}
