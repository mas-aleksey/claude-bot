"""Запуск сессии из браузера: границы доверия и передача session_id.

Клиент задаёт `project` — он становится рабочим каталогом процесса claude, поэтому
проверка «примонтирован ли» тут не косметика. `pane` идёт ключом в runner._runs.
"""

import asyncio
import json
import os
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("TG_BOT_TOKEN", "x")  # app читает env на импорте

import runner
import sessions
import store
import webui


@pytest.fixture
async def client(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    monkeypatch.setattr(sessions, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "transcripts")
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store, "_conn", None)
    c = TestClient(TestServer(webui.build()))
    await c.start_server()
    yield c
    await c.close()


@pytest.fixture
def fake_run(monkeypatch):
    """runner.run → поток из двух событий. Настоящий поднимал бы claude."""
    calls = []

    async def fake(prompt, cwd, session_id=None, model=None, scope="0"):
        calls.append({"prompt": prompt, "cwd": cwd, "session_id": session_id, "scope": scope})
        yield {"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555"}
        yield {"type": "result", "result": "готово"}

    monkeypatch.setattr(runner, "run", fake)
    monkeypatch.setattr(runner, "busy", lambda scope: False)
    return calls


async def test_prompt_starts_run_and_returns_session(client, fake_run, tmp_path):
    r = await client.post("/api/prompt", json={
        "pane": "pane-1", "project": str(tmp_path / "proj"), "prompt": "почини тесты",
    })
    assert r.status == 200
    assert (await r.json())["session"] == "11111111-2222-3333-4444-555555555555"

    await asyncio.sleep(0)  # дать фоновой задаче добежать до конца
    assert fake_run[0]["prompt"] == "почини тесты"
    assert fake_run[0]["cwd"] == str(tmp_path / "proj")
    assert fake_run[0]["scope"] == "web:pane-1"     # скоуп панели, не общий слот
    assert fake_run[0]["session_id"] is None        # новая сессия


