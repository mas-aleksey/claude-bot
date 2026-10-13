"""Аккаунт подписки: ключ, лимиты и каталог моделей.

Вынесено из `runner`: к запуску claude это не относится — тут только HTTP к API аккаунта
токеном из CREDS. Занятость бота, от которой зависит срок кеша лимитов, приходит
параметром: импорт `runner` отсюда замкнул бы модули в кольцо.
"""

import json
import logging
import random
import time
from datetime import datetime

import aiohttp

import store

log = logging.getLogger("claude_bot.account")

CREDS = "/root/.claude/.credentials.json"

# Лимиты подписки. В headless их не спросить — `/usage` и `/cost` отвечают только внутри
# сессии и текстом, — зато CLI берёт их с этого эндпоинта, и токен для него уже лежит
# в CREDS. Ходим туда же сами.
OAUTH_API = "https://api.anthropic.com/api/oauth"
# Пока идёт прогон, проценты растут на глазах и полминуты — предел, за которым цифра
# врёт заметно. В простое им меняться не с чего: единственное событие — сам сброс, а его
# ловит проверка прошедшей даты ниже, не дожидаясь конца паузы.
LIMITS_TTL_BUSY = 30
LIMITS_TTL_IDLE = 300
# Разброс разводит инстансы по времени. Считается один раз на процесс, а не на каждую
# проверку: поднятые одной командой, они иначе ходят в API в одну и ту же секунду, и
# именно так эндпоинт и отвечал `rate_limit_error`. Свежая случайность на каждой проверке
# толку не дала бы — опрос идёт раз в три секунды, и первым сработал бы малый выпавший
# разброс, то есть все снова сошлись бы к нижней границе.
LIMITS_JITTER = random.randint(0, 30)
# Промах живёт куда меньше удачи. Первый же запрос после рестарта попадает в тот самый
# `rate_limit_error`, показать нечего — и при общем TTL панель осталась бы без полосок на
# все пять минут. Проверено на surf 15.09: старт 14:24, отказ 14:25, полоски до 14:30.
LIMITS_RETRY = 30
_limits_wait = LIMITS_RETRY   # пауза после неудачи: удваивается, пока не упрётся в TTL
# Ключ в `store`: последний удачный ответ переживает рестарт. Без него свежий процесс
# минуту стоял без полосок, а с протухшим токеном — пока в песочнице не запустят claude.
LIMITS_KEY = "limits"


def _remembered(key: str, default):
    """Последний удачный ответ из базы. Умолчание, если его там нет или он покорёжен."""
    try:
        return json.loads(store.get(key) or "")
    except ValueError:
        return default


async def _api_get(*urls: str, extra: dict | None = None) -> list:
    """GET по адресам аккаунта токеном подписки. Несколько адресов — одной сессией.

    Заголовки и таймаут одни на всех: и лимиты, и каталог моделей ходят на тот же
    хост тем же OAuth-токеном, и собирать их по второму разу значило разъехаться в
    день, когда `anthropic-beta` сменится.
    """
    headers = {"Authorization": f"Bearer {_oauth_token()}",
               "anthropic-beta": "oauth-2025-04-20", **(extra or {})}
    out = []
    async with aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as s:
        for url in urls:
            async with s.get(url) as r:
                out.append(await r.json())
    return out


def auth() -> dict:
    """Состояние ключа подписки для панели. Три разных случая, а не два.

    В файле два срока: `expiresAt` у доступа (около восьми часов) и
    `refreshTokenExpiresAt` у обновления (около недели). CLI меняет доступ сам по
    refresh-токену на ближайшем запуске claude — пока жив второй. Когда истекает и он,
    прогон отвечает `OAuth session expired and could not be refreshed`, и помогает
    только новый вход.

    Смотреть на один `expiresAt`, как было до 2026-09-29, значит обещать «обновится на
    ближайшем прогоне» и в том случае, когда обновлять уже нечем.

    `refreshTokenExpiresAt` обновление **не двигает** — проверено прогоном 2026-10-02:
    доступ уехал вперёд на сутки, срок обновления сдвинулся на полсекунды. То есть он
    отсчитывается от входа, а не от последней активности, и активной сессии он истекает
    так же, как забытой. Поэтому панель и предупреждает заранее — за сутки до конца.

    Читаем без исключений: панель спрашивает это каждым тиком, и «нет ключа» для неё —
    обычный ответ, а не сбой.
    """
    try:
        oauth = _oauth()
    except (OSError, ValueError, KeyError, TypeError):
        return {"ok": False, "fresh": False, "renewable": False}
    now = time.time()
    return {"ok": True,
            "fresh": oauth.get("expiresAt", 0) / 1000 > now,
            "renewable": oauth.get("refreshTokenExpiresAt", 0) / 1000 > now,
            "until": oauth.get("refreshTokenExpiresAt", 0) / 1000 or None}


def _oauth() -> dict:
    """Блок подписки из CREDS как есть — его читают и `auth`, и `_oauth_token`."""
    with open(CREDS, encoding="utf-8") as f:
        return json.load(f)["claudeAiOauth"]


