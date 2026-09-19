"""Запуск claude-cli: стрим-режим для промптов, обычный пайп для подкоманд."""

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import pty
import random
import re
import signal
import subprocess
import time
from collections.abc import AsyncIterator

import aiohttp

import store
from render import strip_ansi

log = logging.getLogger("claude_bot.runner")

# --settings: путь, а не содержимое — файл лежит в образе и читается claude на каждом
# запуске. Он выбирает output style, то есть правит СИСТЕМНЫЙ промпт; CLAUDE.md инстанса
# остаётся описанием окружения. Правка стиля требует пересборки образа, но это ровно тот
# темп, в котором он меняется.
SETTINGS = "/opt/claude/settings.json"
BASE = ["claude", "--permission-mode", "bypassPermissions", "--settings", SETTINGS]
CREDS = "/root/.claude/.credentials.json"
CONFIG = "/root/.claude.json"
# Домен OAuth уже переезжал (claude.ai → claude.com), поэтому ловим по пути /oauth/,
# а не по списку хостов. Проверено на 2.1.220: claude.com/cai/oauth/authorize?...
URL_RE = re.compile(r"https://\S+/oauth/\S+")

# Лимиты подписки. В headless их не спросить — `/usage` и `/cost` отвечают только внутри
# сессии и текстом, — зато CLI берёт их с этого эндпоинта, и токен для него уже лежит
# в CREDS. Ходим туда же сами.
OAUTH_API = "https://api.anthropic.com/api/oauth"
# Две минуты: проценты столько не меняются, а запрос идёт от каждого инстанса.
LIMITS_TTL = 120
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


def _oauth_token() -> str:
    """Токен подписки из CREDS. Протухший не отдаём вовсе.

    Обновляет его CLI на ближайшем прогоне, а идти с ним в API не просто бесполезно:
    на surf такие запросы раз в полминуты сначала получали `authentication_error`, а
    потом утянули аккаунт в `rate_limit_error` самого эндпоинта.
    """
    with open(CREDS, encoding="utf-8") as f:
        oauth = json.load(f)["claudeAiOauth"]
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

# Запуск на скоуп, а не один на бота: топик форума = своя сессия, и две сессии должны
# идти параллельно. Личка и обычная группа живут в скоупе "0".
# Хранится тройка (процесс, момент старта, id сессии). Время нужно интерфейсу для
# «работает 0:42», а сессия — чтобы панель в браузере понимала, что её сеанс гоняют из
# Telegram: скоупы у них разные, а транскрипт один. Одной структурой, а не тремя
# словарями, — так они не разъедутся.
_runs: dict[str, tuple[asyncio.subprocess.Process, float, str | None]] = {}

@dataclasses.dataclass
class _Queue:
    """Очередь одного скоупа. Занятая панель (или топик) не отказывает, а копит:
    следующий промпт ждёт своего места и уходит в claude, как только предыдущий
    прогон закончился.

    Порядок даёт сам `asyncio.Lock` — он будит ожидающих в порядке постановки, поэтому
    своего deque не нужно, нужен только счётчик ждущих для интерфейса.

    `epoch` двигает `cancel`, и ожидающие, проснувшись, понимают, что их отбросили.
    Разбудить их иначе нечем — они висят на том же локе, который держит текущий
    прогон, и просыпаются только после его смерти.

    Три поля вместе, а не три словаря по скоупу: они заводятся одним вызовом и
    выкидываются одним, и держать этот инвариант в трёх контейнерах значило каждый
    раз не забыть третий `pop`.
    """

    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    waiting: int = 0
    epoch: int = 0


_queues: dict[str, _Queue] = {}
# Последняя сессия скоупа. Промпт, вставший в очередь к новой сессии, её id ещё не знает:
# claude придумает его в предыдущем прогоне, уже после постановки.
_last: dict[str, str] = {}


class Dropped(Exception):
    """Промпт выкинули из очереди отменой: прогона не было и не будет."""


