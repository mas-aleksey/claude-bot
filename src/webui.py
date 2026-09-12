"""Рабочее пространство в браузере: несколько сессий на одной странице.

Живёт в процессе бота, в netns dind — у сессий из браузера ровно то же окружение, что
у сессий из Telegram: `localhost:2375`, те же порты тестовых контейнеров, те же тома.
Отдельным контейнером на сети `proxy` этого не получить, а маршрут из netns даёт хост:
порт публикуется на шлюзе сети proxy, Traefik ходит на него так же, как на AdGuard.

Параллельность бесплатна — `runner._runs` уже словарь, а скоупом служит id панели из
браузера. Панель живёт в localStorage, поэтому её скоуп переживает перезагрузку страницы
и кнопка «стоп» после F5 бьёт по своему запуску.

Вывод не стримится: транскрипт и есть поток. Claude пишет его по ходу, панель тейлит
файл с оффсета, и запуск, начатый в Telegram, виден в браузере тем же механизмом.

Один запуск на панель. Гонять одну сессию из двух мест одновременно никто не мешает —
проверки на это нет сознательно, два claude в одном транскрипте просто перемешают
записи. Понадобится защита — сравнивать session_id активных запусков в `runner`.
"""

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

from aiohttp import web

import render
import runner
import sessions
import store

log = logging.getLogger("claude_bot.webui")

# id сессии приходит от клиента и подставляется в имя файла. Пропускаем только то,
# чем claude их и называет — uuid: ни слешей, ни точек, ни `..`.
SESSION_RE = re.compile(r"[0-9a-fA-F-]{8,64}\Z")
# id панели генерит браузер, а он становится ключом в `runner._runs` и попадает в логи.
PANE_RE = re.compile(r"[0-9a-zA-Z-]{4,64}\Z")

# Строк за один ответ. Транскрипт бывает на десятки тысяч строк, а страница должна
# отрисоваться сразу — остальное доедет следующими опросами по тому же оффсету.
CHUNK = 3000
# Окно контекста, пока claude не назвал своё: столько у haiku и sonnet, у opus больше.
# Значение временное — после первого же прогона модели в `store` ложится настоящее.
DEFAULT_WINDOW = 200_000
# Столько ждём `session_id` от claude, прежде чем ответить панели «не завелось».
# Первое событие приходит за пару секунд, но на холодном старте бывает дольше.
INIT_TIMEOUT = 90

# Ссылки на фоновые прогоны: без них сборщик мусора вправе убить запуск на середине.
_tasks: set[asyncio.Task] = set()

# Последняя ошибка прогона по скоупу. Без неё упавший запуск выглядел в браузере как
# молчание: индикатор гаснет, в панели ничего, и человек ждёт ответа, которого не будет.
# Текст живёт до следующего запуска в той же панели.
_errors: dict[str, str] = {}


def transcript(project: str, session_id: str) -> Path:
    """Путь к транскрипту по проекту и id, существование не проверяется.

    Оба параметра клиентские. `project` безопасен по построению: `_slug` заменяет
    каждый не-алфанумерик на `-`, так что каталог из него не выйдет. `id` держит
    регулярка — она тут и есть защита, а не наличие файла.

    Отсутствие файла — нормальное состояние, а не ошибка: у новой сессии id уже
    известен из первого события, а транскрипт claude создаёт не мгновенно. Панель в
    этот момент уже опрашивает, и 404 в ответ был бы ложной тревогой.
    """
    if not SESSION_RE.match(session_id):
        raise web.HTTPBadRequest(text="плохой id сессии")
    return sessions.TRANSCRIPTS / sessions._slug(project) / f"{session_id}.jsonl"


# Слеш-команда приезжает в транскрипт вот такой обёрткой, а не текстом человека.
COMMAND_RE = re.compile(
    r"<command-name>\s*(?P<name>[^<]+?)\s*</command-name>"
    r"(?:.*?<command-args>\s*(?P<args>[^<]*?)\s*</command-args>)?",
    re.S)

# Служебные обёртки, которые тоже приезжают user-сообщением, но человек их не писал.
# Список не выдуман: пересчитан по живым транскриптам — task-notification 20 штук,
# local-command-caveat 6, local-command-stdout 5. Неизвестный тег специально оставляем
# текстом человека: лучше показать лишнее, чем спрятать настоящее сообщение.
NOTES = {
    "task-notification": "фоновая задача завершилась",
    "local-command-caveat": "служебная пометка клиента",
    "local-command-stdout": "вывод локальной команды",
}
SERVICE_RE = re.compile(r"\A<([a-z-]{4,40})>")
SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)


def items(path: Path, start: int) -> tuple[int, list[dict]]:
    """Со строки `start`: (номер следующей строки, читаемые элементы).

    Роль берётся из типа события, а не из вида блока. Раньше текстовый блок считался
    ответом claude всегда — и тело скилла, которое приходит `user`-сообщением со
    списком блоков, вываливалось в панель как будто это сказал claude.

    Что показываем:
      * `user` строкой — промпт человека; обёртка слеш-команды сжимается в одну строку;
      * `assistant` с `text` — ответ;
      * `assistant` с `tool_use` — шаг инструмента;
      * `user` со списком блоков — подставленный контекст (тело скилла, вывод
        `/context`, вставленная картинка). Человек это не писал и claude не говорил,
        поэтому вместо содержимого — одна серая пометка с размером. Молча выбрасывать
        нельзя: тогда из панели бесследно исчезало бы то, что реально было в сессии.

    Мысли и `tool_result` не показываем: первые длиннее ответа, вторые бывают на
    мегабайт. Незнакомое событие пропускается молча — типов в транскрипте больше, чем
    нам нужно, и список растёт с версиями claude.
    """
    out: list[dict] = []
    seen = start
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < start:
                continue
            seen = i + 1
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            role = ev.get("type")
            if role not in ("user", "assistant"):
                continue
            content = (ev.get("message") or {}).get("content")

            if isinstance(content, str):
                if text := content.strip():
                    out.append(_prompt(text))
                continue

            if role == "user":
                # Подставленный контекст: показываем факт и объём, не содержимое.
                size = sum(len(b.get("text", "")) for b in content or []
                           if b.get("type") == "text")
                if size:
                    out.append({"role": "note", "text": f"подставлен контекст, {size} симв."})
                continue

            for block in content or []:
                kind = block.get("type")
                if kind == "text":
                    if text := block.get("text", ""):
                        out.append({"role": "assistant", "text": text})
                elif kind == "tool_use":
                    name = block.get("name", "?")
                    arg = render._first_arg(name, block.get("input") or {})
                    out.append({
                        "role": "tool",
                        "icon": render.ICONS.get(name, "🔧"),
                        "name": name,
                        "text": render.clip(arg, 200),
                    })
            if len(out) >= CHUNK:
                break
    return seen, out