def _oauth_token() -> str:
    """Токен подписки из CREDS. Протухший не отдаём вовсе.

    Обновляет его CLI на ближайшем прогоне, а идти с ним в API не просто бесполезно:
    на surf такие запросы раз в полминуты сначала получали `authentication_error`, а
    потом утянули аккаунт в `rate_limit_error` самого эндпоинта.
    """
    oauth = _oauth()
    if oauth.get("expiresAt", 0) / 1000 < time.time():
        raise RuntimeError("токен протух, обновится на ближайшем прогоне claude")
    return oauth["accessToken"]
# Последний удачный ответ держим сколько угодно: он уезжает в базу вместе с отметкой
# времени, а панель подписывает его возрастом. Раньше тут стоял порог в полчаса, после
# которого полоски гасли, — с видимым возрастом врать уже нечем, а пустота посреди дня
# сообщает человеку ровно ничего.
_limits: tuple[float, dict] = (float("-inf"), {})

# Подписи известных лимитов. Неизвестный показываем его же ключом: спрятать лимит,
# в который упрёшься, хуже, чем показать непонятную подпись.
LIMIT_NAMES = {"session": "сессия", "weekly_all": "неделя", "weekly_scoped": "неделя"}

# Каталог моделей. Кеш длинный, в отличие от минутного у лимитов: список меняется раз в
# месяцы. Своего перечня у CLI нет — ни одна его команда моделей не печатает, поэтому
# зашитая в панель тройка `opus/sonnet/haiku` устаревала молча и `fable` в ней не было.
MODELS_URL = "https://api.anthropic.com/v1/models?limit=50"
MODELS_TTL = 6 * 3600
# Промах живёт минуту, а не шесть часов. Иначе один неудачный запрос на старте — а он
# случается ровно тогда, когда протух токен, — оставлял выпадашку с запасной тройкой
# `opus/sonnet/haiku` до вечера. Проверено на ассистенте 16.09.
MODELS_RETRY = 60
# Каталог тоже помним в базе: он меняется раз в месяцы, и ждать ответа API, чтобы
# показать список моделей, незачем.
MODELS_KEY = "models"
_models: tuple[float, list] = (float("-inf"), [])

def _bars(usage: dict) -> list[dict]:
    """`limits` из ответа API — в то, что рисует панель.

    Список курирует сервер: у разных тарифов он разной длины и с разными `kind`,
    поэтому перебираем что дали, а не ждём знакомых ключей.
    """
    out = []
    for lim in usage.get("limits") or []:
        try:
            percent = round(float(lim["percent"]))
        except (KeyError, TypeError, ValueError):
            continue
        name = LIMIT_NAMES.get(lim.get("kind"), lim.get("kind") or "лимит")
        if model := ((lim.get("scope") or {}).get("model") or {}).get("display_name"):
            name = f"{name}, {model}"
        out.append({"name": name, "percent": percent,
                    "resets": lim.get("resets_at") or "",
                    "severity": lim.get("severity") or "normal"})
    return out


def _reset_passed(out: dict) -> bool:
    """Есть ли полоска, чей срок сброса уже позади.

    Такой ответ описывает мир до сброса, а мир уже после: проценты в нём завышены, а
    чаще всего стоят на сотне — именно в этот момент на них и смотрят. Сравниваем с
    запасом в минуту: часы сервера и наши расходятся, и секундная разница не повод
    ходить в API на каждый вызов.
    """
    for bar in out.get("bars") or []:
        try:
            gone = datetime.fromisoformat(bar["resets"]).timestamp() < time.time() - 60
        except (KeyError, TypeError, ValueError):
            continue
        if gone:
            return True
    return False


def _plan(profile: dict) -> str:
    """Тариф коротко: `default_claude_max_5x` → `max 5x`."""
    tier = (profile.get("organization") or {}).get("rate_limit_tier") or ""
    return tier.removeprefix("default_").removeprefix("claude_").replace("_", " ")