class Drain:
    """Когда процессу claude можно закрывать stdin, то есть выходить.

    В текстовом режиме (`-p "промпт"`) CLI выходит по первому `result` и уносит с собой
    фоновые задачи: файл вывода остаётся со словом `[killed]`, а обещание «вернусь, когда
    закончится» не сбывается никогда. Поэтому промпт едет через `--input-format
    stream-json`, и пока stdin открыт, процесс жив и сам начинает новый ход на каждое
    уведомление о завершившейся задаче.

    Признак «ждать больше нечего» собран из служебных событий CLI, а не из текста
    tool_result: список живых задач плюс отметка о пришедшем уведомлении. Второе
    закрывает гонку «задача кончилась за миг до `result`»: без него stdin закрылся бы
    ровно между уведомлением и ответом на него.

    Ждать по отметке можно только с потолком, и это не перестраховка, а разбор аварии
    2026-09-15 в песочнице surf. Уведомление о брошенных задачах прошлой сессии CLI
    отдаёт на `--resume`, ещё до первого `init`, и отрабатывает его **тем же** ходом,
    что и промпт человека, — второго `init` не будет никогда. Бессрочное ожидание
    оставило процесс висеть с открытым stdin: работа кончилась, а скоуп занят.
    Поэтому отметка снимается `system/init`, а если он не пришёл за `GRACE` — ждать
    нечего и stdin закрывается. Не по любому `assistant`: события фонового субагента
    текут в тот же поток и `init` не несут, иначе отметка снималась бы чужой строкой.

    Живой фоновой задачи потолок не касается: она разбудит поток сама, и ждать её можно
    часами.

    ponytail: контракт событий недокументирован, проверено на claude 2.1.270
    (`local_bash` и `local_agent` в одном списке). Переименуют — список останется пустым,
    и поведение выродится в сегодняшнее, выход по первому `result`.
    """

    # Ход по уведомлению начинается сразу (`init` в том же кадре потока) или не
    # начинается вовсе. Секунды тут — на неспешный диск, а не на работу модели.
    GRACE = 5.0

    def __init__(self) -> None:
        self.tasks = 0
        self.awaited = False  # уведомление пришло, ход на него ещё не начался
        self.started = False  # первый `init` — старт прогона, а не ход по уведомлению

    def feed(self, ev: dict) -> None:
        if ev.get("type") != "system":
            return
        match ev.get("subtype"):
            case "background_tasks_changed":
                self.tasks = len(ev.get("tasks") or [])
            case "task_notification":
                self.awaited = True
            case "init":
                if self.started:
                    self.awaited = False
                self.started = True

    def done(self, ev: dict) -> bool:
        """`result` при пустом фоне — прогон окончен, stdin можно закрывать."""
        return ev.get("type") == "result" and not self.tasks and not self.awaited

    def wait(self) -> float | None:
        """Сколько ждать следующего события. None — сколько угодно."""
        return self.GRACE if self.awaited and not self.tasks else None

    def give_up(self) -> None:
        """Обещанный ход не начался за `GRACE` — больше его не ждём."""
        self.awaited = False


def busy(scope: str) -> bool:
    entry = _runs.get(scope)
    return entry is not None and entry[0].returncode is None


def active() -> list[dict]:
    """Живые запуски: скоуп, длительность и сессия. Список, а не словарь по скоупу:
    сопоставлять в интерфейсе приходится и по сессии тоже."""
    now = time.monotonic()
    return [{"scope": scope, "secs": now - started, "session": sid}
            for scope, (proc, started, sid) in _runs.items() if proc.returncode is None]


def ahead(scope: str) -> int:
    """Сколько промптов уйдут в claude раньше нового: занятый слот плюс ожидающие.

    Считаем по локу, а не по `busy`: держатель слота попадает в `_runs` только когда
    доберётся до первого события claude, и в этом зазоре очередь бы отвечала «свободно».
    """
    q = _queues.get(scope)
    return 0 if q is None else (1 if q.lock.locked() else 0) + q.waiting


def waiting() -> dict[str, int]:
    """Непустые очереди по скоупам — панели, чтобы показать глубину после перезагрузки."""
    return {scope: q.waiting for scope, q in _queues.items() if q.waiting}


