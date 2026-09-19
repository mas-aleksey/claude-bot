"""Лимиты подписки: разбор ответа недокументированного эндпоинта и строка /status.

Ответ ниже — настоящий, снят с api.anthropic.com/api/oauth/usage 2026-09-15. Смена его
формы не должна ронять статус, поэтому проверяем и мусор.
"""

import json
import os
import time

import pytest

os.environ.setdefault("TG_BOT_TOKEN", "x")  # app читает env на импорте

import app
import runner
import sessions
import store
import transcript

USAGE = {
    "five_hour": {"utilization": 0.0},
    "limits": [
        {"kind": "session", "group": "session", "percent": 0, "severity": "normal",
         "resets_at": "2026-09-15T15:40:00.238450+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 26, "severity": "normal",
         "resets_at": "2026-09-16T17:00:00.238475+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "severity": "normal",
         "resets_at": "2026-09-16T17:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}}, "is_active": False},
    ],
}
PROFILE = {
    "account": {"email": "someone@example.com"},
    "organization": {"rate_limit_tier": "default_claude_max_5x"},
}


def test_bars_from_real_response():
    bars = runner._bars(USAGE)
    assert [(b["name"], b["percent"]) for b in bars] == [
        ("сессия", 0), ("неделя", 26), ("неделя, Fable", 0)]
    assert bars[1]["resets"] == "2026-09-16T17:00:00.238475+00:00"


def test_bars_survive_unknown_shape():
    # Ни одного процента — панель гасит полоски, а не рисует пустые.
    assert runner._bars({}) == []
    assert runner._bars({"limits": [{"kind": "session"}, {"percent": None}]}) == []
    # Незнакомый лимит показываем его же ключом, а не прячем.
    assert runner._bars({"limits": [{"kind": "monthly_x", "percent": 7}]})[0]["name"] == "monthly_x"


def test_plan_short():
    assert runner._plan(PROFILE) == "max 5x"
    assert runner._plan({}) == ""


def test_status_lines():
    lim = {"email": "someone@example.com", "plan": "max 5x", "bars": runner._bars(USAGE)}
    lines = app._plan_lines(lim)
    assert lines.startswith("подписка: someone@example.com · max 5x\n")
    assert "неделя 26%" in lines
    assert "неделя, Fable 0%" in lines


def test_status_lines_without_data():
    assert app._plan_lines({}) == "подписка: —\nлимиты: —"


@pytest.mark.parametrize("iso", ["", None, "не дата", "2020-01-01T00:00:00+00:00"])
def test_until_quiet_on_junk(iso):
    assert app._until(iso) == ""


async def test_limits_cached(monkeypatch):
    """Кеш обязателен: статус панель дёргает раз в три секунды, API — не чаще минуты."""
    calls = 0

    def creds(*a, **kw):
        nonlocal calls
        calls += 1
        raise FileNotFoundError("нет токена")

    monkeypatch.setattr(runner, "_limits", (float("-inf"), {}))
    # Пустой кеш — это холодный старт, и он лезет в базу за прошлым ответом. База тут
    # настоящая, с настоящими процентами живого бота: без заглушки тест сверял бы их.
    monkeypatch.setattr(runner, "_remembered", lambda key, default: default)
    monkeypatch.setattr("builtins.open", creds)
    assert await runner.limits() == {}
    assert await runner.limits() == {}
    assert calls == 1


async def test_limits_keep_last_good_answer(monkeypatch):
    """Промах запроса не должен гасить полоски: проценты известны, а насколько они
    стары — видно по отметке времени в самих числах. Ошибку ловим на нечитаемом CREDS,
    сети в тестах нет и не надо."""
    monkeypatch.setattr(runner, "CREDS", "/несуществующий/файл")
    known = {"email": "", "plan": "max 5x", "at": time.time() - 86400,
             "bars": [{"name": "сессия", "percent": 12}]}

    monkeypatch.setattr(runner, "_limits", (float("-inf"), known))
    monkeypatch.setattr(runner, "_remembered", lambda key, default: default)   # база настоящая, см. выше
    # Даже суточной давности: панель подпишет возраст, а пустота не сообщает ничего.
    assert await runner.limits() == known


async def test_limits_retry_sooner_when_there_is_nothing_to_show(monkeypatch):
    """Промах при пустом кеше должен повториться быстро: первый запрос после рестарта
    попадает в rate_limit, и с общим TTL панель осталась бы без полосок пять минут."""
    monkeypatch.setattr(runner, "CREDS", "/несуществующий/файл")
    calls = []
    real_open = open

    def counting_open(path, *a, **kw):
        calls.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", counting_open)
    # Кеш пуст и промах случился LIMITS_RETRY назад — пора пробовать снова.
    monkeypatch.setattr(runner, "_limits_wait", runner.LIMITS_RETRY)
    monkeypatch.setattr(runner, "_limits", (time.monotonic() - runner.LIMITS_RETRY - 1, {}))
    assert await runner.limits() == {}
    assert calls, "запрос не повторился"

    # Удачный ответ той же давности ещё живёт: TTL у него в десять раз длиннее.
    calls.clear()
    known = {"email": "", "plan": "max 5x", "bars": [{"name": "сессия", "percent": 1}]}
    monkeypatch.setattr(runner, "_limits", (time.monotonic() - runner.LIMITS_RETRY - 1, known))
    assert await runner.limits() == known
    assert not calls, "сходили в сеть, хотя кеш свежий"


async def test_expired_token_never_reaches_the_api(tmp_path, monkeypatch):
    """Протухший токен обновляет CLI на ближайшем прогоне, а запрос с ним не просто
    бесполезен: на surf такие запросы раз в полминуты утянули аккаунт в лимит самого
    эндпоинта, и полоски не появлялись часами."""
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": (time.time() - 60) * 1000}}), encoding="utf-8")
    monkeypatch.setattr(runner, "CREDS", str(creds))

    def no_network(*a, **kw):
        raise AssertionError("пошли в сеть с протухшим токеном")

    monkeypatch.setattr(runner.aiohttp, "ClientSession", no_network)
    monkeypatch.setattr(runner, "_limits", (float("-inf"), {}))
    monkeypatch.setattr(runner, "_remembered", lambda key, default: default)
    assert await runner.limits() == {}


async def test_failures_back_off(tmp_path, monkeypatch):
    """Пауза после неудачи удваивается: сбой бывает общим на аккаунт, и три инстанса,
    долбящие раз в полминуты, сами держат эндпоинт в отказе."""
    monkeypatch.setattr(runner, "CREDS", "/несуществующий/файл")
    monkeypatch.setattr(runner, "_limits_wait", runner.LIMITS_RETRY)
    for expected in (runner.LIMITS_RETRY * 2, runner.LIMITS_RETRY * 4):
        monkeypatch.setattr(runner, "_limits", (float("-inf"), {}))
        await runner.limits()
        assert runner._limits_wait == expected


async def test_limits_survive_a_restart(tmp_path, monkeypatch):
    """Свежий процесс показывает запомненное сразу, не дожидаясь ответа API: после
    рестарта панель стояла без полосок, а с протухшим токеном — пока в песочнице не
    запустят claude."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store._local, "conn", None, raising=False)
    monkeypatch.setattr(runner, "CREDS", "/несуществующий/файл")
    known = {"email": "", "plan": "max 5x", "at": time.time() - 60,
             "bars": [{"name": "сессия", "percent": 7}]}
    store.put(runner.LIMITS_KEY, json.dumps(known))

    monkeypatch.setattr(runner, "_limits", (float("-inf"), {}))
    assert await runner.limits() == known

    # Мусор в базе не должен ронять статус — просто нечего вспоминать.
    store.put(runner.LIMITS_KEY, "не json")
    monkeypatch.setattr(runner, "_limits", (float("-inf"), {}))
    assert await runner.limits() == {}


async def test_model_catalog_survives_a_failed_fetch(tmp_path, monkeypatch):
    """Каталог тоже помним: один промах на старте — а он случается ровно при протухшем
    токене — оставлял выпадашку с запасной тройкой на шесть часов."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store._local, "conn", None, raising=False)
    monkeypatch.setattr(runner, "CREDS", "/несуществующий/файл")
    catalog = [{"id": "claude-opus-5", "name": "Opus 5"}]
    store.put(runner.MODELS_KEY, json.dumps(catalog))

    monkeypatch.setattr(runner, "_models", (float("-inf"), []))
    assert await runner.models() == catalog        # запомненное отдаётся сразу
    assert await runner.models() == catalog        # промах не стёр его

    # Пустая база и неудачный запрос — честно пусто, панель покажет запасную тройку.
    store.put(runner.MODELS_KEY, None)
    monkeypatch.setattr(runner, "_models", (float("-inf"), []))
    assert await runner.models() == []


async def test_context_line(tmp_path, monkeypatch):
    """Строка контекста в `/status`. Регрессия: вызов ушёл в несуществующее имя после
    переименования в webui, и `/status` при живой сессии падал AttributeError."""

    monkeypatch.setattr(sessions, "TRANSCRIPTS", tmp_path)
    monkeypatch.setattr(store, "get", lambda key, default=None: None)
    path = transcript.path_of("/projects/x", "0123abcd-0000-0000-0000-000000000000")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "assistant", "message": {
        "model": "claude-opus-5", "content": [{"type": "text", "text": "x"}],
        "usage": {"input_tokens": 1_000, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 11_300, "output_tokens": 0}}}) + "\n",
        encoding="utf-8")

    assert await app._context_line("/projects/x", path.stem) == "12k/200k? (6%)"
    assert await app._context_line("/projects/x", None) == "—"
    # Сессия есть, а транскрипта ещё нет — обычное состояние сразу после /new.
    assert await app._context_line("/projects/x", "ffffffff-0000-0000-0000-000000000000") == "—"


