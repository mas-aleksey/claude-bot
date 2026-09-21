"""Рабочее пространство в браузере: несколько сессий на одной странице.

Живёт в процессе бота, в netns dind — у сессий из браузера ровно то же окружение, что
у сессий из Telegram: `localhost:2375`, те же порты тестовых контейнеров, те же тома.
Отдельным контейнером на сети `proxy` этого не получить, а маршрут из netns даёт хост:
порт публикуется на шлюзе сети proxy, Traefik ходит на него так же, как на AdGuard.

Параллельность бесплатна — `runner._runs` уже словарь, а скоупом служит id панели из
браузера. Панель живёт в localStorage, поэтому её скоуп переживает перезагрузку страницы
и кнопка «стоп» после F5 бьёт по своему запуску.

Вывод берётся из транскрипта, а не из процесса: claude пишет файл по ходу, сервер
тейлит его с байтового оффсета и отдаёт панели потоком SSE. Поэтому запуск, начатый в
Telegram, виден в браузере тем же механизмом, что и свой.

Один запуск на панель одновременно; промпт, присланный в занятую панель, встаёт в
очередь `runner.slot` и уходит сам, когда освободится место.
Гонять одну сессию из двух мест одновременно никто не мешает —
проверки на это нет сознательно, два claude в одном транскрипте просто перемешают
записи. Понадобится защита — сравнивать session_id активных запусков в `runner`.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from aiohttp import web

import files
import runner
import sessions
import store
import transcript
from page import PAGE

log = logging.getLogger("claude_bot.webui")


# id панели генерит браузер, а он становится ключом в `runner._runs` и попадает в логи.
PANE_RE = re.compile(r"[0-9a-zA-Z-]{4,64}\Z")


# Как часто сервер смотрит на хвост транскрипта. Чтение стоит дописанных байт, поэтому
# частота ограничена не ценой, а тем, что быстрее человек всё равно не заметит.
TAIL_TICK = 0.3
# Холостых заходов между служебными комментариями в молчащем потоке — примерно 20 секунд.
PING_EVERY = 60


# Столько ждём `session_id` от claude, прежде чем ответить панели «не завелось».
# Первое событие приходит за пару секунд, но на холодном старте бывает дольше.
INIT_TIMEOUT = 90

# Ссылки на фоновые прогоны: без них сборщик мусора вправе убить запуск на середине.
_tasks: set[asyncio.Task] = set()

# Последняя ошибка прогона по скоупу. Без неё упавший запуск выглядел в браузере как
# молчание: индикатор гаснет, в панели ничего, и человек ждёт ответа, которого не будет.
# Текст живёт до следующего запуска в той же панели.
_errors: dict[str, str] = {}

# Ответ местной команды claude (`/cost`, `/model`, `/context`) по скоупу. Такие команды
# claude отвечает сам, не обращаясь к модели, и в транскрипт ответ не пишет — там
# остаются только пометка клиента и имя команды. Панель читает транскрипт, поэтому без
# этого словаря она показывала серую пометку и молчание. В Telegram то же самое видно
# всегда: он рендерит поток событий, а не файл.
_local: dict[str, str] = {}

# Итог последнего прогона по скоупу: `{"at": время, "text": строка}`. Живёт до начала
# следующего запуска в той же панели, как и `_errors`. Время нужно панели, чтобы отличить
# новый итог от уже показанного: два прогона подряд могут дать посимвольно равный текст.
_stats: dict[str, dict] = {}


def peers() -> list[dict]:
    """`WEB_PEERS=one=https://one.example,two=https://two.example` → вкладки в шапке.

    Список задаётся руками, а не выясняется сам: инстансы друг о друге не знают, у
    каждого свой compose, своя сеть и свой домен. Пусто или одна запись — вкладок нет,
    одиночная песочница выглядит как раньше.
    """
    out = []
    for chunk in os.environ.get("WEB_PEERS", "").split(","):
        name, _, url = chunk.partition("=")
        if name.strip() and url.strip():
            out.append({"name": name.strip(), "url": url.strip()})
    return out


# Скиллы для подсказки по `/`. Каталог тот же, что читает claude: бот и прогон живут в
# одном контейнере, поэтому список в браузере — это ровно то, что сработает в сессии.
# Плагины (`/root/.claude/plugins`) сюда не входят: у них своё устройство, а каталог
# скиллов — один файл на скилл.
SKILLS = Path("/root/.claude/skills")
# Имя в подсказку берём только такое, каким его можно набрать после слеша.
SKILL_NAME_RE = re.compile(r"[\w-]{1,64}\Z")
FRONT_RE = re.compile(r"^(name|description):\s*(.+)$", re.M)
# Встроенные команды claude: в каталоге скиллов их нет, а в панели они нужны наравне.
# Список руками, потому что наружу claude свой набор не отдаёт (`/help` в headless как
# раз и отвечает «isn't available»). Каждая строка проверена прогоном `claude -p`
# на 2.1.269: то, что открывает панель TUI (`/permissions`, `/hooks`, `/resume`,
# `/export`, `/ide`, `/vim`, `/statusline`, `/sandbox`, `/add-dir`, `/bashes`,
# `/memory`, `/status`, `/release-notes`, `/privacy-settings`, `/theme`), в headless
# отвечает отказом и в меню не попало.
#
# Чего тут нет сознательно: `/login`, `/logout`, `/upgrade`, `/terminal-setup`,
# `/install-github-app`, `/migrate-installer`, `/bug`. Они меняют общее состояние —
# OAuth-ключ один на все сессии контейнера, и разлогин из панели убил бы и бота.
BUILTIN = [
    {"name": "clear", "desc": "начать разговор с чистого листа, панель перейдёт в новую сессию"},
    {"name": "compact", "desc": "сжать контекст, сохранив суть разговора"},
    {"name": "context", "desc": "на что потрачен контекст этой сессии"},
    {"name": "cost", "desc": "расход подписки: сессия, неделя, когда сброс"},
    {"name": "usage", "desc": "то же, что /cost"},
    {"name": "model", "desc": "показать модель сессии; сменить нельзя — её задаёт панель"},
    {"name": "output-style", "desc": "стиль ответа: текущий и список доступных"},
    {"name": "config", "desc": "настройки claude: показать и поменять"},
    {"name": "mcp", "desc": "состояние MCP-серверов"},
    {"name": "doctor", "desc": "проверка установки claude"},
    {"name": "init", "desc": "собрать CLAUDE.md по текущему проекту"},
    {"name": "review", "desc": "ревью изменений в рабочем дереве"},
    {"name": "security-review", "desc": "ревью изменений на дыры, нужен git-репозиторий"},
]


def skills(project: str = "") -> list[dict]:
    """Команды для автодополнения: скиллы общие и проектные, затем встроенные.

    Скиллы наверху, потому что их тринадцать встроенных не должны отжимать вниз: в
    панель ходят за своим `/refine` и `/end`, а `/doctor` набирают раз в полгода.

    Проектные (`<project>/.claude/skills`) идут вторыми и перекрывают общие по имени —
    так же, как их разрешает сам claude. Без них панель localhome не знала бы про
    `/bw` и `/sync-repo`, а вызываются они именно там.

    Каталог без `SKILL.md` пропускается молча: в `skills/` попадают и черновики.
    """
    out: dict[str, dict] = {}
    for root in [SKILLS] + ([Path(project) / ".claude" / "skills"] if project else []):
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
            meta = {k: v.strip() for k, v in FRONT_RE.findall(head)}
            name = meta.get("name", "")
            # Имя из frontmatter бывает и с пробелами, и вовсе отсутствует, а набирают
            # скилл каталогом — на нём и стоим, когда frontmatter не годится.
            if not SKILL_NAME_RE.match(name):
                name = path.parent.name
            out[name] = {"name": name, "desc": meta.get("description", "")[:160]}
    return sorted(out.values(), key=lambda s: s["name"]) + BUILTIN


def _int(value: str | None) -> int:
    """`from` из query. Мусор — это ноль, а не 500: панель не должна падать от
    правки адреса руками."""
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


# Только форма имени: значение уходит в argv через create_subprocess_exec, без шелла,
# поэтому это гигиена, а не защита. Неизвестное имя модели отвергнет сам claude, и
# теперь его текст видно в панели.
MODEL_RE = re.compile(r"[a-zA-Z0-9._-]{2,64}\Z")


# Ниже этого размера цифра в списке — шум: у большинства сессий она одинаково мелкая.
# Выше — предупреждение, что панель будет открываться заметно дольше.
HEAVY = 1 << 20


def _path(project: str, session_id: str) -> Path:
    """Путь к транскрипту с клиентскими параметрами. `transcript` про HTTP не знает и
    бросает ValueError — переводим его в 400 здесь, на границе."""
    try:
        return transcript.path_of(project, session_id)
    except ValueError as err:
        raise web.HTTPBadRequest(text=str(err)) from err


def _heavy(project: str, session_id: str) -> str:
    """Размер транскрипта, но только если он большой. Пустая строка — не показывать."""
    try:
        size = transcript.path_of(project, session_id).stat().st_size
    except (OSError, ValueError):
        return ""
    return f"{size / HEAVY:.1f} МБ" if size >= HEAVY else ""


def _project(raw: str) -> str:
    """Проект из запроса. Только то, что реально примонтировано: строка уходит в `cwd`
    процесса claude, и `/etc` тут был бы полноценным рабочим каталогом."""
    if raw in {str(p) for p in sessions.projects()}:
        return raw
    raise web.HTTPBadRequest(text="нет такого проекта")


def _answered_locally(ev: dict) -> bool:
    """Ответил ли claude сам, не обращаясь к модели.

    Признак — нулевые цена и токены в `result`: местная команда до API не доходит.
    Проверено на `/cost` и `/model`, оба отдают текст при `total_cost_usd: 0` и
    `output_tokens: 0`. Настоящий прогон обоих нулей одновременно дать не может.

    Нужно, чтобы не дублировать обычный ответ: у него тот же `result`, но его панель
    уже вытянула из транскрипта.
    """
    return not ev.get("total_cost_usd") and not (ev.get("usage") or {}).get("output_tokens")


async def _drive(scope: str, prompt: str, project: str, session_id: str | None,
                 got: asyncio.Future, model: str | None = None, adopt: bool = False) -> None:
    """Довести запуск до конца, ничего не рендеря: вывод claude сам пишет в транскрипт,
    а панель его тейлит. Наружу отдаём только первый session_id — панели нужно знать,
    какой файл читать, особенно когда сессия новая и id придумал claude.

    `adopt` ставит промпт, который ждал очереди без сессии, в сессию предыдущего
    прогона. Без него два промпта подряд в пустую панель открыли бы две разные сессии
    вместо продолжения разговора.
    """
    sid = session_id
    err = ""
    _errors.pop(scope, None)  # новый запуск — прошлая ошибка больше не про него
    _local.pop(scope, None)
    _stats.pop(scope, None)
    seen_model = None
    try:
        async with runner.slot(scope):
            if adopt:
                sid = session_id = runner.last_session(scope) or session_id
            # Модель панели, а иначе глобальная из бота: две панели на разных моделях —
            # ровно то, ради чего делалась параллельность.
            async for ev in runner.run(prompt, project, session_id,
                                       model or store.get("model"), scope=scope):
                if not got.done() and (sid := ev.get("session_id") or sid):
                    got.set_result(sid)
                # Внятная причина приходит в `result`, а не в стоп-коде: лимит подписки,
                # отказ модели, недоступный проект — всё это claude пишет в stdout и
                # выходит с rc=1 при пустом stderr. Поэтому текст result важнее кода.
                # Модель берём из ответа, а не из того, что просили: панель с пустым
                # выбором едет на общей модели бота, а сессия могла быть заведена на другой.
                if ev.get("type") == "assistant":
                    seen_model = (ev.get("message") or {}).get("model") or seen_model
                if ev.get("type") == "result":
                    if ev.get("is_error"):
                        err = (ev.get("result") or "").strip()[:2000]
                    elif _answered_locally(ev) and (said := (ev.get("result") or "").strip()):
                        _local[scope] = said
                    elif line := transcript.stat_line(ev, seen_model):
                        _stats[scope] = {"at": time.time(), "text": line}
                if ev.get("type") == "_bot" and ev.get("kind") == "error":
                    rc = ev.get("rc")
                    stderr = (ev.get("text") or "").strip()
                    # Текст вперёд, код в скобках: читают ошибку, а не номер. Если текста
                    # нет ни в result, ни в stderr — говорим это словами, потому что голое
                    # `rc=1` не подсказывает даже, куда смотреть.
                    err = err or (f"{stderr[:2000]} (rc={rc})" if stderr else
                                  f"claude вышел с кодом {rc} и ничего не сообщил — "
                                  f"причина, если она есть, в последнем ответе выше")
                    log.warning("scope=%s rc=%s %s", scope, rc, stderr[:300])
    except runner.Dropped:
        log.info("промпт отброшен из очереди: scope=%s", scope)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.exception("прогон из веба упал: scope=%s", scope)
    finally:
        if err:
            _errors[scope] = err
        # None — панель покажет ошибку. Ставим результат, а не исключение: ждать его
        # уже могло некому, а невынутое исключение из future засоряет лог.
        if not got.done():
            got.set_result(sid)


async def index(_: web.Request) -> web.Response:
    # no-store: страница целиком лежит в образе, и после раскатки вкладка обязана
    # взять новую. Валидаторов у ответа нет, поэтому без этого заголовка браузер
    # вправе отдать свою копию, и человек сидит на прошлой версии панели.
    return web.Response(text=PAGE, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def api_peers(_: web.Request) -> web.Response:
    return web.json_response(peers())


async def api_models(_: web.Request) -> web.Response:
    return web.json_response(await runner.models())


async def api_projects(_: web.Request) -> web.Response:
    return web.json_response(
        [{"name": p.name, "path": str(p)} for p in sessions.projects()]
    )


async def api_skills(req: web.Request) -> web.Response:
    # Проект проверяем тем же `_project`: путь уходит в glob, и чужие каталоги тут
    # читать нечего. Панель со снесённым проектом получит 400 и останется без
    # подсказки — предлагать ей скиллы всё равно некуда.
    project = req.query.get("project") or ""
    return web.json_response(skills(_project(project) if project else ""))


def _row(sid: str, title: str, age: float, **extra) -> dict:
    """Строка списка сессий. Общая и для списка, и для поиска — в панели это один
    и тот же элемент, отличается только четвёртое поле."""
    return {"id": sid, "title": title or sid, "ago": sessions.ago(age), **extra}


async def api_sessions(req: web.Request) -> web.Response:
    # Диск, а не asyncio: заголовок сессии читается из транскрипта целиком, а он
    # бывает на десятки мегабайт — в общем event loop это заморозило бы long-poll.
    project = req.query.get("project", "")
    found = await asyncio.to_thread(sessions.recent, project, 30)
    return web.json_response([_row(sid, title, age, size=_heavy(project, sid))
                              for sid, title, age in found])


async def api_name(req: web.Request) -> web.Response:
    """Имя сессии из панели — то же, что `/name` в Telegram, тот же ключ в `state`.

    Id проверяем тем же `SESSION_RE`, что и запуск: из него собирается ключ в базе, и
    принимать туда произвольную строку от браузера незачем. Пустое имя снимает ключ.
    """
    data = await req.json()
    sid = data.get("session") or ""
    if not transcript.SESSION_RE.match(sid):
        return web.json_response({"error": "bad session"}, status=400)
    store.put(f"name:{sid}", (data.get("name") or "").strip() or None)
    return web.json_response({"ok": True})


async def api_stream(req: web.Request) -> web.StreamResponse:
    """Хвост транскрипта, пока панель открыта: сервер сам говорит о новых строках.

    Оффсет едет в `id:` каждого кадра. Браузер при обрыве переподключается сам и
    возвращает его в `Last-Event-ID` — поэтому переподключение продолжает с места,
    хотя адрес потока остался прежним и `from` в нём давно устарел.

    Отсутствие файла — нормальное состояние, а не ошибка: id новой сессии известен
    раньше, чем claude успевает создать транскрипт. Поток просто ждёт.
    """
    path = _path(req.query.get("project", ""), req.query.get("id", ""))
    off = _int(req.headers.get("Last-Event-ID") or req.query.get("from"))
    res = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        # Traefik ответ не собирает, но заголовок стоит копейку и страхует от прокси,
        # который решит иначе: тогда панель молчала бы до конца потока, то есть всегда.
        "X-Accel-Buffering": "no",
    })
    await res.prepare(req)
    idle = 0
    try:
        while True:
            found: list[dict] = []
            ctx = None
            reset = False
            try:
                size = path.stat().st_size
            except OSError:
                size = None  # транскрипта ещё нет, claude его вот-вот создаст
            if size is not None:
                # Файл короче нашего оффсета — его переписали или подменили. `seek`
                # за его конец молчит вечно, и панель выглядит зависшей при живом
                # потоке. Читаем сначала и просим панель очистить лог: показанное
                # относится к прежнему содержимому файла и уже неверно.
                if size < off:
                    off, reset = 0, True
                # Диск в потоке: первый заход читает сессию целиком, а она бывает на
                # десятки мегабайт — в общем event loop это заморозило бы все панели.
                off, found, ctx = await asyncio.to_thread(transcript.items, path, off)
            if found or ctx:
                body = json.dumps({"next": off, "items": found, "ctx": ctx,
                                   "reset": reset})
                await res.write(f"id: {off}\ndata: {body}\n\n".encode())
            if found:
                idle = 0
                continue  # кусок мог упереться в CHUNK — дочитываем без паузы
            # Комментарий раз в ~20 секунд: молчащее соединение рвут и прокси, и
            # мобильная сеть, а браузер комментарий молча выбрасывает.
            if (idle := idle + 1) % PING_EVERY == 0:
                await res.write(b": ping\n\n")
            await asyncio.sleep(TAIL_TICK)
    except ConnectionResetError:
        pass  # вкладку закрыли — обычный конец потока, а не сбой
    return res


async def api_search(req: web.Request) -> web.Response:
    """Поиск по сессиям проекта. Диск в потоке: скан всех транскриптов проекта —
    полсекунды на 45 МБ, но держать на это event loop незачем."""
    found = await asyncio.to_thread(
        sessions.search, req.query.get("project", ""), req.query.get("q", ""), 20)
    return web.json_response([_row(sid, title, age, snippet=snip)
                              for sid, title, age, snip in found])


async def api_purge(req: web.Request) -> web.Response:
    """GET — предпросмотр, POST — удаление. Разными методами не ради красоты:
    удаление необратимо, и промах адресной строкой не должен его запускать."""
    days = sessions.days(req.query.get("days") if req.method == "GET"
                 else (await req.json()).get("days"))
    older = days * 86400
    if req.method == "GET":
        doomed = await asyncio.to_thread(sessions.stale, older)
        return web.json_response({"days": days, "sessions": doomed,
                                  "bytes": sum(r["bytes"] for r in doomed)})

    killed = await sessions.run_purge(older)
    log.info("purge: старше %.1f дн, снесено %s", days, killed)
    return web.json_response(killed)


async def api_status(_: web.Request) -> web.Response:
    """Живые запуски и упавшие прогоны. Запуски берутся из тех же `runner._runs`,
    что у Telegram, и несут id сессии — по нему панель узнаёт свой сеанс, даже если
    его гоняют из топика под другим скоупом.

    Общая модель — оттуда же, откуда её берёт `_drive` при пустом выборе в панели.
    Без неё в селекте стояло безымянное «модель», и что именно поедет в claude,
    из панели было не видно.

    Лимиты подписки едут тем же ответом, а не своим роутом: он уже опрашивается
    раз в три секунды, а `runner.limits` держит свой минутный кеш — выходит один
    запрос в API на минуту на все открытые вкладки.
    """
    return web.json_response({"runs": runner.active(), "errors": _errors,
                              "local": _local, "stats": _stats,
                              "queued": runner.waiting(),
                              "limits": await runner.limits(),
                              "model": await runner.resolve_model(
                                  store.get("model") or runner.default_model())})


async def api_prompt(req: web.Request) -> web.Response:
    data = await req.json()
    prompt = (data.get("prompt") or "").strip()
    pane = data.get("pane") or ""
    session_id = data.get("session") or None
    if not prompt or not PANE_RE.match(pane):
        raise web.HTTPBadRequest(text="нужны prompt и pane")
    if session_id and not transcript.SESSION_RE.match(session_id):
        raise web.HTTPBadRequest(text="плохой id сессии")
    model = (data.get("model") or "").strip() or None
    if model and not MODEL_RE.match(model):
        raise web.HTTPBadRequest(text="плохое имя модели")
    project = _project(data.get("project") or "")

    scope = f"web:{pane}"
    queued = runner.ahead(scope)

    got: asyncio.Future = asyncio.get_running_loop().create_future()
    # Задача живёт дольше запроса: ответ панели — только session_id, а прогон
    # продолжается в фоне и виден ей через транскрипт.
    task = asyncio.create_task(_drive(scope, prompt, project, session_id, got, model,
                                      adopt=bool(queued) and session_id is None))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    if queued:
        # Ждать нечего: прогон начнётся после предыдущего, а id новой сессии панель
        # подберёт из /api/status по своему скоупу — в том числе после F5.
        return web.json_response({"queued": queued, "session": session_id})
    try:
        sid = await asyncio.wait_for(asyncio.shield(got), INIT_TIMEOUT)
    except TimeoutError:
        sid = None
    return web.json_response({"session": sid})


async def api_cancel(req: web.Request) -> web.Response:
    data = await req.json()
    pane = data.get("pane") or ""
    if not PANE_RE.match(pane):
        raise web.HTTPBadRequest(text="нужен pane")
    stopped, dropped = await runner.cancel(f"web:{pane}")
    return web.json_response({"stopped": stopped, "dropped": dropped})


def build() -> web.Application:
    # client_max_size — предел на тело запроса. По умолчанию у aiohttp мегабайт, и
    # загрузка файла падала бы с 413 раньше нашего кода.
    app = web.Application(client_max_size=files.MAX_UPLOAD)
    app.add_routes([
        web.get("/", index),
        web.get("/api/peers", api_peers),
        web.get("/api/models", api_models),
        web.get("/api/projects", api_projects),
        web.get("/api/skills", api_skills),
        web.get("/api/sessions", api_sessions),
        web.post("/api/name", api_name),
        web.get("/api/search", api_search),
        web.get("/api/roots", files.api_roots),
        web.get("/api/files", files.api_files),
        web.get("/api/file", files.api_file),
        web.post("/api/file", files.api_save),
        web.post("/api/new", files.api_new),
        web.get("/api/purge", api_purge),
        web.post("/api/purge", api_purge),
        web.get("/api/stream", api_stream),
        web.get("/api/status", api_status),
        web.post("/api/prompt", api_prompt),
        web.post("/api/upload", files.api_upload),
        web.post("/api/cancel", api_cancel),
    ])
    return app


async def start(port: int) -> None:
    site = web.AppRunner(build())
    await site.setup()
    await web.TCPSite(site, "0.0.0.0", port).start()
    log.info("webui на :%d", port)
