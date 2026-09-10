"""Удаление старых сессий. Действие необратимое, поэтому проверяем и то, что удаляется,
и то, что НЕ должно быть тронуто."""

import json
import os
import time

import pytest

import sessions

DAY = 86400
SID_OLD = "aaaaaaaa-1111-4111-8111-111111111111"
SID_NEW = "bbbbbbbb-2222-4222-8222-222222222222"
SID_GONE = "cccccccc-3333-4333-8333-333333333333"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Раскладка как у claude: projects рядом с session-env, file-history, history.jsonl."""
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "projects")
    proj = tmp_path / "projects" / "-projects-rp"
    proj.mkdir(parents=True)

    def transcript(sid, age):
        path = proj / f"{sid}.jsonl"
        path.write_text(json.dumps(
            {"type": "user", "message": {"role": "user", "content": "привет"}},
            ensure_ascii=False) + "\n", encoding="utf-8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    transcript(SID_OLD, 3 * DAY)
    transcript(SID_NEW, 3600)

    for sid, age in ((SID_OLD, 3 * DAY), (SID_NEW, 3600), (SID_GONE, 5 * DAY)):
        d = tmp_path / "session-env" / sid
        d.mkdir(parents=True)
        (d / "env").write_text("x")
        os.utime(d, (time.time() - age, time.time() - age))
    # Осиротевшее окружение молодой сессии — транскрипта ещё нет, трогать нельзя.
    fresh = tmp_path / "session-env" / "dddddddd-4444-4444-8444-444444444444"
    fresh.mkdir(parents=True)
    # Постороннее имя в том же каталоге не должно попасть под нож.
    (tmp_path / "session-env" / "not-a-uuid").mkdir()

    fh = tmp_path / "file-history" / SID_OLD
    fh.mkdir(parents=True)
    (fh / "1.json").write_text("{}")

    (tmp_path / "history.jsonl").write_text("\n".join([
        json.dumps({"display": "/model", "sessionId": SID_OLD}, ensure_ascii=False),
        json.dumps({"display": "живой промпт", "sessionId": SID_NEW}, ensure_ascii=False),
        json.dumps({"display": "битая строка"}, ensure_ascii=False)[:-1],  # без скобки
        "42",  # разбирается, но это не объект — на таком .get падал
    ]) + "\n", encoding="utf-8")
    return tmp_path


def test_stale_picks_only_old(home):
    got = sessions.stale(2 * DAY)
    assert [r["id"] for r in got] == [SID_OLD]
    assert got[0]["project"] == "-projects-rp"
    assert got[0]["bytes"] > 0


def test_running_session_is_out_of_reach(home):
    """У работающей сессии mtime обновляется постоянно — это и есть защита."""
    assert sessions.stale(0.5) != []          # всё старше половины секунды
    assert SID_NEW not in {r["id"] for r in sessions.stale(2 * DAY)}


def test_purge_removes_every_trace(home):
    killed = sessions.purge(2 * DAY)

    assert killed["sessions"] == 1
    assert killed["env"] == 1
    assert killed["file_history"] == 1
    assert killed["history_lines"] == 1
    assert killed["orphans"] == 1             # SID_GONE, окружение без транскрипта
    assert killed["bytes"] > 0
    assert killed["ids"] == [SID_OLD]  # по ним вызывающий снимает указатели в store

    assert not (home / "projects" / "-projects-rp" / f"{SID_OLD}.jsonl").exists()
    assert not (home / "session-env" / SID_OLD).exists()
    assert not (home / "session-env" / SID_GONE).exists()
    assert not (home / "file-history" / SID_OLD).exists()


def test_purge_keeps_the_young_and_the_unknown(home):
    sessions.purge(2 * DAY)

    assert (home / "projects" / "-projects-rp" / f"{SID_NEW}.jsonl").exists()
    assert (home / "session-env" / SID_NEW).exists()
    # Окружение свежей сессии без транскрипта: она могла только что начаться.
    assert (home / "session-env" / "dddddddd-4444-4444-8444-444444444444").exists()
    assert (home / "session-env" / "not-a-uuid").exists()


def test_history_keeps_live_lines_and_survives_broken_json(home):
    sessions.purge(2 * DAY)

    lines = (home / "history.jsonl").read_text().splitlines()
    assert any("живой промпт" in line for line in lines)
    assert not any(SID_OLD in line for line in lines)
    assert any("битая строка" in line for line in lines)  # неразбираемое не выбрасываем


def test_purge_on_empty_home_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "projects")
    (tmp_path / "projects").mkdir()
    assert sessions.purge(DAY) == {"sessions": 0, "bytes": 0, "env": 0, "ids": [],
                                   "file_history": 0, "history_lines": 0, "orphans": 0}


def test_stale_sorted_by_real_age_not_by_label(home, tmp_path):
    """Лексикографически «5ч» больше «15д». Список смотрят перед удалением, и
    перемешанный порядок там дороже всего."""
    proj = tmp_path / "projects" / "-projects-rp"
    for name, age in (("11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 15 * DAY),
                      ("22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb", 3 * DAY)):
        path = proj / f"{name}.jsonl"
        path.write_text("{}\n")
        os.utime(path, (time.time() - age, time.time() - age))

    got = sessions.stale(2 * DAY)
    assert [r["age"] for r in got] == sorted(r["age"] for r in got)
    assert got[-1]["ago"] == "15д"
