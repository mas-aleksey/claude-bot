"""Поиск по сессиям. Проверяем то, что легко сделать неправильно: регистр, границы
фрагмента и отказ искать в выводе инструментов."""

import json

import pytest

import sessions


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "t")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return cwd


def write(cwd, sid, *events):
    d = sessions.TRANSCRIPTS / sessions.slug(str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False — как пишет сам claude: в его транскриптах текст лежит
    # литералами UTF-8, а не escape-последовательностями. На это опирается быстрый
    # поиск по подстроке, поэтому фикстура обязана писать так же.
    (d / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")


def prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def answer(text):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_finds_in_prompt_and_answer(project):
    write(project, "1111", prompt("почини докер"))
    write(project, "2222", answer("докер починен"))
    write(project, "3333", prompt("совсем про другое"))

    got = sessions.search(str(project), "докер")
    assert {sid for sid, *_ in got} == {"1111", "2222"}


def test_case_insensitive_for_cyrillic(project):
    write(project, "1111", prompt("Проверь ДОКЕР на стенде"))
    assert len(sessions.search(str(project), "докер")) == 1


def test_tool_output_is_not_searched(project):
    """Попадание в tool_result означало бы «нашлось там, где ты ничего не писал»."""
    write(project, "1111", prompt("привет"),
          {"type": "user", "message": {"role": "user", "content": [
              {"type": "tool_result", "content": "докер лог на мегабайт"}]}})
    assert sessions.search(str(project), "докер") == []


def test_snippet_shows_context_around_hit(project):
    write(project, "1111", prompt("а" * 200 + " докер " + "б" * 200))
    (_, _, _, snippet), = sessions.search(str(project), "докер")
    assert "докер" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) < 200


def test_empty_query_finds_nothing(project):
    write(project, "1111", prompt("что угодно"))
    assert sessions.search(str(project), "   ") == []


def test_limit_and_order(project, monkeypatch):
    import os
    for i, sid in enumerate(["1111", "2222", "3333"]):
        write(project, sid, prompt("докер"))
        path = sessions.TRANSCRIPTS / sessions.slug(str(project)) / f"{sid}.jsonl"
        os.utime(path, (1000 + i, 1000 + i))  # свежесть задаём явно: overlayfs огрубляет mtime

    got = sessions.search(str(project), "докер", limit=2)
    assert [sid for sid, *_ in got] == ["3333", "2222"]


async def test_search_without_project_covers_them_all(tmp_path, monkeypatch):
    """Панель спрашивает «где я это обсуждал»: без `project` ручка идёт по всем проектам
    сразу и подписывает каждую строку её каталогом, иначе открыть найденное нечем."""
    import os
    import threading

    from aiohttp.test_utils import TestClient, TestServer

    os.environ.setdefault("TG_BOT_TOKEN", "x")
    import store
    import webui

    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "t")
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store, "_local", threading.local())
    one, two = tmp_path / "projects" / "one", tmp_path / "projects" / "two"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    write(one, "11111111-1111-4111-8111-111111111111", prompt("почини докер"))
    write(two, "22222222-2222-4222-8222-222222222222", answer("докер починен"))
    write(two, "33333333-3333-4333-8333-333333333333", prompt("про другое"))

    c = TestClient(TestServer(webui.build()))
    await c.start_server()
    try:
        rows = await (await c.get("/api/search", params={"q": "докер"})).json()
    finally:
        await c.close()

    assert {r["project"] for r in rows} == {str(one), str(two)}
    assert {r["id"][:8] for r in rows} == {"11111111", "22222222"}
