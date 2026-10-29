"""Аккаунт: вход по подписке или по ключу API, лимиты и каталог моделей.

Вынесено из `runner`: к запуску claude это не относится — тут только HTTP к API аккаунта
токеном из CREDS. Занятость бота, от которой зависит срок кеша лимитов, приходит
параметром: импорт `runner` отсюда замкнул бы модули в кольцо.
"""

import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

import store

log = logging.getLogger("claude_bot.account")

CREDS = "/root/.claude/.credentials.json"
# Ключ API, введённый в панели, — в `env` пользовательских настроек claude. Файл на томе
# `/root`: переживает пересоздание контейнера, его же читает claude из IDE по ssh, и
# рестарт не нужен — каждый прогон поднимает claude заново. Переменные из этого `env`
# перебивают одноимённые из `.env` контейнера: проверено 2026-10-29 на 2.1.x вместе с
# `--settings /opt/claude/settings.json`, которым бот запускает прогоны.
SETTINGS = "/root/.claude/settings.json"
KEY_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS")
API = "https://api.anthropic.com"

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


def _settings() -> dict:
    """Пользовательские настройки claude. Нет файла или он битый — пустые."""
    try:
        got = json.loads(Path(SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _headers(raw: str) -> dict:
    """`ANTHROPIC_CUSTOM_HEADERS` в словарь. Формат claude: строки `Имя: значение`."""
    return dict((n.strip(), v.strip()) for n, _, v in
                (line.partition(":") for line in raw.splitlines()) if n.strip() and v.strip())


def key() -> dict:
    """Вход по ключу API, если он есть: `{url, key, headers, host, source}`, иначе `{}`.

    Источников два, и сливаются они так же, как у самого claude: переменная из
    `settings.json` перебивает такую же из окружения контейнера. `source` — откуда ключ:
    `panel` можно снять из панели, `env` — только правкой `.env` инстанса.

    Прокси любой — свой пул, TeamClaude, сам api.anthropic.com: режим один на всех.
    """
    panel = {k: v for k, v in (_settings().get("env") or {}).items() if k in KEY_VARS}
    env = {k: os.environ.get(k, "") for k in KEY_VARS} | panel
    if not env["ANTHROPIC_API_KEY"]:
        return {}
    url = (env["ANTHROPIC_BASE_URL"] or API).rstrip("/")
    return {"url": url, "key": env["ANTHROPIC_API_KEY"],
            "headers": _headers(env["ANTHROPIC_CUSTOM_HEADERS"]),
            "host": urlparse(url).hostname or url,
            "source": "panel" if panel.get("ANTHROPIC_API_KEY") else "env"}


def save_key(url: str, api_key: str, headers: str) -> None:
    """Записать ключ в `settings.json`, пустой ключ — убрать. Остальные настройки целы.

    Пишем все три переменные, даже пустые: иначе недостающая взялась бы из `.env`
    контейнера, и заголовок TeamClaude уехал бы в чужой прокси.
    """
    data = _settings()
    env = {k: v for k, v in (data.get("env") or {}).items() if k not in KEY_VARS}
    if api_key:
        env |= dict(zip(KEY_VARS, (url or API, api_key, headers), strict=True))
    if env:
        data["env"] = env
    else:
        data.pop("env", None)
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    # 0600 с момента создания: в файле ключ, а у settings.json по умолчанию 0664.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)   # O_CREAT не трогает права уже существующего файла
    os.replace(tmp, SETTINGS)


async def check_key(url: str, api_key: str, headers: str) -> str | None:
    """Пустит ли прокси с этим ключом: `GET /v1/models`. Текст отказа или None.

    Проверка до записи: с битым ключом каждый следующий прогон падал бы, а панель
    честно рисовала бы «ключ активен» — сроков у ключа нет, судить больше не по чему.
    """
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s, \
                s.get(f"{(url or API).rstrip('/')}/v1/models?limit=1",
                      headers={"x-api-key": api_key, **_headers(headers),
                               "anthropic-version": "2023-06-01"}) as r:
            if r.status == 200:
                return None
            return f"прокси ответил {r.status}: {(await r.text())[:200]}"
    except Exception as e:
        return f"прокси недоступен: {e}"


async def _api_get(*urls: str, extra: dict | None = None, k: dict | None = None) -> list:
    """GET по адресам аккаунта токеном подписки. Несколько адресов — одной сессией.

    Заголовки и таймаут одни на всех: и лимиты, и каталог моделей ходят на тот же
    хост тем же OAuth-токеном, и собирать их по второму разу значило разъехаться в
    день, когда `anthropic-beta` сменится.
    """
    headers = ({"x-api-key": k["key"], **k["headers"]} if k else
               {"Authorization": f"Bearer {_oauth_token()}",
                "anthropic-beta": "oauth-2025-04-20"}) | (extra or {})
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

    Ключ API сроков не имеет: он есть — и вход есть. Он же перебивает подписку и у
    самого claude, поэтому и тут проверяется первым.
    """
    if k := key():
        return {"ok": True, "fresh": True, "renewable": True, "kind": "key",
                "host": k["host"], "source": k["source"]}
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
LIMIT_NAMES = {"session": "сессия", "weekly_all": "неделя", "weekly_scoped": "неделя",
               "5h": "сессия", "7d": "неделя"}   # последние два — окна из `/stats` пула
# Чей вход сейчас: `None` — подписка, иначе (адрес, ключ). Сменился — кеши лимитов и
# моделей прошлого входа не про нынешний.
_limits_mode: tuple | None = None
_models_mode: tuple | None = None

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


def _stats_bars(stats: dict) -> tuple[str, list[dict]]:
    """Окна из `GET <прокси>/stats` в полоски панели: (имя аккаунта, полоски).

    Формат — нашего пула (`services/claude_pool`): у каждого аккаунта `windows` с долей
    и сбросом, `clients` — кто на нём сидит, `client` — чей ключ спросил. Свой аккаунт
    по `client`; не нашёлся (клиент ещё ничего не спрашивал) — худшее окно по всем, чтобы
    не обещать запас, которого может не быть. Прокси с другим ответом даёт пустой список.
    """
    accounts = [a for a in stats.get("accounts") or [] if isinstance(a, dict)]
    me = next((a for a in accounts if stats.get("client") in (a.get("clients") or [])), None)
    worst: dict[str, dict] = {}
    for a in [me] if me else accounts:
        for w, v in (a.get("windows") or {}).items():
            try:
                used = float(v["used"])
            except (KeyError, TypeError, ValueError):
                continue
            if w not in worst or used > worst[w]["used"]:
                worst[w] = {"used": used, "resets": v.get("resets") or ""}
    return (me or {}).get("name") or "", [
        {"name": LIMIT_NAMES.get(w, w), "percent": round(v["used"] * 100),
         "resets": v["resets"], "severity": "normal"} for w, v in sorted(worst.items())]


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
    global _limits, _limits_wait, _limits_mode
    k = key()
    if (mode := (k["url"], k["key"]) if k else None) != _limits_mode:
        _limits, _limits_wait, _limits_mode = (float("-inf"), {}), LIMITS_RETRY, mode
    now = time.monotonic()
    ttl = LIMITS_TTL_BUSY if busy else LIMITS_TTL_IDLE
    # Холодный старт: показываем запомненное сразу, а запрос уходит этим же вызовом.
    # Числа с отметкой времени — панель сама решит, насколько они устарели. Только у
    # подписки: запомненное в базе — её, а пул отвечает сразу и без лимита на опрос.
    if not k and _limits[0] == float("-inf") and (was := _remembered(LIMITS_KEY, {})):
        _limits = (now - ttl - LIMITS_JITTER, was)
    fresh = now - _limits[0] < (ttl + LIMITS_JITTER if _limits[1] else _limits_wait)
    if fresh and not _reset_passed(_limits[1]):
        return _limits[1]
    out: dict = {}
    try:
        if k:
            # Полоски по ключу — только если прокси их отдаёт. Нет `/stats` (TeamClaude) —
            # обычное дело, а не сбой: панель покажет хост без полосок.
            stats, = await _api_get(f"{k['url']}/stats", k=k)
            name, bars = _stats_bars(stats)
            if bars:
                out = {"email": f"ключ API · {k['host']}", "plan": name,
                       "bars": bars, "at": time.time()}
        else:
            usage, profile = await _api_get(f"{OAUTH_API}/usage", f"{OAUTH_API}/profile")
            if bars := _bars(usage):
                out = {"email": (profile.get("account") or {}).get("email") or "",
                       "plan": _plan(profile), "bars": bars, "at": time.time()}
                store.put(LIMITS_KEY, json.dumps(out))
            else:
                log.warning("лимиты подписки: в ответе нет процентов, %s", str(usage)[:200])
    except Exception as e:
        log.log(logging.DEBUG if k else logging.WARNING, "лимиты не прочитались: %s", e)
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
    global _models, _models_mode
    k = key()
    if (mode := (k["url"], k["key"]) if k else None) != _models_mode:
        # Каталог прошлого входа показываем, пока не придёт свой, но спрашиваем сразу.
        _models, _models_mode = (float("-inf"), _models[1]), mode
    now = time.monotonic()
    if _models[0] == float("-inf") and (was := _remembered(MODELS_KEY, [])):
        _models = (now - MODELS_TTL, was)   # показываем запомненное, запрос уйдёт сейчас
    if now - _models[0] < (MODELS_TTL if _models[1] else MODELS_RETRY):
        return _models[1]
    out: list[dict] = []
    try:
        url = f"{k['url']}/v1/models?limit=50" if k else MODELS_URL
        body, = await _api_get(url, extra={"anthropic-version": "2023-06-01"}, k=k)
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