async def test_api_get_shares_one_session(tmp_path, monkeypatch):
    """Два адреса лимитов ходят одной сессией и одними заголовками — их собирают в
    одном месте, чтобы не разъехаться в день, когда сменится `anthropic-beta`."""
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "тк", "expiresAt": (time.time() + 600) * 1000}}), encoding="utf-8")
    monkeypatch.setattr(runner, "CREDS", str(creds))

    seen = {"sessions": 0, "urls": [], "headers": None}

    class FakeReply:
        def __init__(self, url):
            self.url = url
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def json(self):
            return {"откуда": self.url}

    class FakeSession:
        def __init__(self, headers=None, timeout=None):
            seen["sessions"] += 1
            seen["headers"] = headers
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def get(self, url):
            seen["urls"].append(url)
            return FakeReply(url)

    monkeypatch.setattr(runner.aiohttp, "ClientSession", FakeSession)
    got = await runner._api_get("https://a/one", "https://a/two",
                                extra={"anthropic-version": "2023-06-01"})

    assert got == [{"откуда": "https://a/one"}, {"откуда": "https://a/two"}]
    assert seen["sessions"] == 1                       # одна сессия на оба адреса
    assert seen["urls"] == ["https://a/one", "https://a/two"]
    assert seen["headers"]["Authorization"] == "Bearer тк"
    assert seen["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"


def test_until_counts_down_in_the_same_units_as_session_age():
    """Единицы до сброса считает `sessions.ago` — тот же формат, что у возраста сессии.
    Раньше это был свой каскад if-ов, и он же округлял полминуты в «0м».

    Секунда сверху в каждом сроке — обе реализации отбрасывают дробную часть, а
    `now()` внутри `_until` вызывается чуть позже здешнего."""
    from datetime import UTC, datetime, timedelta

    def left(**kw):
        when = datetime.now(UTC) + timedelta(**kw) + timedelta(seconds=1)
        return app._until(when.isoformat())

    assert left(hours=2, minutes=5) == " (↻2ч)"
    assert left(minutes=7) == " (↻7м)"
    assert left(days=3) == " (↻3д)"
    assert left(seconds=29) == " (↻29с)"   # прежний каскад показывал тут «0м»