def last_session(scope: str) -> str | None:
    return _last.get(scope)


@contextlib.asynccontextmanager
async def slot(scope: str) -> AsyncIterator[None]:
    """Место в очереди скоупа: под `async with` внутри одновременно только один прогон.

    Счётчик ждущих растёт до `acquire`, поэтому вызывающий должен спросить `ahead`
    ДО входа сюда — иначе он посчитает в очереди сам себя.
    """
    # Запись держим ссылкой, а не перечитываем из словаря: пока мы числимся ждущими,
    # выкинуть её некому, а после `acquire` это ровно та очередь, в которую мы встали.
    q = _queues.setdefault(scope, _Queue())
    epoch = q.epoch
    q.waiting += 1
    try:
        await q.lock.acquire()
    finally:
        q.waiting -= 1
    try:
        if q.epoch != epoch:
            raise Dropped
        yield
    finally:
        q.lock.release()
        # Пусто — выкидываем состояние скоупа целиком: панелей за месяцы заводят много,
        # а живут они по одному промпту. Ждущих нет, значит на эту очередь никто не смотрит.
        if not q.waiting:
            _queues.pop(scope, None)


def _tag(scope: str, session_id: str) -> None:
    """Запомнить сессию запуска. У новой сессии id придумывает claude, поэтому он
    появляется только из первого события — вызывается изнутри `run`, чтобы ни один
    вызывающий не смог об этом забыть."""
    _last[scope] = session_id
    if entry := _runs.get(scope):
        _runs[scope] = (entry[0], entry[1], session_id)


def _patch_config(mutate) -> None:
    """Прочитать /root/.claude.json, дать `mutate` его поправить, записать если менялось.

    Файл ведёт сам claude-cli и кладёт туда много своего, поэтому только точечная
    правка: читаем целиком, меняем нужный ключ, пишем обратно.
    """
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if mutate(cfg):
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)


def trust(cwd: str) -> None:
    """Без `hasTrustDialogAccepted` claude в новой директории игнорит settings.json
    и просит принять диалог интерактивно — в headless это тупик. Диалог тут не нужен:
    в /projects попадает только то, что смонтировал владелец бота."""

    def mutate(cfg: dict) -> bool:
        proj = cfg.setdefault("projects", {}).setdefault(cwd, {})
        if proj.get("hasTrustDialogAccepted"):
            return False
        proj["hasTrustDialogAccepted"] = True
        return True

    _patch_config(mutate)


def skip_onboarding() -> None:
    """Погасить приветственный TUI, который claude показывает на свежем конфиге.

    Боту он не мешает — headless `-p` его не рисует и флаг не пишет. Мешает человеку:
    в контейнере живёт sshd, и вход из IDE упирается в «Welcome to Claude Code»
    с выбором темы, а следом в вопрос о доверии к /root — оба на пустом ${USER_DATA}.
    Зовётся после логина: раньше конфига может не быть, а на этом шаге claude его
    уже создал. Значения не перетираем — тему и доверие человек мог выбрать сам.
    """

    def mutate(cfg: dict) -> bool:
        changed = False
        if not cfg.get("hasCompletedOnboarding"):
            cfg["hasCompletedOnboarding"] = True
            changed = True
        # ssh-сессия стартует в /root, а не в проекте — свой trust нужен и ему.
        root = cfg.setdefault("projects", {}).setdefault("/root", {})
        if not root.get("hasTrustDialogAccepted"):
            root["hasTrustDialogAccepted"] = True
            changed = True
        return changed

    _patch_config(mutate)