async def limits(busy: bool = False) -> dict:
    """Занятость лимитов подписки и чей это аккаунт, с кешем.

    Пустой словарь значит «показывать нечего»: нет файла с токеном, токен протух или
    ответ не той формы. Эндпоинт недокументированный, и смена его формы не должна
    ронять статус — панель на пустом словаре просто гасит полоски. Протухший токен
    чиним не мы: CLI обновляет CREDS на следующем прогоне, поэтому файл читаем заново
    на каждый промах кеша, а неудачу кешируем наравне с успехом — иначе трёхсекундный
    опрос панели будет долбить API.

    Срок жизни кеша зависит от занятости: под прогоном цифры движутся, в простое стоят.
    Плюс отдельный повод перезапросить — прошедший `resets` хоть у одной полоски. Это
    доказательство, что лимит уже сбросился, а у нас лежит доотказный ответ: без такой
    проверки панель держала 100% ещё пять минут после сброса и говорила «время сброса
    неизвестно», потому что срок истёк.
    """
    global _limits, _limits_wait
    now = time.monotonic()
    ttl = LIMITS_TTL_BUSY if busy else LIMITS_TTL_IDLE
    # Холодный старт: показываем запомненное сразу, а запрос уходит этим же вызовом.
    # Числа с отметкой времени — панель сама решит, насколько они устарели.
    if _limits[0] == float("-inf") and (was := _remembered(LIMITS_KEY, {})):
        _limits = (now - ttl - LIMITS_JITTER, was)
    fresh = now - _limits[0] < (ttl + LIMITS_JITTER if _limits[1] else _limits_wait)
    if fresh and not _reset_passed(_limits[1]):
        return _limits[1]
    out: dict = {}
    try:
        usage, profile = await _api_get(f"{OAUTH_API}/usage", f"{OAUTH_API}/profile")
        if bars := _bars(usage):
            out = {"email": (profile.get("account") or {}).get("email") or "",
                   "plan": _plan(profile), "bars": bars, "at": time.time()}
            store.put(LIMITS_KEY, json.dumps(out))
        else:
            log.warning("лимиты подписки: в ответе нет процентов, %s", str(usage)[:200])
    except Exception as e:
        log.warning("лимиты подписки не прочитались: %s", e)
    if out:
        _limits_wait = LIMITS_RETRY
    else:
        # Пауза растёт: сбой бывает и общим на аккаунт, и тогда три инстанса, долбящие
        # раз в полминуты, сами и держат эндпоинт в отказе.
        _limits_wait = min(_limits_wait * 2, LIMITS_TTL_IDLE)
        out = _limits[1]  # не вышло сейчас — показываем прошлое, а не пустоту
    _limits = (now, out)
    return out


async def models() -> list[dict]:
    """Модели для выпадашки панели: `{"id", "name"}`, свежие сверху.

    Тем же токеном подписки, что читает `limits`: каталог отдаётся по OAuth и
    всегда актуален — новая модель появляется в списке сама, снятая исчезает.

    Пустой список значит «панель покажет запасную тройку». Каталог недоступен — это не
    повод оставить человека без выбора модели вообще.

    Подпись — `display_name` без слова «Claude»: в списке из одиннадцати строк оно стоит
    в каждой и не различает ничего.
    """
    global _models
    now = time.monotonic()
    if _models[0] == float("-inf") and (was := _remembered(MODELS_KEY, [])):
        _models = (now - MODELS_TTL, was)   # показываем запомненное, запрос уйдёт сейчас
    if now - _models[0] < (MODELS_TTL if _models[1] else MODELS_RETRY):
        return _models[1]
    out: list[dict] = []
    try:
        body, = await _api_get(MODELS_URL, extra={"anthropic-version": "2023-06-01"})
        out = [{"id": m["id"], "name": (m.get("display_name") or m["id"]).removeprefix("Claude ")}
               for m in body.get("data") or [] if m.get("id")]
    except Exception as e:
        log.warning("каталог моделей не прочитался: %s", e)
    if out:
        store.put(MODELS_KEY, json.dumps(out))
    else:
        out = _models[1]  # не вышло — остаёмся на прошлом каталоге, а не на пустоте
    _models = (now, out)
    return out


async def resolve_model(value: str) -> dict:
    """Модель бота в том же виде, что и строки каталога: `{"id", "name"}`.

    Алиас каталог не перечисляет, но он и значит «свежая модель этого семейства», а
    каталог отсортирован по свежести — берём первую подходящую. Панели это нужно, чтобы
    показать не слово `opus`, а ту же строку `Opus 5`, что стоит в списке: иначе в
    выпадашке два разных пункта об одной модели.

    Не нашлось (нет сети, незнакомое имя) — отдаём как есть: соврать хуже, чем показать
    сырую строку.
    """
    cat = await models()
    for m in cat:
        if m["id"] == value:
            return m
    for m in cat:
        if m["id"].startswith(f"claude-{value}-"):
            return m
    return {"id": value, "name": value}


# Алиасы «последняя модель этого семейства». В каталоге их нет — он перечисляет только
# конкретные версии, — поэтому список держим рядом. Проверено прогоном `claude -p --model
# <алиас>` на 2.1.270: эти шесть CLI принимает, `sonnet-5` уже отвергает.
ALIASES = {"opus", "sonnet", "haiku", "fable", "default", "opusplan"}


async def check_model(model: str) -> str | None:
    """Знаем ли такую модель. Текст ошибки или None.

    По каталогу, а не по жалобе CLI: формулировка жалобы уже сменилась с `is not a model`
    на `isn\'t described by this version\'s model catalog`, и проверка молча пропускала
    любую опечатку — `/model фигня` сохранялся как есть и ломал следующий прогон.

    Каталог недоступен — не запрещаем: интернета может не быть, а это не повод не дать
    сменить модель.
    """
    if model in ALIASES:
        return None
    cat = await models()
    if not cat or any(m["id"] == model for m in cat):
        return None
    return (f"не знаю модель «{model}». Полные имена: "
            + ", ".join(m["id"] for m in cat[:4]) + " …; алиасы: "
            + ", ".join(sorted(ALIASES)))
