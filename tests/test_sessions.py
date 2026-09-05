import json

import pytest

import sessions


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path)
    return tmp_path


def write(dir_, sid, *events):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in events))


def test_slug_replaces_every_non_alnum():
    # Не только `/`: подчёркивание в пути ломало поиск папки и давало пустой список.
    # Хвост, а не всю строку: на macOS realpath дописывает /System/Volumes/Data.
    assert sessions._slug("/home/user_1/my.proj").endswith("-home-user-1-my-proj")


def test_recent_title_and_order(transcripts, tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    d = transcripts / sessions._slug(str(cwd))
    write(d, "old", {"type": "last-prompt", "lastPrompt": "первый"})
    write(d, "new", {"type": "last-prompt", "lastPrompt": "второй"},
          {"type": "ai-title", "aiTitle": "Заголовок"})
    (d / "old.jsonl").touch()  # старую делаем свежее — проверяем сортировку по mtime

    got = sessions.recent(str(cwd))
    assert [(sid, name) for sid, name, _ in got] == [("old", "первый"), ("new", "Заголовок")]
    assert all(age >= 0 for _, _, age in got)


@pytest.mark.parametrize(
    ("seconds", "want"),
    [(5, "5с"), (60, "1м"), (3599, "59м"), (3600, "1ч"), (86400, "1д"), (86400 * 400, "1г")],
)
def test_ago(seconds, want):
    assert sessions.ago(seconds) == want


def test_recent_missing_project(transcripts, tmp_path):
    assert sessions.recent(str(tmp_path / "nope")) == []


def test_title_prefers_ai_title_over_prompt(transcripts, tmp_path):
    # headless-запуски бота пишут только last-prompt, интерактивные — ещё и ai-title
    cwd = tmp_path / "p"
    cwd.mkdir()
    d = transcripts / sessions._slug(str(cwd))
    write(d, "s", {"type": "last-prompt", "lastPrompt": "промпт"})
    assert sessions.title(d / "s.jsonl") == "промпт"