async def run(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    model: str | None = None,
    scope: str = "0",
) -> AsyncIterator[dict]:
    """Событие за событием из `--output-format stream-json`.

    Служебные события бота отдаются с типом `_bot` — так вызывающему не нужен
    второй канал под ошибки и код возврата.
    """
    trust(cwd)
    # Промпт уходит в stdin, а не в argv: см. `Drain` — только в этом режиме процесс
    # переживает конец хода и доносит фоновые задачи до конца.
    argv = [*BASE, "-p", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose"]
    if session_id:
        argv += ["--resume", session_id]
    if model:
        argv += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # своя группа — /cancel бьёт по всем детям
        # Одно событие = одна строка, а в ней бывает целый файл. Дефолтные 64 KiB
        # роняют readline на `Separator is found, but chunk is longer than limit`.
        limit=16 * 1024 * 1024,
    )
    _runs[scope] = (proc, time.monotonic(), session_id)
    proc.stdin.write(json.dumps(
        {"type": "user", "message": {"role": "user", "content": prompt}}).encode() + b"\n")
    await proc.stdin.drain()

    drain = Drain()
    try:
        while True:
            try:
                # Читаем с потолком, а не `async for`: ожидание хода по уведомлению
                # обязано кончиться (см. Drain), а живой фоновой задачи ждём без срока.
                line = await asyncio.wait_for(proc.stdout.readline(), drain.wait())
            except TimeoutError:
                drain.give_up()
                if not proc.stdin.is_closing():
                    proc.stdin.close()
                continue
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                yield {"type": "_bot", "kind": "raw", "text": line.decode("utf-8", "replace")}
                continue
            # Сессию запоминаем здесь, а не в вызывающем: у новой её придумывает claude,
            # и первое событие — единственное место, где она становится известна всем.
            if sid := ev.get("session_id"):
                _tag(scope, sid)
            # Размер окна контекста знает только CLI, и говорит он его один раз, в
            # `result`. В транскрипт это не попадает, поэтому запоминаем на модель —
            # панель и /status считают проценты по этому числу.
            if ev.get("type") == "result":
                for name, info in (ev.get("modelUsage") or {}).items():
                    if window := info.get("contextWindow"):
                        store.put(f"ctxwin:{name}", str(window))
            # Закрываем до `yield`: вызывающий рисует ответ в Telegram, а процесс
            # столько ждать не должен.
            drain.feed(ev)
            if drain.done(ev) and not proc.stdin.is_closing():
                proc.stdin.close()
            yield ev

        rc = await proc.wait()
        err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
        if rc != 0:
            yield {"type": "_bot", "kind": "error", "rc": rc, "text": err}
    finally:
        # Вызывающий может бросить генератор на середине (ошибка рендера, отмена задачи).
        # Открытый stdin держал бы claude живым вечно — раньше он выходил сам.
        if not proc.stdin.is_closing():
            proc.stdin.close()
        if (entry := _runs.get(scope)) and entry[0] is proc:
            del _runs[scope]


async def cancel(scope: str) -> tuple[bool, int]:
    """SIGTERM группе, через 2 с — SIGKILL. proc.kill() оставил бы живых детей.

    Отмена гасит и очередь: «стоп» — это про всё, что человек сюда накидал, иначе
    следом сама собой поедет следующая задача. Возвращает (убит ли прогон, сколько
    промптов отброшено).
    """
    q = _queues.get(scope)
    dropped = q.waiting if q else 0
    if dropped:
        q.epoch += 1
    entry = _runs.get(scope)
    proc = entry[0] if entry else None
    if proc is None or proc.returncode is not None:
        return False, dropped
    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except TimeoutError:
        os.killpg(pgid, signal.SIGKILL)
    return True, dropped


