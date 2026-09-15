"""Лимиты подписки: разбор ответа недокументированного эндпоинта и строка /status.

Ответ ниже — настоящий, снят с api.anthropic.com/api/oauth/usage 2026-09-15. Смена его
формы не должна ронять статус, поэтому проверяем и мусор.
"""

import os
import time

import pytest

os.environ.setdefault("TG_BOT_TOKEN", "x")  # app читает env на импорте

import app
import runner

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
    monkeypatch.setattr("builtins.open", creds)
    assert await runner.limits() == {}
    assert await runner.limits() == {}
    assert calls == 1


async def test_limits_keep_last_good_answer(monkeypatch):
    """Промах запроса не должен гасить полоски: проценты известны и за минуту не
    устареют. Ошибку ловим на нечитаемом CREDS — сети в тестах нет и не надо."""
    monkeypatch.setattr(runner, "CREDS", "/несуществующий/файл")
    known = {"email": "", "plan": "max 5x", "bars": [{"name": "сессия", "percent": 12}]}

    monkeypatch.setattr(runner, "_limits", (float("-inf"), known))
    monkeypatch.setattr(runner, "_limits_ok", time.monotonic())
    assert await runner.limits() == known

    # Полчаса без единого удачного ответа — скорее всего бот разлогинен, и старым
    # числам веры нет.
    monkeypatch.setattr(runner, "_limits", (float("-inf"), known))
    monkeypatch.setattr(runner, "_limits_ok", time.monotonic() - runner.LIMITS_STALE - 1)
    assert await runner.limits() == {}


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
    monkeypatch.setattr(runner, "_limits", (time.monotonic() - runner.LIMITS_RETRY - 1, {}))
    monkeypatch.setattr(runner, "_limits_ok", float("-inf"))
    assert await runner.limits() == {}
    assert calls, "запрос не повторился"

    # Удачный ответ той же давности ещё живёт: TTL у него в десять раз длиннее.
    calls.clear()
    known = {"email": "", "plan": "max 5x", "bars": [{"name": "сессия", "percent": 1}]}
    monkeypatch.setattr(runner, "_limits", (time.monotonic() - runner.LIMITS_RETRY - 1, known))
    assert await runner.limits() == known
    assert not calls, "сходили в сеть, хотя кеш свежий"