def ctx_of(path: Path) -> dict | None:
    """Занятый контекст сессии: `{used, window}` в токенах, либо None.

    Занято — сумма по последнему событию `assistant`: свежий ввод, записанный кэш,
    прочитанный кэш и ответ. Суммировать по всей сессии нельзя — контекст не растёт
    линейно, после `/compact` он падает, и последнее событие единственное честное.

    Размер окна в транскрипт не пишется: его отдаёт `result` в конце прогона, откуда
    `runner` кладёт его в `store` по имени модели. Пока модель ни разу не отвечала в
    этом контейнере, берём 200k и помечаем оценкой.

    ponytail: свой проход по файлу, а рядом такой же делает `items()`. Файл в
    страничном кэше, на десятках мегабайт станет заметно — тогда считать одним
    проходом и отдавать из `items()`.
    """
    used = model = None
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            msg = ev.get("message") or {}
            if ev.get("type") != "assistant" or not (u := msg.get("usage")):
                continue
            used = sum(int(u.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens"))
            model = msg.get("model") or model
    if not used:
        return None
    window = store.get(f"ctxwin:{model}")
    return {"used": used, "window": int(window) if window else DEFAULT_WINDOW,
            "guess": not window}


def _prompt(text: str) -> dict:
    """Строковое `user`-сообщение: промпт человека, слеш-команда или служебная врезка.

    Слеш-команду сжимаем до `/имя аргументы` — в сыром виде это три XML-подобных тега.
    Проверяем её первой: `local-command-caveat` часто идёт преамбулой к настоящей
    команде в том же сообщении, и команда тут главнее.

    Служебные врезки уходят в серую пометку. Иначе уведомление о фоновой задаче стоит
    в панели под подписью «ты», хотя человек не писал ни строки.
    """
    if m := COMMAND_RE.search(text):
        return {"role": "user", "text": f"{m['name']} {m['args'] or ''}".strip()}

    tag = SERVICE_RE.match(text)
    if not tag or not (label := NOTES.get(tag[1])):
        return {"role": "user", "text": text}

    if tag[1] == "task-notification" and (m := SUMMARY_RE.search(text)):
        label += ": " + " ".join(m[1].split())[:160]
    elif tag[1] == "local-command-stdout":
        body = " ".join(re.sub(r"</?local-command-stdout>", " ", text).split())
        if body:
            label += ": " + body[:160]
    return {"role": "note", "text": label}


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


def _int(value: str | None) -> int:
    """`from` из query. Мусор — это ноль, а не 500: панель не должна падать от
    правки адреса руками."""
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


# Каталог для файлов из браузера — тот же, что у файлов из Telegram: бот кладёт их
# сюда же и подставляет путь в промпт. Значение читают и app.py, и этот модуль, поэтому
# живёт в одном месте.
INBOX = Path(os.environ.get("INBOX_DIR", "/data/inbox"))
# Предел на запрос. Больше двадцати пяти мегабайт в промпт всё равно не имеет смысла:
# claude читает файл сам, а место в песочнице не бесконечное.
MAX_UPLOAD = 25 << 20
# Только форма имени: значение уходит в argv через create_subprocess_exec, без шелла,
# поэтому это гигиена, а не защита. Неизвестное имя модели отвергнет сам claude, и
# теперь его текст видно в панели.
MODEL_RE = re.compile(r"[a-zA-Z0-9._-]{2,64}\Z")


# Ниже этого размера цифра в списке — шум: у большинства сессий она одинаково мелкая.
# Выше — предупреждение, что панель будет открываться заметно дольше.
HEAVY = 1 << 20


def _heavy(project: str, session_id: str) -> str:
    """Размер транскрипта, но только если он большой. Пустая строка — не показывать."""
    try:
        size = transcript(project, session_id).stat().st_size
    except (OSError, web.HTTPException):
        return ""
    return f"{size / HEAVY:.1f} МБ" if size >= HEAVY else ""


# Порог в днях. Пол — половина суток: `days=0` снесло бы всё, включая сегодняшнюю
# работу, а «удалить всё» — это не то же самое, что «удалить старое».
MIN_DAYS = 0.5
DEFAULT_DAYS = 2.0


def _days(raw) -> float:
    try:
        return max(MIN_DAYS, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


async def run_purge(older: float) -> dict:
    """Удаление файлов плюс снятие указателей. Одной функцией, потому что вызывают из
    двух мест: кнопка в браузере и /purge в Telegram.

    Файлы сносим в потоке, а `store` трогаем на event loop: соединение sqlite создано
    в главном потоке, и обращение к нему из другого — ProgrammingError.

    Указатели снимаются по списку из отчёта: заново их не найти, транскриптов уже нет.
    Порядок именно такой — если удаление упадёт на середине, лишний указатель
    безобиднее потерянного при живом транскрипте.
    """
    killed = await asyncio.to_thread(sessions.purge, older)
    killed["pointers"] = store.forget_sessions(killed.pop("ids"))
    return killed


def _filename(raw: str | None) -> str:
    """Безопасное имя для файла из браузера.

    Сначала раскодируем, потом отрезаем каталоги: клиент может прислать имя
    percent-кодированным (aiohttp так и делает), и `..%2F..%2Fetc%2Fpasswd` без
    раскодирования осталось бы одним длинным именем — не побег, но и не имя.
    Дальше оставляем только буквы, цифры, точку и дефис.
    """
    name = Path(urllib.parse.unquote(raw or "")).name
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._")
    return name[:80] or "file"


def _project(raw: str) -> str:
    """Проект из запроса. Только то, что реально примонтировано: строка уходит в `cwd`
    процесса claude, и `/etc` тут был бы полноценным рабочим каталогом."""
    if raw in {str(p) for p in sessions.projects()}:
        return raw
    raise web.HTTPBadRequest(text="нет такого проекта")


async def _drive(scope: str, prompt: str, project: str, session_id: str | None,
                 got: asyncio.Future, model: str | None = None) -> None:
    """Довести запуск до конца, ничего не рендеря: вывод claude сам пишет в транскрипт,
    а панель его тейлит. Наружу отдаём только первый session_id — панели нужно знать,
    какой файл читать, особенно когда сессия новая и id придумал claude.
    """
    sid = session_id
    err = ""
    _errors.pop(scope, None)  # новый запуск — прошлая ошибка больше не про него
    try:
        # Модель панели, а иначе глобальная из бота: две панели на разных моделях —
        # ровно то, ради чего делалась параллельность.
        async for ev in runner.run(prompt, project, session_id, model or store.get("model"),
                                   scope=scope):
            if not got.done() and (sid := ev.get("session_id") or sid):
                got.set_result(sid)
            # Внятная причина приходит в `result`, а не в стоп-коде: лимит подписки,
            # отказ модели, недоступный проект — всё это claude пишет в stdout и
            # выходит с rc=1 при пустом stderr. Поэтому текст result важнее кода.
            if ev.get("type") == "result" and ev.get("is_error"):
                err = (ev.get("result") or "").strip()[:2000]
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


def build() -> web.Application:
    async def index(_: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    async def api_peers(_: web.Request) -> web.Response:
        return web.json_response(peers())

    async def api_projects(_: web.Request) -> web.Response:
        return web.json_response(
            [{"name": p.name, "path": str(p)} for p in sessions.projects()]
        )

    async def api_sessions(req: web.Request) -> web.Response:
        # Диск, а не asyncio: заголовок сессии читается из транскрипта целиком, а он
        # бывает на десятки мегабайт — в общем event loop это заморозило бы long-poll.
        project = req.query.get("project", "")
        found = await asyncio.to_thread(sessions.recent, project, 30)
        return web.json_response([
            {"id": sid, "title": title or sid, "ago": sessions.ago(age),
             "size": _heavy(project, sid)}
            for sid, title, age in found
        ])

    async def api_messages(req: web.Request) -> web.Response:
        path = transcript(req.query.get("project", ""), req.query.get("id", ""))
        start = _int(req.query.get("from"))
        if not path.is_file():
            return web.json_response({"next": start, "items": []})
        seen, found = await asyncio.to_thread(items, path, start)
        # Занятость контекста едет с каждым ответом: панель хранит последнее значение,
        # и опрос без новых событий её не гасит.
        ctx = await asyncio.to_thread(ctx_of, path)
        return web.json_response({"next": seen, "items": found, "ctx": ctx})

    async def api_search(req: web.Request) -> web.Response:
        """Поиск по сессиям проекта. Диск в потоке: скан всех транскриптов проекта —
        полсекунды на 45 МБ, но держать на это event loop незачем."""
        found = await asyncio.to_thread(
            sessions.search, req.query.get("project", ""), req.query.get("q", ""), 20)
        return web.json_response([
            {"id": sid, "title": title or sid, "ago": sessions.ago(age), "snippet": snip}
            for sid, title, age, snip in found
        ])

    async def api_purge(req: web.Request) -> web.Response:
        """GET — предпросмотр, POST — удаление. Разными методами не ради красоты:
        удаление необратимо, и промах адресной строкой не должен его запускать."""
        days = _days(req.query.get("days") if req.method == "GET"
                     else (await req.json()).get("days"))
        older = days * 86400
        if req.method == "GET":
            doomed = await asyncio.to_thread(sessions.stale, older)
            return web.json_response({"days": days, "sessions": doomed,
                                      "bytes": sum(r["bytes"] for r in doomed)})

        killed = await run_purge(older)
        log.info("purge: старше %.1f дн, снесено %s", days, killed)
        return web.json_response(killed)

    async def api_upload(req: web.Request) -> web.Response:
        """Файл из браузера — на диск, наружу только путь. Дальше он уходит в промпт
        текстом, как это делает бот с файлами из Telegram: claude читает файл сам, и
        содержимое через нас гонять не надо.

        Размер режет сам aiohttp по client_max_size ниже — до нашего кода такой запрос
        не доходит вовсе.
        """
        data = await req.post()
        field = data.get("file")
        if not hasattr(field, "filename"):
            raise web.HTTPBadRequest(text="нужен файл в поле file")
        name = _filename(field.filename)
        INBOX.mkdir(parents=True, exist_ok=True)
        dest = INBOX / f"{int(time.time())}-{name}"
        await asyncio.to_thread(dest.write_bytes, field.file.read())
        log.info("upload: %s (%d байт)", dest, dest.stat().st_size)
        return web.json_response({"path": str(dest)})

    async def api_status(_: web.Request) -> web.Response:
        """Живые запуски и упавшие прогоны. Запуски берутся из тех же `runner._runs`,
        что у Telegram, и несут id сессии — по нему панель узнаёт свой сеанс, даже если
        его гоняют из топика под другим скоупом."""
        return web.json_response({"runs": runner.active(), "errors": _errors})

    async def api_prompt(req: web.Request) -> web.Response:
        data = await req.json()
        prompt = (data.get("prompt") or "").strip()
        pane = data.get("pane") or ""
        session_id = data.get("session") or None
        if not prompt or not PANE_RE.match(pane):
            raise web.HTTPBadRequest(text="нужны prompt и pane")
        if session_id and not SESSION_RE.match(session_id):
            raise web.HTTPBadRequest(text="плохой id сессии")
        model = (data.get("model") or "").strip() or None
        if model and not MODEL_RE.match(model):
            raise web.HTTPBadRequest(text="плохое имя модели")
        project = _project(data.get("project") or "")

        scope = f"web:{pane}"
        if runner.busy(scope):
            raise web.HTTPConflict(text="панель занята")

        got: asyncio.Future = asyncio.get_running_loop().create_future()
        # Задача живёт дольше запроса: ответ панели — только session_id, а прогон
        # продолжается в фоне и виден ей через транскрипт.
        task = asyncio.create_task(_drive(scope, prompt, project, session_id, got, model))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
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
        return web.json_response({"stopped": await runner.cancel(f"web:{pane}")})

    # client_max_size — предел на тело запроса. По умолчанию у aiohttp мегабайт, и
    # загрузка файла падала бы с 413 раньше нашего кода.
    app = web.Application(client_max_size=MAX_UPLOAD)
    app.add_routes([
        web.get("/", index),
        web.get("/api/peers", api_peers),
        web.get("/api/projects", api_projects),
        web.get("/api/sessions", api_sessions),
        web.get("/api/search", api_search),
        web.get("/api/purge", api_purge),
        web.post("/api/purge", api_purge),
        web.get("/api/messages", api_messages),
        web.get("/api/status", api_status),
        web.post("/api/prompt", api_prompt),
        web.post("/api/upload", api_upload),
        web.post("/api/cancel", api_cancel),
    ])
    return app


async def start(port: int) -> None:
    site = web.AppRunner(build())
    await site.setup()
    await web.TCPSite(site, "0.0.0.0", port).start()
    log.info("webui на :%d", port)


# Строка сырая: в скрипте страницы теперь есть регулярки, и питон иначе съедает их
# обратные слеши — `/\n/g` превратился бы в перевод строки внутри литерала регулярки.
PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude</title>
<style>
:root { color-scheme: dark light }
* { box-sizing: border-box }
/* Кнопки не залиты: фон свой, родительский, выделена только рамка. Иначе серая плашка
   по умолчанию спорила и с тёмной темой, и с оттенком панели.
   `#list button` и `.copy` бьют это правило своей специфичностью — список не набор
   кнопок, а «копировать» лежит поверх кода и обязана быть непрозрачной. */
button, .clip { font:inherit; color:inherit; background:none; cursor:pointer;
  border:1px solid #888a; border-radius:4px; padding:4px 8px }
button:hover, .clip:hover { border-color:#8ad }
body { margin:0; font:14px/1.5 system-ui,sans-serif; display:flex; height:100vh }
aside { width:280px; flex:none; border-right:1px solid #8884; display:flex; flex-direction:column }
#peers { display:flex; gap:2px; padding:8px 8px 0 }
#peers a { flex:1; text-align:center; padding:5px; border:1px solid #8884; border-radius:4px;
  text-decoration:none; color:inherit; font-size:13px }
#peers a[aria-current=page] { background:#8884; font-weight:600 }
aside select, aside button.new, aside input { margin:8px 8px 0; padding:6px }
aside input { background:none; color:inherit; border:1px solid #8884; border-radius:4px;
  font:inherit }
#list .snip { display:block; font-size:11px; opacity:.6; margin-top:2px;
  overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical }
#list { overflow:auto; flex:1; margin-top:8px }
#list button { display:block; width:100%; text-align:left; padding:8px 10px; border:0;
  border-bottom:1px solid #8882; background:none; color:inherit; font:inherit; cursor:pointer }
#list button:hover { background:#8882 }
#list .ago { opacity:.6; font-size:12px }
#list .size { float:right; opacity:.5; font-size:11px }
/* Явные клетки, а не поток: у панели есть колонка и ряд, поэтому её можно тянуть за
   любую сторону, а не только растить вправо-вниз от левого верхнего угла. Перекрытие
   разрешено — это рабочий стол, а не плиточный менеджер; поверх лежит та, которую
   трогали последней. Фон непрозрачный по той же причине.
   Клетка — доля области (1fr), а не пиксели: панели тянутся вместе с окном и держат
   свои пропорции. Числа 12 и 8 дублируют COLS и ROWS в скрипте — вёрстка рисует сетку,
   а скрипт по ней считает шаг перетаскивания, поэтому значения обязаны совпадать. */
#panes { flex:1; overflow:hidden; display:grid; gap:8px; padding:8px;
  grid-template-columns:repeat(12, minmax(0,1fr));
  grid-template-rows:repeat(8, minmax(0,1fr)) }
/* Цвет панели: рамка в полную силу, фон бледной заливкой. Заливка идёт градиентом
   поверх Canvas, а не цветом с альфой: панели перекрываются, и полупрозрачный фон
   просвечивал бы соседнюю. Градиент — верхний слой, Canvas — нижний непрозрачный. */
section { position:relative; display:flex; flex-direction:column; overflow:hidden;
  min-width:0; min-height:0; border-radius:6px;
  border:1px solid oklch(0.62 0.16 var(--hue,250) / .5);
  background:linear-gradient(oklch(0.62 0.16 var(--hue,250) / .07),
                             oklch(0.62 0.16 var(--hue,250) / .07)), Canvas;
  grid-column:var(--c,1) / span var(--w,4); grid-row:var(--r,1) / span var(--h,4) }
/* Активная — та же рамка, но в полную насыщенность, плюс тень. Цвет не единственный
   признак: в заголовке остаются проект и id сессии. */
section.act { z-index:5; box-shadow:0 6px 24px #0005;
  border-color:oklch(0.62 0.20 var(--hue,250)) }
/* touch-action:none — без него Safari и тач-устройства отдают жест прокрутке страницы
   и pointermove до нас не доходит. */
.grip, .h { touch-action:none }   /* иначе жест уходит прокрутке, pointermove не придёт */
.grip { cursor:grab; user-select:none }
.grip.moving { cursor:grabbing }
/* Ручка внутри панели, а не на 3px снаружи: у section стоит overflow:hidden, и он
   обрезает абсолютно позиционированных потомков — снаружи оставалась прозрачная полоска
   в считанные пиксели, по которой было не попасть.
   Уголок виден всегда, а не только у активной панели: невидимую ручку не найти. */
.h { position:absolute; z-index:2 }
.h-se { right:0; bottom:0; width:16px; height:16px; cursor:nwse-resize;
  background:linear-gradient(135deg, transparent 45%, #8887 45%) }
header { position:relative; display:flex; gap:6px; align-items:center; padding:6px 10px;
  border-bottom:1px solid oklch(0.62 0.16 var(--hue,250) / .4);
  background:oklch(0.62 0.18 var(--hue,250) / .30) }
header .who { flex:1; font-size:12px; opacity:.7; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap }
header .dot { width:8px; height:8px; border-radius:50%; flex:none;
  background:oklch(0.62 0.20 var(--hue,250)) }
/* Занятость важнее опознавания: оранжевый перебивает цвет панели. */
header .dot.busy { background:#e90; animation:pulse 1.1s ease-in-out infinite }
@keyframes pulse { 50% { opacity:.25; transform:scale(.7) } }
header .timer { font-size:11px; opacity:.75; font-variant-numeric:tabular-nums; flex:none }

/* Мигает заголовок панели, а не точка 8x8: полоса во всю ширину против 64 пикселей в
   углу — потому точку и не было видно. Насыщенность в ярком кадре умеренная: в
   заголовке лежит текст, и заливка в полную силу его бы утопила. */
section.busy header { animation:blink 1.2s ease-in-out infinite }
@keyframes blink { 50% { background:oklch(0.68 0.21 var(--hue,250) / .70) } }
/* Без движения подсказка обязана остаться: раньше правило просто убирало анимацию, и
   занятость становилась совсем невидимой. Теперь заголовок просто горит ярко. */
@media (prefers-reduced-motion: reduce) {
  header .dot.busy { animation:none }
  section.busy header { animation:none;
    background:oklch(0.68 0.21 var(--hue,250) / .70) }
}
header .hits { font-size:11px; opacity:.6; flex:none }
/* Занятый контекст — полоска в нижней кромке заголовка: места не занимает, а через всю
   сетку видно, какая панель подошла к пределу. Без чисел и подсказки: текст в узкой
   панели вытеснил бы название сессии, а title на полоске в два пикселя недостижим —
   имя сессии растянуто на всю ширину и перекрывает его своим, даже пустым. */
header .ctx { position:absolute; left:0; bottom:0; height:2px; width:0;
  background:oklch(0.62 0.18 var(--hue,250)) }
header .ctx.full { background:#e55 }
header button { padding:1px 6px; line-height:1.2 }
/* Оба цвета заданы явно: подсветка должна читаться и в тёмной теме, и в светлой. */
::highlight(find) { background:#fd0; color:#000 }
.log { flex:1; overflow:auto; padding:12px 14px }
.msg { margin:0 0 12px; overflow-wrap:anywhere }
.user, .tool { white-space:pre-wrap }
/* Своё сообщение залито целиком, а не отмечено полоской: в четырёх панелях глаз ищет
   «где я говорил» первым делом. Полупрозрачный oklch читается и в тёмной теме, и в
   светлой — страница живёт под color-scheme: dark light. */
.user { background:oklch(0.62 0.10 165 / .20); padding:8px 10px; border-radius:6px }
.body p { margin:.5em 0 }
.body > :first-child { margin-top:0 }
.body > :last-child { margin-bottom:0 }
.body h1, .body h2, .body h3, .body h4, .body h5, .body h6 { margin:.6em 0 .3em; font-size:1em }
.body h1, .body h2 { font-size:1.08em }
.body ul, .body ol { margin:.3em 0; padding-left:1.4em }
.body pre { position:relative; margin:.4em 0; padding:8px; overflow:auto;
  background:#8881; border-radius:4px }
/* Кнопка появляется по наведению: в узкой панели постоянная отнимала бы место у кода.
   Прилипает к правому краю самого блока, поэтому не уезжает при его прокрутке. */
.body pre .copy { position:sticky; float:right; top:0; right:0; opacity:0;
  font:inherit; font-size:11px; padding:1px 5px; cursor:pointer; color:inherit;
  background:Canvas; border:1px solid #8884; border-radius:3px }
.body pre:hover .copy, .body pre .copy:focus { opacity:.9 }
.body code { font-family:ui-monospace,monospace; font-size:.92em }
.body :not(pre) > code { background:#8882; padding:.1em .3em; border-radius:3px }
.body table { border-collapse:collapse; margin:.4em 0; font-size:.95em }
.body th, .body td { border:1px solid #8884; padding:2px 6px; text-align:left }
.body a { color:#7ad }
.body blockquote { margin:.4em 0; padding-left:.8em; border-left:3px solid #8884; opacity:.85 }
.assistant { border-left:3px solid #88f; padding-left:10px }
.tool { opacity:.65; font-size:13px; font-family:ui-monospace,monospace }
.note { opacity:.45; font-size:12px; font-style:italic }
.err { color:#e55 }
.role { display:block; font-size:11px; text-transform:uppercase; opacity:.5 }
/* Композер: рамка одна на всё, поле внутри без своей, под ним ряд управления. Раньше
   тут стояли в ряд три рамки разной высоты — селект, скрепка и поле.
   Правый отступ 20px — под ручку .h-se: она лежит в том же углу, и кнопка отправки
   вплотную к краю её бы накрыла. */
form { display:flex; flex-direction:column; gap:4px; margin:8px 20px 8px 8px; padding:6px;
  border:1px solid #8886; border-radius:12px;
  background:oklch(0.62 0.16 var(--hue,250) / .05) }
form:focus-within { border-color:oklch(0.62 0.20 var(--hue,250) / .7) }
textarea { resize:none; min-height:40px; max-height:240px; padding:4px 4px 0;
  font:inherit; background:none; color:inherit; border:0; outline:none }
.bar { display:flex; gap:4px; align-items:center }
/* «Плюс» и модель — призраки: рамка тут уже есть, своя каждой кнопке дробила бы ряд. */
.bar .clip, .bar .model { border:0; background:none; opacity:.65; padding:3px 6px;
  border-radius:8px; font:inherit; font-size:12px; color:inherit; cursor:pointer }
.bar .model { appearance:none; width:auto }
/* Круг под плюсом ровно того же размера, что кнопка отправки напротив. */
.bar .clip { display:grid; place-items:center; width:26px; height:26px; padding:0;
  border-radius:50%; font-size:18px; line-height:1 }
.bar .clip:hover, .bar .model:hover { background:#8882; opacity:1 }
.bar .clip input { display:none }
/* Правый угол ряда: пока запуск идёт, вместо «отправить» стоит «стоп». Переключает
   класс `busy` на секции, его же ставит tick() — своего состояния в JS не нужно. */
.bar .send, .bar .stop { margin-left:auto; width:28px; height:28px; padding:0; flex:none;
  place-items:center; border:0; border-radius:8px; font-size:13px; line-height:1 }
.bar .send { display:grid; background:oklch(0.62 0.16 var(--hue,250)); color:#fff }
.bar .send:hover { background:oklch(0.68 0.20 var(--hue,250)) }
/* Пустое поле — бледная кнопка, без JS: `:placeholder-shown` и есть признак пустоты. */
textarea:placeholder-shown ~ .bar .send { opacity:.35 }
.bar .stop, section.busy .bar .send { display:none }
section.busy .bar .stop { display:grid; background:#e90; color:#000 }
/* Панель под курсором с файлом — заметная рамка, иначе непонятно, куда бросать. */
section.drop { outline:2px dashed oklch(0.68 0.21 var(--hue,250)); outline-offset:-3px }
#empty { grid-column:1/-1; margin:auto; opacity:.5 }
/* Узкий экран: доли области дали бы панель в 30px шириной. Раскладываем столбиком и
   отключаем ручки — тянуть тут всё равно нечего. */
@media (max-width: 700px) {
  #panes { overflow:auto; grid-template-columns:1fr; grid-template-rows:none;
    grid-auto-rows:min(70vh, 480px) }
  section { grid-column:1/-1 !important; grid-row:auto !important }
  .h { display:none }
}
</style></head><body>
<aside>
  <nav id=peers></nav>
  <select id=proj></select>
  <button class=new id=new>+ новая сессия</button>
  <input id=find type=search placeholder="поиск по сессиям проекта">
  <input id=filter type=search placeholder="поиск по открытым панелям">
  <button class=new id=purge title="удалить старые сессии во всех проектах">
    очистить старше 2 дней</button>
  <div id=list></div>
</aside>
<div id=panes><div id=empty>открой сессию слева или начни новую</div></div>
<script>
const $ = (id) => document.getElementById(id);
const get = (u) => fetch(u).then(r => r.ok ? r.json() : Promise.reject(r.status));
const post = (u, body) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body) }).then(r => r.ok ? r.json() : Promise.reject(r.status));
const esc = (s) => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));

// Оттенки для панелей: шесть штук по кругу, светлота и насыщенность заданы в CSS.
// Берём первый незанятый, чтобы соседние панели не совпали по цвету.
// Первые четыре разнесены максимально: синий, красно-оранжевый, зелёный, пурпурный.
// Дальше циан и янтарный. Насыщенность и светлота заданы в CSS одинаковыми для всех,
// поэтому панели отличаются только тоном и выглядят одной семьёй.
const HUES = [250, 25, 145, 305, 195, 60];
const freeHue = () => {
  const used = new Set(panes.map(x => x.hue));
  return HUES.find(h => !used.has(h)) ?? HUES[panes.length % HUES.length];
};

// Панели переживают F5: в них лежит id, который на сервере служит скоупом запуска,
// поэтому после перезагрузки «стоп» бьёт по своему прогону, а не по чужому.
let panes = JSON.parse(localStorage.getItem('panes') || '[]');
const save = () => localStorage.setItem('panes', JSON.stringify(panes));

// Отправленный промпт панель печатает сразу, а через секунды он же приезжает из
// транскрипта. Держим его тут, чтобы снять дубль. Не в самой панели: она уходит в
// localStorage, и после F5 залипшее эхо съело бы строку из истории.
const echoes = new Map();
// Показанная ошибка — чтобы не перерисовывать её на каждом тике.
const shownErr = new Map();

async function loadPeers() {
  let ps; try { ps = await get('api/peers'); } catch (e) { return; }
  if (ps.length < 2) return;
  $('peers').innerHTML = ps.map(p => {
    const here = p.url.includes(location.host) ? ' aria-current=page' : '';
    return `<a href="${esc(p.url)}"${here}>${esc(p.name)}</a>`;
  }).join('');
}

async function loadProjects() {
  const ps = await get('api/projects');
  $('proj').innerHTML = ps.map(p => `<option value="${esc(p.path)}">${esc(p.name)}</option>`).join('');
  if (panes.length) $('proj').value = panes[panes.length - 1].project || ps[0]?.path;
  if (ps.length) loadSessions();
}

function fillList(project, rows, empty) {
  $('list').innerHTML = rows.map(s =>
    `<button data-id="${s.id}" data-title="${esc(s.title)}">` +
    `<span class=ago>${esc(s.ago)}</span> ${esc(s.title.slice(0, 60))}` +
    (s.size ? `<span class=size>${esc(s.size)}</span>` : '') +
    (s.snippet ? `<span class=snip>${esc(s.snippet)}</span>` : '') + '</button>').join('') ||
    `<div style="padding:10px;opacity:.5">${empty}</div>`;
  for (const b of $('list').querySelectorAll('button')) {
    b.onclick = () => addPane({ pane: uid(), project, session: b.dataset.id, next: 0,
                                title: b.dataset.title });
  }
}

async function loadSessions() {
  const project = $('proj').value;
  fillList(project, await get('api/sessions?project=' + encodeURIComponent(project)),
           'сессий нет');
}

// Поиск по сессиям проекта: сервер сканирует транскрипты и отдаёт фрагмент вокруг
// попадания. Дебаунс, потому что скан хоть и быстрый, но не на каждую букву.
let findTimer = null;

function scheduleFind() {
  clearTimeout(findTimer);
  findTimer = setTimeout(runFind, 300);
}

async function runFind() {
  const q = $('find').value.trim();
  const project = $('proj').value;
  if (!q) return loadSessions();
  const url = 'api/search?project=' + encodeURIComponent(project) + '&q=' + encodeURIComponent(q);
  try {
    fillList(project, await get(url), 'ничего не нашлось');
  } catch (e) { /* следующий ввод попробует снова */ }
}

// Поиск по открытым панелям: подсвечиваем найденное, ничего не скрывая — как Ctrl+F.
// Раньше несовпавшие сообщения прятались, то есть контекст исчезал ровно тогда, когда
// он нужнее всего.
//
// Подсветка через CSS Custom Highlight API: диапазоны регистрируются в CSS.highlights,
// DOM не мутируется вообще. Это принципиально — внутри .body лежит готовый HTML
// разметки, и вставка <mark> его бы порвала. В браузере без этого API останется
// счётчик без жёлтого.
//
// Объект Highlight один на всё время жизни страницы, и меняется его содержимое, а не
// запись в реестре: после `CSS.highlights.delete` Safari оставлял жёлтое на экране до
// следующего рефлоу, то есть подсветка переживала очистку поля.
const HL = 'highlights' in CSS ? new Highlight() : null;
if (HL) CSS.highlights.set('find', HL);

// Инлайновые теги текст не разрывают: «функция » и «linkify» из <code> идут подряд и
// склеиваются обратно в одну строку. Всё остальное — граница строки, иначе конец
// одного сообщения слипся бы с началом следующего в несуществующее слово.
const INLINE = new Set(['A', 'B', 'CODE', 'EM', 'I', 'S', 'SPAN', 'STRONG', 'SUB', 'SUP', 'U']);

// Плоский текст панели и карта «смещение → узел». Поиск по каждому узлу отдельно не
// находил ничего, что пересекает границу тега или перевод строки, — а разметка ответа
// режет текст на узлы буквально по каждому <br>, <b> и `коду`.
//
// ponytail: карта строится на каждое нажатие клавиши и на каждую вставку в панель.
// На десятках тысяч строк начнёт подтормаживать — тогда кешировать по узлу .log и
// сбрасывать кеш в poll().
function flatten(root) {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
    // Подписи «ты»/«claude» и кнопка «копировать» — не текст беседы, попадание в них
    // было бы шумом. REJECT на элементе отсекает его вместе с содержимым.
    acceptNode: (n) => n.nodeType === 1 && (n.classList.contains('role') ||
                                            n.classList.contains('copy'))
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  let text = '';
  const map = [];
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (n.nodeType === 1) {
      if (!INLINE.has(n.tagName) && !text.endsWith('\n')) text += '\n';
    } else {
      map.push({ node: n, at: text.length, len: n.data.length });
      text += n.data;
    }
  }
  return { text, map };
}

// Диапазон по смещениям в плоском тексте: начало и конец могут оказаться в разных
// узлах — именно ради этого всё и затевалось. Смещение, попавшее на вставленный
// разделитель строк, прижимается к границе ближайшего узла: на экране его всё равно нет.
function rangeFor(map, from, to) {
  const spot = (off, end) => {
    for (const m of map)
      if (end ? off <= m.at + m.len : off < m.at + m.len)
        return [m.node, clamp(off - m.at, 0, m.len)];
    return null;
  };
  const a = spot(from, false), b = spot(to, true);
  if (!a || !b) return null;
  const r = new Range();
  r.setStart(a[0], a[1]);
  r.setEnd(b[0], b[1]);
  return r;
}

// --- find:begin ---
// Совпадения в плоском тексте. Пробел в запросе матчит любой пробельный кусок, включая
// перевод строки между сообщениями, — иначе фраза, разорванная переносом, не находится.
// Остальное экранируется: в запросе бывают точки, скобки и звёздочки из кода.
function hits(text, q) {
  const pat = q.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+');
  if (!pat) return [];
  const re = new RegExp(pat, 'gi');
  const out = [];
  for (let m = re.exec(text); m; m = re.exec(text)) out.push([m.index, m.index + m[0].length]);
  return out;
}
// --- find:end ---

function applyFilter() {
  const q = $('filter').value.trim();
  HL?.clear();
  for (const p of panes) {
    const el = document.getElementById('pane-' + p.pane);
    if (!el) continue;
    let found = 0;
    if (q) {
      // Ищем только по выводу: в заголовке и в композере искать нечего, а варианты
      // селекта моделей давали ложные попадания.
      const { text, map } = flatten(el.querySelector('.log'));
      for (const [from, to] of hits(text, q)) {
        const r = rangeFor(map, from, to);
        if (r) { HL?.add(r); found++; }
      }
    }
    const box = el.querySelector('.hits');
    if (box) box.textContent = q ? (found || '—') : '';
  }
}

// Сетка фиксированного размера в клетках: 12 на 8. Клетка — доля области, а не пиксели,
// поэтому панели тянутся и сжимаются вместе с окном, сохраняя свои пропорции. Панель по
// умолчанию 6x4, то есть ровно четверть: четыре сессии раскладываются по углам.
const COLS = 12, ROWS = 8, W = COLS / 2, H = ROWS / 2, GAP = 8, PAD = 8;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// Шаг клетки меряем по факту, а не считаем от константы: окно могли изменить, а grid
// пересчитал доли сам. Поэтому перетаскивание точно и до, и после ресайза окна.
const colStep = () => ($('panes').clientWidth - PAD * 2 - GAP * (COLS - 1)) / COLS + GAP;
const rowStep = () => ($('panes').clientHeight - PAD * 2 - GAP * (ROWS - 1)) / ROWS + GAP;

// Прямоугольник обязан лежать внутри сетки: за её краем grid добавил бы неявные ряды,
// и панель уехала бы за границу области.
function fit(p) {
  p.w = clamp(p.w || W, 1, COLS);
  p.h = clamp(p.h || H, 1, ROWS);
  p.c = clamp(p.c || 1, 1, COLS - p.w + 1);
  p.r = clamp(p.r || 1, 1, ROWS - p.h + 1);
}

function applyGeom(p) {
  const el = document.getElementById('pane-' + p.pane);
  if (!el) return;
  for (const k of ['c', 'r', 'w', 'h']) el.style.setProperty('--' + k, p[k]);
}

// Первое свободное место под прямоугольник панели. Без этого две новые панели легли бы
// одна на другую, и рабочий стол начинался бы с разбора завала.
function place(p) {
  const busy = (c, r) => panes.some(x => x !== p && x.c <= c && c < x.c + x.w &&
                                                    x.r <= r && r < x.r + x.h);
  const free = (c, r) => {
    for (let i = 0; i < p.w; i++)
      for (let j = 0; j < p.h; j++)
        if (busy(c + i, r + j)) return false;
    return true;
  };
  for (let r = 1; r <= ROWS - p.h + 1; r++)
    for (let c = 1; c <= COLS - p.w + 1; c++)
      if (free(c, r)) { p.c = c; p.r = r; return; }
  p.c = 1; p.r = 1;  // мест нет — кладём поверх, разберёт человек
}

function raise(el) {
  document.querySelectorAll('#panes section.act').forEach(s => s.classList.remove('act'));
  el.classList.add('act');
}

// Одна механика на перенос и на растягивание: и то и другое меняет прямоугольник панели
// в клетках. `edge` пуст для переноса, иначе содержит буквы сторон, за которые тянут.
// Pointer events, а не HTML5 drag-and-drop: последний в Safari работает через пень-колоду,
// а пальцем не работает вообще.
function wireGrab(p, el, node, edge) {
  node.onpointerdown = (e) => {
    // Кнопки в заголовке («стоп», «×») не должны запускать перенос: preventDefault ниже
    // съел бы их click, и панель стало бы нечем закрыть.
    if (e.button || e.target.closest('button')) return;
    e.preventDefault();
    node.setPointerCapture(e.pointerId);
    raise(el);
    if (!edge) node.classList.add('moving');
    const from = { x: e.clientX, y: e.clientY, c: p.c, r: p.r, w: p.w, h: p.h };
    const step = { x: colStep(), y: rowStep() };

    node.onpointermove = (ev) => {
      const dc = Math.round((ev.clientX - from.x) / step.x);
      const dr = Math.round((ev.clientY - from.y) / step.y);
      if (!edge) {
        p.c = clamp(from.c + dc, 1, COLS - p.w + 1);
        p.r = clamp(from.r + dr, 1, ROWS - p.h + 1);
      } else {
        if (edge.includes('e')) p.w = clamp(from.w + dc, 1, COLS - p.c + 1);
        if (edge.includes('s')) p.h = clamp(from.h + dr, 1, ROWS - p.r + 1);
      }
      applyGeom(p);  // панель переставляется по клеткам сразу, а не после отпускания
    };

    node.onpointerup = node.onpointercancel = () => {
      node.onpointermove = null;
      node.classList.remove('moving');
      save();
    };
  };
}

// Один угол вместо восьми ручек: остальные стороны ловили курсор на пути к тексту и
// к кнопкам, а тянуть панель хватает и правого нижнего угла.
const EDGES = ['se'];

function wireHandles(p, el) {
  wireGrab(p, el, el.querySelector('header'), '');
  for (const edge of EDGES) {
    const h = document.createElement('div');
    h.className = 'h h-' + edge;
    el.append(h);
    wireGrab(p, el, h, edge);
  }
}

function addPane(p) {
  if (p.session && panes.some(x => x.session === p.session)) return;  // уже открыта
  p.w = p.w || W; p.h = p.h || H;
  p.hue = p.hue ?? freeHue();
  panes.push(p);
  if (!p.c) place(p);
  save();
  drawPane(p);
  poll(p);
}

function closePane(p) {
  panes = panes.filter(x => x.pane !== p.pane); save();
  document.getElementById('pane-' + p.pane)?.remove();
  $('empty').hidden = panes.length > 0;
}

function drawPane(p) {
  $('empty').hidden = true;
  const el = document.createElement('section');
  el.id = 'pane-' + p.pane;
  el.innerHTML = `
    <header>
      <span class=dot></span>
      <span class=who></span>
      <span class=timer></span>
      <span class=hits></span>
      <button class=close title="закрыть панель">×</button>
      <i class=ctx></i>
    </header>
    <div class=log></div>
    <form>
      <textarea placeholder="промпт"
        title="Enter — отправить, Shift+Enter — перенос строки"></textarea>
      <div class=bar>
        <label class=clip title="прикрепить файлы">+<input type=file multiple></label>
        <select class=model title="модель этой панели">
          <option value="">модель</option>
          <option>opus</option><option>sonnet</option><option>haiku</option>
        </select>
        <button class=send title="отправить">↑</button>
        <button class=stop type=button title="остановить">■</button>
      </div>
    </form>`;
  $('panes').append(el);
  setWho(p, el);
  el.querySelector('.close').onclick = () => closePane(p);
  el.querySelector('.stop').onclick = () => post('api/cancel', { pane: p.pane }).catch(() => {});
  const form = el.querySelector('form');
  const ta = el.querySelector('textarea');
  // Выбор файлов — тот же путь, что у перетаскивания. `value = ''` нужен, чтобы второй
  // выбор того же файла тоже дал событие.
  const pick = el.querySelector('.clip input');
  pick.onchange = () => { attach(p, ta, pick.files); pick.value = ''; };
  form.onsubmit = (e) => { e.preventDefault(); send(p, ta); };
  // Enter отправляет, перенос строки — с Shift или Alt. Ctrl/Cmd+Enter оставлен: он
  // работал раньше, и пальцы помнят.
  ta.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey) { e.preventDefault(); send(p, ta); }
  };
  ta.oninput = () => grow(ta);

  const model = el.querySelector('.model');
  model.value = p.model || '';
  model.onchange = () => { p.model = model.value; save(); };

  // Файл: перетащить на панель или вставить из буфера. Наружу уходит путь, а не
  // содержимое — claude читает файл сам, ровно как с файлами из Telegram.
  el.ondragover = (e) => { e.preventDefault(); el.classList.add('drop'); };
  el.ondragleave = () => el.classList.remove('drop');
  el.ondrop = (e) => {
    e.preventDefault();
    el.classList.remove('drop');
    attach(p, ta, e.dataTransfer?.files);
  };
  ta.onpaste = (e) => {
    if (e.clipboardData?.files?.length) attach(p, ta, e.clipboardData.files);
  };
  el.style.setProperty('--hue', p.hue ?? HUES[0]);
  el.querySelector('header').classList.add('grip');
  wireHandles(p, el);
  el.onpointerdown = () => raise(el);
  fit(p);
  applyGeom(p);
}

// Заголовок панели: проект и название сессии. Восьми символов id хватало, чтобы
// отличить панели, но не чтобы вспомнить, о чём сессия. Название приходит с сервера в
// списке сессий, а у новой берётся из первого промпта.
function setWho(p, el) {
  const who = (el || document.getElementById('pane-' + p.pane))?.querySelector('.who');
  if (!who) return;
  const label = p.title ? p.title.slice(0, 48)
                        : (p.session ? p.session.slice(0, 8) : 'новая');
  who.textContent = p.project.split('/').pop() + ' · ' + label;
  who.title = p.title || p.session || '';
}

// Поле растёт под текст до потолка в 240px: сбрасываем высоту, чтобы scrollHeight
// пересчитался, и ставим по содержимому. Дальше поле скроллится само.
function grow(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
}

// Кнопка «копировать» у каждого блока кода. Вешаем после вставки и помечаем блок,
// чтобы на следующем опросе не навесить вторую. Текст снимаем до добавления кнопки —
// иначе в буфер попало бы и её собственное слово.
function wireCopy(box) {
  if (!navigator.clipboard) return;  // без HTTPS или в старом браузере кнопки не будет
  for (const pre of box.querySelectorAll('pre:not([data-copy])')) {
    pre.dataset.copy = '1';
    const text = (pre.querySelector('code') || pre).textContent;
    const btn = document.createElement('button');
    btn.className = 'copy';
    btn.type = 'button';
    btn.textContent = 'копировать';
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = 'скопировано';
      } catch (e) {
        btn.textContent = 'не вышло';
      }
      setTimeout(() => { btn.textContent = 'копировать'; }, 1200);
    };
    pre.prepend(btn);
  }
}

// Загрузка файлов по одному: ответ сервера — путь в песочнице, его и дописываем в
// поле ввода. Отдельной строкой, чтобы промпт остался читаемым.
async function attach(p, ta, files) {
  for (const file of files || []) {
    const form = new FormData();
    form.append('file', file);
    try {
      const r = await fetch('api/upload', { method: 'POST', body: form });
      if (!r.ok) throw new Error(await r.text());
      const { path } = await r.json();
      ta.value = (ta.value ? ta.value.replace(/\s*$/, '\n') : '') + path + '\n';
      grow(ta);
      ta.focus();
    } catch (e) {
      log(p, `<div class="msg err">файл не загрузился: ${esc(String(e).slice(0, 200))}</div>`);
    }
  }
}

// Вставка мимо poll(): свой промпт и красные строки. Прокрутка тут обязательна —
// без неё длинный промпт уезжал за нижний край, и poll() дальше считал панель
// «отлистанной вверх» и переставал доводить до низа уже и ответ.
// Полоска контекста. Значение живёт в панели: опрос без новых событий его не присылает,
// а контекст без событий и не меняется.
function setCtx(p, ctx) {
  const bar = document.getElementById('pane-' + p.pane)?.querySelector('.ctx');
  if (!bar || !ctx) return;
  const share = Math.min(1, ctx.used / ctx.window);
  bar.style.width = (share * 100).toFixed(1) + '%';
  bar.classList.toggle('full', share >= 0.9);
}

function log(p, html) {
  const box = document.querySelector('#pane-' + p.pane + ' .log');
  if (!box) return;
  box.insertAdjacentHTML('beforeend', html);
  box.scrollTop = 1e9;
}

async function send(p, ta) {
  const prompt = ta.value.trim();
  if (!prompt) return;
  ta.value = '';
  grow(ta);
  // Момент отправки — единственный жест пользователя, на котором браузер позволяет
  // спросить разрешение. На загрузке страницы Safari и Chrome такой запрос игнорируют.
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
  echoes.set(p.pane, prompt);
  log(p, `<div class="msg user"><span class=role>ты</span>${linkify(esc(prompt))}</div>`);
  try {
    const r = await post('api/prompt', { pane: p.pane, project: p.project,
      session: p.session || null, prompt, model: p.model || null });
    if (!r.session) { log(p, '<div class="msg err">claude не отдал id сессии</div>'); return; }
    if (!p.session) {
      // Новая сессия: id придумал claude, панель дочитывает уже созданный транскрипт.
      // Название берём из промпта — сервер даст своё только при следующем обновлении
      // списка, а подпись нужна сразу.
      p.session = r.session; p.next = 0;
      p.title = p.title || prompt.slice(0, 60);
      save();
      setWho(p);
      loadSessions();
    }
  } catch (code) {
    log(p, `<div class="msg err">не отправилось (${esc(code)})</div>`);
  }
}

// Маркеры md:begin/md:end нужны tests/test_markdown.py: скрипт целиком в node не
// запустить, он с первой строки лезет в document и localStorage. Поэтому маркер стоит
// на своей строке — всё, что после него, уходит в тест как есть.
// --- md:begin ---
// Подмножество markdown своими руками. Библиотеку не тянем: сорок строк регулярок
// закрывают то, чем claude на самом деле пишет, а marked.js — это сорок килобайт в образ
// плюс версия, которую надо обновлять.
//
// Порядок обязателен: сначала вынимаем блоки кода в ящик, потом экранируем, и только
// потом правила разметки. Экранирование ДО вставки своих тегов — единственная тут защита:
// текст пришёл от claude и может содержать что угодно, включая теги скриптов.
// (Слово «script» в угловых скобках тут не пишем: страница режется по нему в тестах.)
//
// Разделитель ящика — символ из приватной зоны Unicode: в обычном тексте его не бывает,
// в отличие от любой печатной пары вроде @@.
//
// ponytail: таблицы только простые, вложенных списков нет. Начнёт калечить вывод —
// вендорить marked.js в образ, а не наращивать регулярки.
const BOX = '\uE000';

function md(src) {
  const box = [];
  const stash = (html) => BOX + (box.push(html) - 1) + BOX;

  let t = String(src).replace(/```[^\n]*\n?([\s\S]*?)```/g,
    (_, body) => stash('<pre><code>' + esc(body.replace(/\n+$/, '')) + '</code></pre>'));
  t = esc(t);

  // Таблица: строка заголовка, строка-разделитель, дальше данные.
  t = t.replace(/^\|(.+)\|[ \t]*\n\|[ \t:|-]+\|[ \t]*\n((?:\|.*\|[ \t]*\n?)*)/gm,
    (_, head, rows) => {
      const cells = (line, tag) => line.split('|').slice(1, -1)
        .map(c => `<${tag}>${inline(c.trim())}</${tag}>`).join('');
      const body = rows.trimEnd().split('\n').filter(Boolean)
        .map(r => `<tr>${cells(r, 'td')}</tr>`).join('');
      return stash(`<table><tr>${cells('|' + head + '|', 'th')}</tr>${body}</table>`);
    });

  t = t.replace(/^(#{1,6}) +(.*)$/gm,
    (_, h, txt) => `<h${h.length}>${inline(txt)}</h${h.length}>`);
  t = t.replace(/^&gt; ?(.*)$/gm, (_, txt) => `<blockquote>${inline(txt)}</blockquote>`);

  // Список — подряд идущие строки одного вида. Вложенность не поддерживается.
  t = t.replace(/(?:^[-*] +.*(?:\n|$))+/gm, (m) => list(m, /^[-*] +/, 'ul'));
  t = t.replace(/(?:^\d+[.)] +.*(?:\n|$))+/gm, (m) => list(m, /^\d+[.)] +/, 'ol'));

  t = inline(t);

  // Одиночный перевод строки — перенос, а текст между блоками заворачивается в <p>.
  // Абзац тут не про отступы: голый текстовый узел рядом с блочным соседом (список,
  // таблица) образует анонимный блок, а WebKit в таком блоке не красит ::highlight —
  // ровно одно попадание поиска оставалось неподсвеченным при верном счётчике.
  // Заодно уходят переносы вокруг блоков: пустая строка между списком и текстом
  // раньше убиралась отдельной парой регулярок, теперь её просто нечему создать.
  t = t.replace(/\n/g, '<br>');
  t = t.split(/(<(?:h[1-6]|ul|ol|blockquote)\b[\s\S]*?<\/(?:h[1-6]|ul|ol|blockquote)>|\uE000\d+\uE000)/)
    .map((part, i) => {
      if (i % 2) return part;                              // сам блок, как есть
      const run = part.replace(/^(?:<br>)+|(?:<br>)+$/g, '');
      return run ? '<p>' + run + '</p>' : '';
    }).join('');

  // Ящик распаковываем последним: внутри него готовый HTML, правила его не касались.
  return t.replace(new RegExp(BOX + '(\\d+)' + BOX, 'g'), (_, i) => box[+i]);
}

function list(block, marker, tag) {
  const li = block.trimEnd().split('\n').filter(Boolean)
    .map(l => `<li>${inline(l.replace(marker, ''))}</li>`).join('');
  return `<${tag}>${li}</${tag}>`;
}

function inline(t) {
  return linkify(t
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<i>$2</i>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>'));
}

// Голый url в тексте тоже ссылка: claude пишет их чаще, чем `[текст](url)`.
// Идёт последним и только после пробела, скобки или начала строки — url внутри уже
// готового `href="..."` или между `>` и `</a>` под это не подпадает и не заворачивается
// повторно. Хвостовая пунктуация в ссылку не входит: точка в конце предложения — точка.
// Вызывается и на тексте без разметки (промпт человека, строка инструмента), поэтому
// принимает уже экранированную строку и ничего больше с ней не делает.
function linkify(t) {
  return t.replace(/(^|[\s(])(https?:\/\/[^\s<]*[^\s<.,;:!?)\]'"])/g,
                   '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}

// --- md:end ---

function renderItem(it) {
  // Подставленный контекст: факт и объём, без содержимого.
  if (it.role === 'note') {
    return `<div class="msg note">↳ ${esc(it.text)}</div>`;
  }
  if (it.role === 'tool') {
    return `<div class="msg tool">${esc(it.icon)} ${esc(it.name)}: ${linkify(esc(it.text))}</div>`;
  }
  // Промпт человека остаётся текстом: он набирал его руками, и случайная звёздочка не
  // должна оказаться курсивом. Разметку рисуем только у ответа.
  if (it.role === 'user') {
    return `<div class="msg user"><span class=role>ты</span>${linkify(esc(it.text))}</div>`;
  }
  return `<div class="msg assistant"><span class=role>claude</span>` +
         `<div class=body>${md(it.text)}</div></div>`;
}

async function poll(p) {
  if (!p.session) return;
  const q = new URLSearchParams({ project: p.project, id: p.session, from: p.next });
  let data; try { data = await get('api/messages?' + q); } catch (e) { return; }
  const first = p.next === 0;
  p.next = data.next; save();
  setCtx(p, data.ctx);
  if (!data.items.length) return;
  const box = document.querySelector('#pane-' + p.pane + ' .log');
  if (!box) return;
  const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  // Свой же промпт, уже напечатанный локально, из транскрипта не берём — иначе он
  // стоит в панели дважды. Снимаем ровно одно совпадение: тот же текст мог быть
  // отправлен и раньше, в истории он законный.
  const echo = echoes.get(p.pane);
  let taken = false;
  const shown = echo
    ? data.items.filter(it => {
        if (!taken && it.role === 'user' && it.text === echo) { taken = true; return false; }
        return true;
      })
    : data.items;
  if (taken) echoes.delete(p.pane);
  box.insertAdjacentHTML('beforeend', shown.map(renderItem).join(''));
  wireCopy(box);
  // Дописанное при активном поиске тоже надо подсветить. Встроенный Ctrl+F на каждой
  // вставке в DOM теряет позицию, а тут диапазоны просто пересобираются.
  if ($('filter').value.trim()) applyFilter();
  if (atEnd || first) box.scrollTop = 1e9;
}

// Сравниваем с последними ответами, а не со всей панелью: тот же текст мог быть в
// истории давно и законно.
function lastAnswerHas(p, text) {
  const msgs = document.querySelectorAll('#pane-' + p.pane + ' .msg.assistant');
  const needle = text.trim().toLowerCase();
  return [...msgs].slice(-3).some(m => m.textContent.toLowerCase().includes(needle));
}

const fmt = (sec) => {
  const s = Math.max(0, Math.round(sec));
  return s < 3600 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
                  : `${Math.floor(s / 3600)}ч ${Math.floor(s % 3600 / 60)}м`;
};

// Сколько шёл запуск на прошлом тике: в момент, когда панель освободилась, сервер уже
// не знает длительности, а в уведомлении она — самое интересное.
const lastElapsed = new Map();

// Уведомление шлём только когда вкладки не видно: на экране пульсирующая рамка и так
// заметна, а дубль поверх неё раздражает. tag сворачивает повторы по одной панели.
function notifyDone(p, seconds) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (!document.hidden) return;
  const where = p.project.split('/').pop();
  new Notification(`claude · ${where}`,
    { body: seconds ? `ответ готов за ${fmt(seconds)}` : 'ответ готов', tag: p.pane });
}

let ticks = 0;

async function tick() {
  let st = { runs: [], errors: {} };
  try { st = await get('api/status'); } catch (e) { /* переживём до следующего тика */ }
  let running = 0;
  for (const p of panes) {
    const scope = 'web:' + p.pane;
    const el = document.getElementById('pane-' + p.pane);
    // Свой запуск — либо начатый этой панелью, либо любой другой над той же сессией:
    // из топика Telegram или из соседней панели. Скоупы у них разные, транскрипт один,
    // и без сопоставления по сессии панель молчала, пока в неё сыпались ответы.
    const mine = (st.runs || []).find(
      (r) => r.scope === scope || (p.session && r.session === p.session));
    const busy = !!mine;
    if (busy) running++;

    el?.classList.toggle('busy', busy);
    el?.querySelector('.dot')?.classList.toggle('busy', busy);
    const timer = el?.querySelector('.timer');
    if (timer) {
      const foreign = busy && mine.scope !== scope;
      timer.textContent = busy ? (foreign ? '↗ ' : '') + fmt(mine.secs) : '';
      timer.title = foreign ? 'запуск начат не из этой панели' : '';
    }

    // Переход «занята → свободна» — единственный момент, когда есть что сообщить.
    if (busy) {
      lastElapsed.set(p.pane, mine.secs);
    } else if (lastElapsed.has(p.pane)) {
      notifyDone(p, lastElapsed.get(p.pane));
      lastElapsed.delete(p.pane);
    }

    // Упавший прогон: показываем текст один раз, до следующего запуска в этой панели.
    const err = (st.errors || {})[scope];
    if (err) {
      if (shownErr.get(p.pane) !== err) {
        shownErr.set(p.pane, err);
        // Причину claude часто пишет и в сам ответ — например про исчерпанный лимит.
        // Тогда красная строка была бы вторым экземпляром того же текста.
        if (!lastAnswerHas(p, err)) log(p, `<div class="msg err">❌ ${esc(err)}</div>`);
      }
    } else {
      shownErr.delete(p.pane);
    }

    await poll(p);
  }
  // Число работающих панелей в заголовке вкладки: видно, даже когда браузер свёрнут.
  document.title = running ? `● ${running} · claude` : 'claude';

  // Сессию могли начать в Telegram или в соседней панели — список слева должен это
  // увидеть сам, а не после перезагрузки страницы.
  if (++ticks % 5 === 0 && !$('find').value.trim()) loadSessions().catch(() => {});
}

// Удаление необратимо, поэтому две ступени: сначала сервер говорит, что уйдёт, и
// только подтверждение запускает. Порог фиксированный — число в кнопке и есть договор.
$('purge').onclick = async () => {
  const days = 2;
  let plan;
  try { plan = await get('api/purge?days=' + days); } catch (e) { return; }
  if (!plan.sessions.length) return alert(`нет сессий старше ${days} дней`);
  const mb = (plan.bytes / 1048576).toFixed(1);
  const head = plan.sessions.slice(0, 12).map(s => `${s.ago} · ${s.title.slice(0, 44)}`);
  const more = plan.sessions.length > 12 ? `\n…и ещё ${plan.sessions.length - 12}` : '';
  if (!confirm(`Удалить безвозвратно ${plan.sessions.length} сессий (${mb} МБ)?\n\n` +
               head.join('\n') + more)) return;
  const killed = await post('api/purge', { days });
  alert(`удалено ${killed.sessions} сессий, ${(killed.bytes / 1048576).toFixed(1)} МБ`);
  loadSessions();
};

$('proj').onchange = () => { $('find').value = ''; loadSessions(); };
$('find').oninput = scheduleFind;
$('filter').oninput = applyFilter;
$('new').onclick = () => addPane({ pane: uid(), project: $('proj').value, session: null, next: 0 });
loadPeers();
loadProjects().then(() => {
  // Панели из localStorage могли получить оттенок из прежней палитры. Переназначаем по
  // одной: freeHue смотрит на уже занятые, поэтому цвета не совпадут.
  panes.forEach(p => { if (!HUES.includes(p.hue)) p.hue = freeHue(); });
  save();
  panes.forEach(p => { p.next = 0; drawPane(p); });
  tick();
});
setInterval(tick, 3000);
</script></body></html>
"""
