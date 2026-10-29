"""Лимиты подписки: разбор ответа недокументированного эндпоинта и строка /status.

Ответ ниже — настоящий, снят с api.anthropic.com/api/oauth/usage 2026-09-15. Смена его
формы не должна ронять статус, поэтому проверяем и мусор.
"""

import json
import os
import time
from datetime import UTC, datetime

import pytest

os.environ.setdefault("TG_BOT_TOKEN", "x")  # app читает env на импорте

import account
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
    bars = account._bars(USAGE)
    assert [(b["name"], b["percent"]) for b in bars] == [
        ("сессия", 0), ("неделя", 26), ("неделя, Fable", 0)]
    assert bars[1]["resets"] == "2026-09-16T17:00:00.238475+00:00"


def test_bars_survive_unknown_shape():
    # Ни одного процента — панель гасит полоски, а не рисует пустые.
    assert account._bars({}) == []
    assert account._bars({"limits": [{"kind": "session"}, {"percent": None}]}) == []
    # Незнакомый лимит показываем его же ключом, а не прячем.
    assert account._bars({"limits": [{"kind": "monthly_x", "percent": 7}]})[0]["name"] == "monthly_x"


def test_plan_short():
    assert account._plan(PROFILE) == "max 5x"
    assert account._plan({}) == ""


def test_status_lines():
    lim = {"email": "someone@example.com", "plan": "max 5x", "bars": account._bars(USAGE)}
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

    monkeypatch.setattr(account, "_limits", (float("-inf"), {}))
    # Пустой кеш — это холодный старт, и он лезет в базу за прошлым ответом. База тут
    # настоящая, с настоящими процентами живого бота: без заглушки тест сверял бы их.
    monkeypatch.setattr(account, "_remembered", lambda key, default: default)
    monkeypatch.setattr("builtins.open", creds)
    assert await account.limits() == {}
    assert await account.limits() == {}
    assert calls == 1


async def test_limits_keep_last_good_answer(monkeypatch):
    """Промах запроса не должен гасить полоски: проценты известны, а насколько они
    стары — видно по отметке времени в самих числах. Ошибку ловим на нечитаемом CREDS,
    сети в тестах нет и не надо."""
    monkeypatch.setattr(account, "CREDS", "/несуществующий/файл")
    known = {"email": "", "plan": "max 5x", "at": time.time() - 86400,
             "bars": [{"name": "сессия", "percent": 12}]}

    monkeypatch.setattr(account, "_limits", (float("-inf"), known))
    monkeypatch.setattr(account, "_remembered", lambda key, default: default)   # база настоящая, см. выше
    # Даже суточной давности: панель подпишет возраст, а пустота не сообщает ничего.
    assert await account.limits() == known


async def test_limits_retry_sooner_when_there_is_nothing_to_show(monkeypatch):
    """Промах при пустом кеше должен повториться быстро: первый запрос после рестарта
    попадает в rate_limit, и с общим TTL панель осталась бы без полосок пять минут."""
    monkeypatch.setattr(account, "CREDS", "/несуществующий/файл")
    calls = []
    real_open = open

    def counting_open(path, *a, **kw):
        calls.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", counting_open)
    # Кеш пуст и промах случился LIMITS_RETRY назад — пора пробовать снова.
    monkeypatch.setattr(account, "_limits_wait", account.LIMITS_RETRY)
    monkeypatch.setattr(account, "_limits", (time.monotonic() - account.LIMITS_RETRY - 1, {}))
    assert await account.limits() == {}
    assert calls, "запрос не повторился"

    # Удачный ответ той же давности ещё живёт: TTL у него в десять раз длиннее.
    calls.clear()
    known = {"email": "", "plan": "max 5x", "bars": [{"name": "сессия", "percent": 1}]}
    monkeypatch.setattr(account, "_limits", (time.monotonic() - account.LIMITS_RETRY - 1, known))
    assert await account.limits() == known
    assert not calls, "сходили в сеть, хотя кеш свежий"


async def test_expired_token_never_reaches_the_api(tmp_path, monkeypatch):
    """Протухший токен обновляет CLI на ближайшем прогоне, а запрос с ним не просто
    бесполезен: на surf такие запросы раз в полминуты утянули аккаунт в лимит самого
    эндпоинта, и полоски не появлялись часами."""
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "t", "expiresAt": (time.time() - 60) * 1000}}), encoding="utf-8")
    monkeypatch.setattr(account, "CREDS", str(creds))

    def no_network(*a, **kw):
        raise AssertionError("пошли в сеть с протухшим токеном")

    monkeypatch.setattr(account.aiohttp, "ClientSession", no_network)
    monkeypatch.setattr(account, "_limits", (float("-inf"), {}))
    monkeypatch.setattr(account, "_remembered", lambda key, default: default)
    assert await account.limits() == {}