async def cli(*args: str, cwd: str | None = None, timeout: float = 60) -> tuple[int, str]:
    """Неинтерактивная подкоманда claude → (код возврата, stdout+stderr)."""
    if cwd:
        trust(cwd)
    proc = await asyncio.create_subprocess_exec(
        "claude",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


class Login:
    """`claude auth login` в pty: без tty команда молча висит (проверено, POC шаг 1).

    Бот выдирает URL из потока и шлёт в чат, код из чата пишет процессу в stdin.
    """

    def __init__(self) -> None:
        self.master, self._slave = pty.openpty()
        self.proc: subprocess.Popen | None = None
        self.buf = ""

    async def start(self) -> str | None:
        """Запустить и дождаться URL. None — если URL не появился."""
        # ASYNC101: Popen тут не блокирует — pty, ждём потом через _read_until.
        self.proc = subprocess.Popen(  # noqa: ASYNC220
            ["claude", "auth", "login", "--claudeai"],
            stdin=self._slave,
            stdout=self._slave,
            stderr=self._slave,
            start_new_session=True,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(self._slave)
        os.set_blocking(self.master, False)
        return await self._read_until(URL_RE, 60)

    async def submit(self, code: str) -> tuple[bool, str]:
        os.write(self.master, code.encode() + b"\n")
        # Успех определяем по факту появления кред, а не по тексту в TUI.
        for _ in range(60):
            await asyncio.sleep(1)
            if os.path.exists(CREDS):
                self.close()
                skip_onboarding()
                return True, "авторизован"
            if self.proc and self.proc.poll() is not None:
                break
        self.close()
        return False, strip_ansi(self.buf)[-500:]

    async def _read_until(self, rx: re.Pattern, timeout: float) -> str | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(0.3)
            try:
                chunk = os.read(self.master, 65536)
            except BlockingIOError:
                continue
            except OSError:
                break
            self.buf += chunk.decode("utf-8", "replace")
            if m := rx.search(strip_ansi(self.buf)):
                return m.group(0)
        return None

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.close(self.master)


async def auth_status() -> dict:
    rc, out = await cli("auth", "status", "--json", timeout=20)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": out or f"rc={rc}"}


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


def _plan(profile: dict) -> str:
    """Тариф коротко: `default_claude_max_5x` → `max 5x`."""
    tier = (profile.get("organization") or {}).get("rate_limit_tier") or ""
    return tier.removeprefix("default_").removeprefix("claude_").replace("_", " ")


async def limits() -> dict:
    """Занятость лимитов подписки и чей это аккаунт, с кешем на LIMITS_TTL.

    Пустой словарь значит «показывать нечего»: нет файла с токеном, токен протух или
    ответ не той формы. Эндпоинт недокументированный, и смена его формы не должна
    ронять статус — панель на пустом словаре просто гасит полоски. Протухший токен
    чиним не мы: CLI обновляет CREDS на следующем прогоне, поэтому файл читаем заново
    на каждый промах кеша, а неудачу кешируем наравне с успехом — иначе трёхсекундный
    опрос панели будет долбить API.
    """
    global _limits, _limits_wait
    now = time.monotonic()
    # Холодный старт: показываем запомненное сразу, а запрос уходит этим же вызовом.
    # Числа с отметкой времени — панель сама решит, насколько они устарели.
    if _limits[0] == float("-inf") and (was := _remembered(LIMITS_KEY, {})):
        _limits = (now - LIMITS_TTL - LIMITS_JITTER, was)
    if now - _limits[0] < (LIMITS_TTL + LIMITS_JITTER if _limits[1] else _limits_wait):
        return _limits[1]
    out: dict = {}
    try:
        token = _oauth_token()
        headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
        async with aiohttp.ClientSession(
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{OAUTH_API}/usage") as r:
                usage = await r.json()
            async with s.get(f"{OAUTH_API}/profile") as r:
                profile = await r.json()
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
        _limits_wait = min(_limits_wait * 2, LIMITS_TTL)
        out = _limits[1]  # не вышло сейчас — показываем прошлое, а не пустоту
    _limits = (now, out)
    return out


async def models() -> list[dict]:
    """Модели для выпадашки панели: `{"id", "name"}`, свежие сверху.

    Тем же токеном подписки, что читает `runner.limits`: каталог отдаётся по OAuth и
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
        token = _oauth_token()
        headers = {"Authorization": f"Bearer {token}",
                   "anthropic-beta": "oauth-2025-04-20",
                   "anthropic-version": "2023-06-01"}
        async with aiohttp.ClientSession(
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as s, \
                s.get(MODELS_URL) as r:
            body = await r.json()
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


def default_model() -> str:
    """Чем пойдёт прогон, когда в боте ничего не выбрано: модель из settings.json.

    Раньше и бот, и панель показывали в этом случае слово «default», из которого не
    следует ничего. Файл лежит в образе, читается редко — кеш тут не нужен.
    """
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f).get("model") or "default"
    except (OSError, ValueError):
        return "default"


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
