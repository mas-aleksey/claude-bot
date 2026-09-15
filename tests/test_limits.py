"""Лимиты подписки: разбор ответа недокументированного эндпоинта и строка /status.

Ответ ниже — настоящий, снят с api.anthropic.com/api/oauth/usage 2026-09-15. Смена его
формы не должна ронять статус, поэтому проверяем и мусор.
"""

import os

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
    "account": {"email": "cc-ba-08@surf.dev"},
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
    lim = {"email": "cc-ba-08@surf.dev", "plan": "max 5x", "bars": runner._bars(USAGE)}
    lines = app._plan_lines(lim)
    assert lines.startswith("подписка: cc-ba-08@surf.dev · max 5x\n")
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