async def test_prompt_resumes_given_session(client, fake_run, tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    await client.post("/api/prompt", json={
        "pane": "pane-1", "project": str(tmp_path / "proj"), "session": sid, "prompt": "дальше",
    })
    assert fake_run[0]["session_id"] == sid


@pytest.mark.parametrize("body", [
    {"pane": "pane-1", "prompt": "x", "project": "/etc"},                  # чужой каталог
    {"pane": "pane-1", "prompt": "x", "project": "/projects/../etc"},      # обход
    {"pane": "pane-1", "prompt": "   ", "project": "PROJ"},                # пустой промпт
    {"pane": "ой", "prompt": "x", "project": "PROJ"},                      # мусор в pane
    {"pane": "pane-1", "prompt": "x", "project": "PROJ", "session": "../x"},
])
async def test_prompt_rejects_bad_input(client, fake_run, tmp_path, body):
    if body.get("project") == "PROJ":
        body["project"] = str(tmp_path / "proj")
    r = await client.post("/api/prompt", json=body)
    assert r.status == 400
    assert fake_run == []  # ни одного запуска не поднялось


async def test_prompt_conflicts_when_pane_busy(client, fake_run, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "busy", lambda scope: scope == "web:pane-1")
    r = await client.post("/api/prompt", json={
        "pane": "pane-1", "project": str(tmp_path / "proj"), "prompt": "x"})
    assert r.status == 409
    assert fake_run == []


async def test_status_lists_busy_scopes(client, monkeypatch):
    monkeypatch.setattr(runner, "active", lambda: ["web:pane-1", "5"])
    r = await client.get("/api/status")
    assert (await r.json())["busy"] == ["web:pane-1", "5"]


async def test_cancel_hits_own_scope(client, monkeypatch):
    seen = []

    async def fake_cancel(scope):
        seen.append(scope)
        return True

    monkeypatch.setattr(runner, "cancel", fake_cancel)
    r = await client.post("/api/cancel", json={"pane": "pane-1"})
    assert (await r.json())["stopped"] is True
    assert seen == ["web:pane-1"]


def test_active_reports_only_live(monkeypatch):
    class Proc:
        def __init__(self, rc):
            self.returncode = rc

    monkeypatch.setattr(runner, "_runs", {"a": Proc(None), "b": Proc(0)})
    assert runner.active() == ["a"]


async def test_messages_empty_until_transcript_appears(client, tmp_path):
    """Панель начинает опрос сразу после ответа /api/prompt, а файл появляется позже."""
    q = {"project": str(tmp_path / "proj"), "id": "11111111-2222-3333-4444-555555555555",
         "from": "7"}
    r = await client.get("/api/messages", params=q)
    assert r.status == 200
    assert await r.json() == {"next": 7, "items": []}  # оффсет не сбрасывается


@pytest.fixture
def failing_run(monkeypatch):
    """runner.run, который отдаёт session_id и следом падение с кодом возврата."""
    async def fake(prompt, cwd, session_id=None, model=None, scope="0"):
        yield {"type": "system", "session_id": "11111111-2222-3333-4444-555555555555"}
        yield {"type": "_bot", "kind": "error", "rc": 2, "text": "claude: no such option"}

    monkeypatch.setattr(runner, "run", fake)
    monkeypatch.setattr(runner, "busy", lambda scope: False)
    monkeypatch.setattr(webui, "_errors", {})


async def test_failed_run_shows_up_in_status(client, failing_run, tmp_path):
    """Упавший прогон обязан быть виден в браузере: иначе панель просто молчит."""
    await client.post("/api/prompt", json={
        "pane": "pane-1", "project": str(tmp_path / "proj"), "prompt": "x"})
    await asyncio.sleep(0)

    errors = (await (await client.get("/api/status")).json())["errors"]
    assert "rc=2" in errors["web:pane-1"]
    assert "no such option" in errors["web:pane-1"]


async def test_new_run_clears_previous_error(client, fake_run, tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "_errors", {"web:pane-1": "rc=2 старое"})
    await client.post("/api/prompt", json={
        "pane": "pane-1", "project": str(tmp_path / "proj"), "prompt": "x"})
    await asyncio.sleep(0)

    errors = (await (await client.get("/api/status")).json())["errors"]
    assert "web:pane-1" not in errors


async def test_result_error_text_beats_bare_return_code(client, monkeypatch, tmp_path):
    """Причина приходит в `result`, а стоп-код — это всегда просто rc=1 при пустом
    stderr. Поймано живьём на лимите подписки: панель показывала «rc=1» и молчала
    о том, что лимит исчерпан."""
    async def fake(prompt, cwd, session_id=None, model=None, scope="0"):
        yield {"type": "system", "session_id": "11111111-2222-3333-4444-555555555555"}
        yield {"type": "result", "is_error": True, "result": "You've hit your session limit"}
        yield {"type": "_bot", "kind": "error", "rc": 1, "text": ""}

    monkeypatch.setattr(runner, "run", fake)
    monkeypatch.setattr(runner, "busy", lambda scope: False)
    monkeypatch.setattr(webui, "_errors", {})

    await client.post("/api/prompt", json={
        "pane": "pane-1", "project": str(tmp_path / "proj"), "prompt": "x"})
    await asyncio.sleep(0)

    assert (await (await client.get("/api/status")).json())["errors"] == {
        "web:pane-1": "You've hit your session limit"}


@pytest.mark.parametrize("stderr,expect", [
    ("No conversation found with session ID: x", "No conversation found with session ID: x (rc=1)"),
    ("", "claude вышел с кодом 1 и ничего не сообщил — "
         "причина, если она есть, в последнем ответе выше"),
])
async def test_stderr_text_goes_to_panel(client, monkeypatch, tmp_path, stderr, expect):
    """Голый код возврата ничего не объясняет. Текст из stderr идёт вперёд, код в скобки,
    а при пустом stderr панель хотя бы говорит, куда смотреть."""
    async def fake(prompt, cwd, session_id=None, model=None, scope="0"):
        yield {"type": "system", "session_id": "11111111-2222-3333-4444-555555555555"}
        yield {"type": "_bot", "kind": "error", "rc": 1, "text": stderr}

    monkeypatch.setattr(runner, "run", fake)
    monkeypatch.setattr(runner, "busy", lambda scope: False)
    monkeypatch.setattr(webui, "_errors", {})

    await client.post("/api/prompt", json={
        "pane": "pane-1", "project": str(tmp_path / "proj"), "prompt": "x"})
    await asyncio.sleep(0)

    assert (await (await client.get("/api/status")).json())["errors"]["web:pane-1"] == expect


async def test_search_endpoint_returns_snippets(client, tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    d = tmp_path / "transcripts" / sessions._slug(str(cwd))
    d.mkdir(parents=True)
    (d / "11111111-2222-3333-4444-555555555555.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "почини докер"}},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "transcripts")

    r = await client.get("/api/search", params={"project": str(cwd), "q": "докер"})
    (row,) = await r.json()
    assert row["id"] == "11111111-2222-3333-4444-555555555555"
    assert "докер" in row["snippet"]


async def test_search_without_query_is_empty(client, tmp_path):
    r = await client.get("/api/search", params={"project": str(tmp_path / "proj"), "q": " "})
    assert await r.json() == []


async def test_session_list_shows_size_only_for_heavy(client, tmp_path, monkeypatch):
    """Цифра у каждой сессии — шум: у большинства она одинаково мелкая. Показываем
    только те, что открываются заметно дольше."""
    cwd = tmp_path / "proj"
    d = tmp_path / "transcripts" / sessions._slug(str(cwd))
    d.mkdir(parents=True)
    line = json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n"
    (d / "aaaaaaaa-2222-3333-4444-555555555555.jsonl").write_text(line)
    (d / "bbbbbbbb-2222-3333-4444-555555555555.jsonl").write_text(
        line + "#" * (2 << 20))
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "transcripts")

    rows = {r["id"][:8]: r["size"] for r in
            await (await client.get("/api/sessions", params={"project": str(cwd)})).json()}
    assert rows["aaaaaaaa"] == ""
    assert rows["bbbbbbbb"].endswith("МБ")


@pytest.fixture
def stale_home(tmp_path, monkeypatch):
    """Одна старая сессия и одна свежая, как в test_purge, но через эндпоинт."""
    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path / "projects")
    proj = tmp_path / "projects" / "-projects-proj"
    proj.mkdir(parents=True)
    line = json.dumps({"type": "user", "message": {"role": "user", "content": "тест"}},
                      ensure_ascii=False) + "\n"
    for sid, age in (("aaaaaaaa-1111-4111-8111-111111111111", 5 * 86400),
                     ("bbbbbbbb-2222-4222-8222-222222222222", 60)):
        path = proj / f"{sid}.jsonl"
        path.write_text(line, encoding="utf-8")
        os.utime(path, (time.time() - age, time.time() - age))
    return tmp_path


async def test_purge_preview_does_not_delete(client, stale_home):
    r = await client.get("/api/purge", params={"days": "2"})
    plan = await r.json()
    assert [s["id"][:8] for s in plan["sessions"]] == ["aaaaaaaa"]
    assert plan["bytes"] > 0
    # Файл на месте: GET обязан быть безопасным.
    assert (stale_home / "projects" / "-projects-proj" /
            "aaaaaaaa-1111-4111-8111-111111111111.jsonl").exists()


async def test_purge_post_deletes_and_clears_pointers(client, stale_home, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", str(stale_home / "bot.db"))
    monkeypatch.setattr(store, "_conn", None)
    store.save_session("0", "/projects/proj", "aaaaaaaa-1111-4111-8111-111111111111")

    killed = await (await client.post("/api/purge", json={"days": 2})).json()
    assert killed["sessions"] == 1
    assert killed["pointers"] == 1
    assert store.session_of("0", "/projects/proj") is None
    assert (stale_home / "projects" / "-projects-proj" /
            "bbbbbbbb-2222-4222-8222-222222222222.jsonl").exists()


@pytest.mark.parametrize("raw,want", [("2", 2.0), ("0", 0.5), ("-9", 0.5),
                                      ("абв", 2.0), (None, 2.0), ("7.5", 7.5)])
def test_days_has_a_floor(raw, want):
    """days=0 снесло бы и сегодняшнюю работу. «Удалить всё» — не то же самое, что
    «удалить старое»."""
    assert webui._days(raw) == want
