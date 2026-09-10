"""Запуск сессии из браузера: границы доверия и передача session_id.

Клиент задаёт `project` — он становится рабочим каталогом процесса claude, поэтому
проверка «примонтирован ли» тут не косметика. `pane` идёт ключом в runner._runs.
"""

import asyncio
import os

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