async def test_failures_back_off(tmp_path, monkeypatch):
    """Пауза после неудачи удваивается: сбой бывает общим на аккаунт, и три инстанса,
    долбящие раз в полминуты, сами держат эндпоинт в отказе."""
    monkeypatch.setattr(account, "CREDS", "/несуществующий/файл")
    monkeypatch.setattr(account, "_limits_wait", account.LIMITS_RETRY)
    for expected in (account.LIMITS_RETRY * 2, account.LIMITS_RETRY * 4):
        monkeypatch.setattr(account, "_limits", (float("-inf"), {}))
        await account.limits()
        assert account._limits_wait == expected


async def test_limits_survive_a_restart(tmp_path, monkeypatch):
    """Свежий процесс показывает запомненное сразу, не дожидаясь ответа API: после
    рестарта панель стояла без полосок, а с протухшим токеном — пока в песочнице не
    запустят claude."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store._local, "conn", None, raising=False)
    monkeypatch.setattr(account, "CREDS", "/несуществующий/файл")
    known = {"email": "", "plan": "max 5x", "at": time.time() - 60,
             "bars": [{"name": "сессия", "percent": 7}]}
    store.put(account.LIMITS_KEY, json.dumps(known))

    monkeypatch.setattr(account, "_limits", (float("-inf"), {}))
    assert await account.limits() == known

    # Мусор в базе не должен ронять статус — просто нечего вспоминать.
    store.put(account.LIMITS_KEY, "не json")
    monkeypatch.setattr(account, "_limits", (float("-inf"), {}))
    assert await account.limits() == {}


async def test_model_catalog_survives_a_failed_fetch(tmp_path, monkeypatch):
    """Каталог тоже помним: один промах на старте — а он случается ровно при протухшем
    токене — оставлял выпадашку с запасной тройкой на шесть часов."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store._local, "conn", None, raising=False)
    monkeypatch.setattr(account, "CREDS", "/несуществующий/файл")
    catalog = [{"id": "claude-opus-5", "name": "Opus 5"}]
    store.put(account.MODELS_KEY, json.dumps(catalog))

    monkeypatch.setattr(account, "_models", (float("-inf"), []))
    assert await account.models() == catalog        # запомненное отдаётся сразу
    assert await account.models() == catalog        # промах не стёр его

    # Пустая база и неудачный запрос — честно пусто, панель покажет запасную тройку.
    store.put(account.MODELS_KEY, None)
    monkeypatch.setattr(account, "_models", (float("-inf"), []))
    assert await account.models() == []


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
    monkeypatch.setattr(account, "CREDS", str(creds))

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

    monkeypatch.setattr(account.aiohttp, "ClientSession", FakeSession)
    got = await account._api_get("https://a/one", "https://a/two",
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


def test_reset_passed_spots_stale_numbers():
    """Прошедший срок сброса — доказательство, что ответ описывает мир до сброса.

    Именно в этот момент на полоски и смотрят: лимит исчерпан, проценты стоят на сотне.
    Раньше такой ответ жил до конца TTL, а панель говорила «время сброса неизвестно» —
    хотя срок был известен и как раз истёк.
    """
    future = datetime.fromtimestamp(time.time() + 3600, UTC).isoformat()
    past = datetime.fromtimestamp(time.time() - 3600, UTC).isoformat()

    assert account._reset_passed({"bars": [{"resets": future}]}) is False
    assert account._reset_passed({"bars": [{"resets": future}, {"resets": past}]}) is True
    # Запас в минуту: часы сервера и наши расходятся, и секундная разница не повод
    # ходить в API на каждый вызов.
    just = datetime.fromtimestamp(time.time() - 5, UTC).isoformat()
    assert account._reset_passed({"bars": [{"resets": just}]}) is False
    # Мусор и пустота — не повод перезапрашивать: это другой случай, и он не наш.
    assert account._reset_passed({}) is False
    assert account._reset_passed({"bars": [{"resets": ""}, {}, {"resets": "не дата"}]}) is False


async def test_limits_refetch_when_the_reset_is_behind(monkeypatch):
    """Свежий по времени кеш всё равно перезапрашивается, если срок сброса уже позади."""
    calls = 0

    def creds(*a, **kw):
        nonlocal calls
        calls += 1
        raise FileNotFoundError("нет токена")

    monkeypatch.setattr(account, "_remembered", lambda key, default: default)
    monkeypatch.setattr("builtins.open", creds)
    stale = {"bars": [{"resets": datetime.fromtimestamp(time.time() - 3600, UTC).isoformat()}]}
    monkeypatch.setattr(account, "_limits", (time.monotonic(), stale))

    await account.limits()
    assert calls == 1, "прошедший сброс обязан пробить кеш"


async def test_limits_ttl_follows_the_load(monkeypatch):
    """Под прогоном проценты движутся, в простое стоят — и срок кеша идёт за этим."""
    calls = 0

    def creds(*a, **kw):
        nonlocal calls
        calls += 1
        raise FileNotFoundError("нет токена")

    monkeypatch.setattr(account, "_remembered", lambda key, default: default)
    monkeypatch.setattr("builtins.open", creds)
    fresh = {"bars": [{"resets": datetime.fromtimestamp(time.time() + 3600, UTC).isoformat()}]}

    # Возраст между двумя порогами: в простое ещё свеж, под прогоном уже протух.
    age = time.monotonic() - (account.LIMITS_TTL_BUSY + account.LIMITS_JITTER + 1)
    monkeypatch.setattr(account, "_limits", (age, fresh))
    await account.limits(busy=False)
    assert calls == 0, "в простое кеш живёт пять минут"

    monkeypatch.setattr(account, "_limits", (age, fresh))
    await account.limits(busy=True)
    assert calls == 1, "под прогоном тот же кеш уже протух"


def test_suggestion_lives_in_the_base_and_dies_with_the_next_prompt(tmp_path, monkeypatch):
    """Подсказку следующего промпта присылает CLI событием `prompt_suggestion`.

    Держим её в базе по сессии, а не в памяти процесса: панель читает её статусом, и так
    она переживает и перезагрузку страницы, и рестарт бота — а тот случается по десять
    раз на дню. По сессии, а не по скоупу окна: окно закрыли и открыли ту же сессию — у
    него новый скоуп, а подсказка должна остаться. Снимается она при новом прогоне той же
    сессии: подсказка прошлого хода больше не про то, что сейчас происходит.
    """
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(store._local, "conn", None, raising=False)

    runner.suggest("s-1", "раскатай на песочницы")
    runner.suggest("s-2", "запушь")
    assert runner.suggestions() == {"s-1": "раскатай на песочницы", "s-2": "запушь"}

    # Новый прогон сессии стирает её ключ, соседнюю не трогает.
    runner.suggest("s-1", None)
    assert runner.suggestions() == {"s-2": "запушь"}

    # Пустую строку CLI отдать может, показывать её нечего — ключа быть не должно.
    runner.suggest("s-2", None)
    assert runner.suggestions() == {}

    # Протухшая подсказка не показывается и стирается на том же проходе. Иначе ключи
    # копились бы по одному на каждое когда-либо открытое окно: закрытие окна случается
    # в браузере, и сказать об этом боту некому.
    runner.suggest("s-3", "давно забытое")
    old_at = json.loads(store.get("suggest:s-3"))
    old_at["at"] -= runner.SUGGEST_TTL + 1
    store.put("suggest:s-3", json.dumps(old_at))

    assert runner.suggestions() == {}
    assert store.get("suggest:s-3") is None, "протухший ключ обязан исчезнуть"


def test_key_from_panel_beats_env(monkeypatch):
    """Как у самого claude: переменная из settings.json перебивает такую же из `.env`.
    Запись — все три переменные, даже пустые: иначе заголовок из `.env` уехал бы в чужой
    прокси. Остальные настройки файла не трогаем."""
    assert account.key() == {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "из-env")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-teamclaude-usage: me|claude")
    k = account.key()
    assert (k["source"], k["host"], k["headers"]) == (
        "env", "api.anthropic.com", {"x-teamclaude-usage": "me|claude"})

    with open(account.SETTINGS, "w") as f:
        json.dump({"model": "opus", "env": {"FOO": "1"}}, f)
    account.save_key("http://claude-pool:8080/", "из-панели", "")
    k = account.key()
    assert (k["source"], k["key"], k["url"], k["headers"]) == (
        "panel", "из-панели", "http://claude-pool:8080", {})
    assert oct(os.stat(account.SETTINGS).st_mode & 0o777) == "0o600"

    account.save_key("", "", "")
    with open(account.SETTINGS) as f:
        assert json.load(f) == {"model": "opus", "env": {"FOO": "1"}}
    assert account.key()["source"] == "env"   # из панели снят, `.env` остался


def test_stats_bars_pick_own_account():
    stats = {"client": "client-b", "accounts": [
        {"name": "main", "clients": ["assistant"],
         "windows": {"5h": {"used": 0.9, "resets": "r1"}}},
        {"name": "reserve", "clients": ["client-b"],
         "windows": {"7d": {"used": 0.3, "resets": "r2"}, "5h": {"used": 0.126, "resets": "r3"}}},
    ]}
    name, bars = account._stats_bars(stats)
    assert name == "reserve"
    assert [(b["name"], b["percent"], b["resets"]) for b in bars] == [
        ("сессия", 13, "r3"), ("неделя", 30, "r2")]
    # Свой аккаунт не нашёлся — худшее окно по всем, а не самое свободное.
    stats["client"] = "новый"
    assert account._stats_bars(stats)[1][0]["percent"] == 90
    # Чужой формат — без полосок, без исключений.
    assert account._stats_bars({"type": "error"}) == ("", [])
    assert account._stats_bars({"accounts": [{"windows": {"5h": {"used": "x"}}}]}) == ("", [])


async def test_limits_by_key_ask_the_proxy_and_forget_the_subscription(monkeypatch):
    """Ключ API: полоски — из `/stats` прокси с ключом в заголовке. Запомненные цифры
    подписки чужие и не показываются; без `/stats` блок пуст, это не сбой."""
    store.put(account.LIMITS_KEY, json.dumps({"email": "старый@акк", "bars": [{"percent": 50}]}))
    monkeypatch.setattr(account, "_limits", (float("-inf"), {}))
    monkeypatch.setattr(account, "_limits_mode", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "к")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://pool:8080")
    seen = {}

    async def fake_get(*urls, extra=None, k=None):
        seen["urls"], seen["k"] = urls, k
        return [{"client": "a", "accounts": [
            {"name": "main", "clients": ["a"], "windows": {"5h": {"used": 0.5, "resets": ""}}}]}]

    monkeypatch.setattr(account, "_api_get", fake_get)
    lim = await account.limits()
    assert seen["urls"] == ("http://pool:8080/stats",) and seen["k"]["key"] == "к"
    assert (lim["email"], lim["plan"], lim["bars"][0]["percent"]) == ("ключ API · pool", "main", 50)
    assert app._plan_lines(lim).startswith("вход: ключ API · pool · main\n")

    async def no_stats(*a, **kw):
        raise ValueError("404")

    monkeypatch.setattr(account, "_api_get", no_stats)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ai-hub.example")   # другой вход — кеш сброшен
    assert await account.limits() == {}
    assert app._plan_lines({}) == "вход: ключ API · ai-hub.example\nлимиты: —"
    assert account.auth() == {"ok": True, "fresh": True, "renewable": True, "kind": "key",
                              "host": "ai-hub.example", "source": "env"}


def test_stats_accounts_show_every_account_and_who_is_blocked():
    """Все аккаунты пула, свой отмечен: по одной своей полоске не понять, есть ли куда
    переключиться (2026-10-29 оба упёрлись в 5h, а видно было только своё)."""
    stats = {"client": "assistant", "accounts": [
        {"name": "main", "clients": [], "blocked_until": "2026-10-29T17:20:00+00:00",
         "blocked_reason": "limit", "windows": {"5h": {"used": 1.03, "resets": "r1"}}},
        {"name": "reserve", "clients": ["assistant"], "blocked_until": None,
         "windows": {"7d": {"used": 0.43, "resets": "r2"}}},
    ]}
    accs = account._stats_accounts(stats)
    assert [(a["name"], a["current"], a["reason"]) for a in accs] == [
        ("main", False, "limit"), ("reserve", True, "")]
    assert accs[0]["bars"][0]["percent"] == 103 and accs[1]["bars"][0]["name"] == "неделя"
    assert account._stats_accounts({"type": "error"}) == []


async def test_limits_by_key_carry_the_exhausted_line(monkeypatch):
    """Пул сказал «все исчерпаны» — фраза и срок доходят до панели и /status как есть."""
    monkeypatch.setattr(account, "key", lambda: {"url": "http://pool", "key": "k",
                                                 "headers": {}, "host": "pool"})
    stats = {"client": "a", "exhausted_until": "2026-10-29T17:20:00+00:00",
             "exhausted_text": "Все подписки пула исчерпаны: x — лимит до 29.10 17:20",
             "accounts": [{"name": "x", "clients": ["a"], "blocked_until": "t",
                           "windows": {"5h": {"used": 1.0, "resets": "r"}}},
                          {"name": "y", "clients": [], "blocked_until": "t", "windows": {}}]}

    async def get(*urls, **kw):
        return [stats]
    monkeypatch.setattr(account, "_api_get", get)
    monkeypatch.setattr(account, "_limits", (float("-inf"), {}))
    monkeypatch.setattr(account, "_limits_mode", None)
    out = await account.limits()
    assert out["exhausted"].startswith("Все подписки") and out["exhausted_until"]
    assert [a["name"] for a in out["accounts"]] == ["x", "y"]
    assert "⚠️ Все подписки" in app._plan_lines(out) and "x (сейчас): сессия 100%" in \
        app._plan_lines(out)
