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


def items(path: Path, start: int) -> tuple[int, list[dict]]:
    """Со строки `start`: (номер следующей строки, читаемые элементы).

    Оставляем три вида: промпт человека, текст ответа и шаг инструмента. Мысли и
    `tool_result` выброшены намеренно — первые длиннее самого ответа, вторые бывают
    на мегабайт, а в панели нужен разговор, а не сырой поток. Незнакомое событие
    пропускается молча: типов в транскрипте больше, чем нам нужно, и список растёт
    с версиями claude.
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
            if ev.get("type") not in ("user", "assistant"):
                continue
            content = (ev.get("message") or {}).get("content")
            # Промпт человека приходит строкой, всё остальное — списком блоков.
            if isinstance(content, str):
                if content.strip():
                    out.append({"role": "user", "text": content})
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


def _project(raw: str) -> str:
    """Проект из запроса. Только то, что реально примонтировано: строка уходит в `cwd`
    процесса claude, и `/etc` тут был бы полноценным рабочим каталогом."""
    if raw in {str(p) for p in sessions.projects()}:
        return raw
    raise web.HTTPBadRequest(text="нет такого проекта")


async def _drive(scope: str, prompt: str, project: str, session_id: str | None,
                 got: asyncio.Future) -> None:
    """Довести запуск до конца, ничего не рендеря: вывод claude сам пишет в транскрипт,
    а панель его тейлит. Наружу отдаём только первый session_id — панели нужно знать,
    какой файл читать, особенно когда сессия новая и id придумал claude.
    """
    sid = session_id
    err = ""
    _errors.pop(scope, None)  # новый запуск — прошлая ошибка больше не про него
    try:
        async for ev in runner.run(prompt, project, session_id, store.get("model"),
                                   scope=scope):
            if not got.done() and (sid := ev.get("session_id") or sid):
                got.set_result(sid)
            # Внятная причина приходит в `result`, а не в стоп-коде: лимит подписки,
            # отказ модели, недоступный проект — всё это claude пишет в stdout и
            # выходит с rc=1 при пустом stderr. Поэтому текст result важнее кода.
            if ev.get("type") == "result" and ev.get("is_error"):
                err = (ev.get("result") or "").strip()[:2000]
            if ev.get("type") == "_bot" and ev.get("kind") == "error":
                rc_text = f"rc={ev.get('rc')} {(ev.get('text') or '').strip()}".strip()
                err = err or rc_text
                log.warning("scope=%s %s", scope, rc_text[:300])
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
        found = await asyncio.to_thread(sessions.recent, req.query.get("project", ""), 30)
        return web.json_response([
            {"id": sid, "title": title or sid, "ago": sessions.ago(age)}
            for sid, title, age in found
        ])

    async def api_messages(req: web.Request) -> web.Response:
        path = transcript(req.query.get("project", ""), req.query.get("id", ""))
        start = _int(req.query.get("from"))
        if not path.is_file():
            return web.json_response({"next": start, "items": []})
        seen, found = await asyncio.to_thread(items, path, start)
        return web.json_response({"next": seen, "items": found})

    async def api_status(_: web.Request) -> web.Response:
        """Какие панели заняты и что упало. Занятость берётся из тех же `runner._runs`,
        что у Telegram, поэтому веб видит и чужие запуски, а не только свои."""
        return web.json_response({"busy": runner.active(), "errors": _errors})

    async def api_prompt(req: web.Request) -> web.Response:
        data = await req.json()
        prompt = (data.get("prompt") or "").strip()
        pane = data.get("pane") or ""
        session_id = data.get("session") or None
        if not prompt or not PANE_RE.match(pane):
            raise web.HTTPBadRequest(text="нужны prompt и pane")
        if session_id and not SESSION_RE.match(session_id):
            raise web.HTTPBadRequest(text="плохой id сессии")
        project = _project(data.get("project") or "")

        scope = f"web:{pane}"
        if runner.busy(scope):
            raise web.HTTPConflict(text="панель занята")

        got: asyncio.Future = asyncio.get_running_loop().create_future()
        # Задача живёт дольше запроса: ответ панели — только session_id, а прогон
        # продолжается в фоне и виден ей через транскрипт.
        task = asyncio.create_task(_drive(scope, prompt, project, session_id, got))
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

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/peers", api_peers),
        web.get("/api/projects", api_projects),
        web.get("/api/sessions", api_sessions),
        web.get("/api/messages", api_messages),
        web.get("/api/status", api_status),
        web.post("/api/prompt", api_prompt),
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
body { margin:0; font:14px/1.5 system-ui,sans-serif; display:flex; height:100vh }
aside { width:280px; flex:none; border-right:1px solid #8884; display:flex; flex-direction:column }
#peers { display:flex; gap:2px; padding:8px 8px 0 }
#peers a { flex:1; text-align:center; padding:5px; border:1px solid #8884; border-radius:4px;
  text-decoration:none; color:inherit; font-size:13px }
#peers a[aria-current=page] { background:#8884; font-weight:600 }
aside select, aside button.new { margin:8px 8px 0; padding:6px }
#list { overflow:auto; flex:1; margin-top:8px }
#list button { display:block; width:100%; text-align:left; padding:8px 10px; border:0;
  border-bottom:1px solid #8882; background:none; color:inherit; font:inherit; cursor:pointer }
#list button:hover { background:#8882 }
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
section { position:relative; display:flex; flex-direction:column; overflow:hidden;
  min-width:0; min-height:0; border:1px solid #8884; border-radius:6px; background:Canvas;
  grid-column:var(--c,1) / span var(--w,4); grid-row:var(--r,1) / span var(--h,4) }
section.act { z-index:5; box-shadow:0 6px 24px #0005; border-color:#8ab }
/* touch-action:none — без него Safari и тач-устройства отдают жест прокрутке страницы
   и pointermove до нас не доходит. */
.grip, .h { touch-action:none }   /* иначе жест уходит прокрутке, pointermove не придёт */
.grip { cursor:grab; user-select:none }
.grip.moving { cursor:grabbing }
/* Ручки внутри панели, а не на 3px снаружи: у section стоит overflow:hidden, и он
   обрезает абсолютно позиционированных потомков — снаружи оставалась прозрачная полоска
   в считанные пиксели, по которой было не попасть. Перетаскивание работало, потому что
   заголовок — большая непрозрачная область.
   Углы видны всегда, а не только у активной панели: невидимую ручку не найти. */
.h { position:absolute; z-index:2 }
.h:hover { background:#8ab6 }
.h-n { top:0; left:16px; right:16px; height:12px; cursor:ns-resize }
.h-s { bottom:0; left:16px; right:16px; height:12px; cursor:ns-resize }
.h-w { left:0; top:16px; bottom:16px; width:12px; cursor:ew-resize }
.h-e { right:0; top:16px; bottom:16px; width:12px; cursor:ew-resize }
.h-nw { left:0; top:0; width:16px; height:16px; cursor:nwse-resize }
.h-ne { right:0; top:0; width:16px; height:16px; cursor:nesw-resize }
.h-sw { left:0; bottom:0; width:16px; height:16px; cursor:nesw-resize }
.h-se { right:0; bottom:0; width:16px; height:16px; cursor:nwse-resize;
  background:linear-gradient(135deg, transparent 45%, #8887 45%) }
.h-se:hover { background:linear-gradient(135deg, transparent 45%, #8ab 45%) }
/* Отступы по 20px — под угловые ручки: иначе .h-ne накрыл бы кнопку «×», и панель
   стало бы нечем закрыть. Верхние 12px заголовка отданы ручке .h-n, ниже — перенос,
   как у обычного окна. */
header { display:flex; gap:6px; align-items:center; padding:6px 22px 6px 20px;
  border-bottom:1px solid #8884 }
header .who { flex:1; font-size:12px; opacity:.7; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap }
header .dot { width:8px; height:8px; border-radius:50%; background:#8886; flex:none }
header .dot.busy { background:#e90 }
.log { flex:1; overflow:auto; padding:12px 14px }
.msg { margin:0 0 12px; overflow-wrap:anywhere }
.user, .tool { white-space:pre-wrap }
.body > :first-child { margin-top:0 }
.body > :last-child { margin-bottom:0 }
.body h1, .body h2, .body h3, .body h4, .body h5, .body h6 { margin:.6em 0 .3em; font-size:1em }
.body h1, .body h2 { font-size:1.08em }
.body ul, .body ol { margin:.3em 0; padding-left:1.4em }
.body pre { margin:.4em 0; padding:8px; overflow:auto; background:#8881; border-radius:4px }
.body code { font-family:ui-monospace,monospace; font-size:.92em }
.body :not(pre) > code { background:#8882; padding:.1em .3em; border-radius:3px }
.body table { border-collapse:collapse; margin:.4em 0; font-size:.95em }
.body th, .body td { border:1px solid #8884; padding:2px 6px; text-align:left }
.body a { color:#7ad }
.body blockquote { margin:.4em 0; padding-left:.8em; border-left:3px solid #8884; opacity:.85 }
.user { border-left:3px solid #4a9; padding-left:10px }
.assistant { border-left:3px solid #88f; padding-left:10px }
.tool { opacity:.65; font-size:13px; font-family:ui-monospace,monospace }
.err { color:#e55 }
.role { display:block; font-size:11px; text-transform:uppercase; opacity:.5 }
form { display:flex; gap:6px; padding:8px 20px 8px 8px; border-top:1px solid #8884 }
textarea { flex:1; resize:none; height:52px; padding:6px; font:inherit;
  background:none; color:inherit; border:1px solid #8884; border-radius:4px }
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

async function loadSessions() {
  const project = $('proj').value;
  const ss = await get('api/sessions?project=' + encodeURIComponent(project));
  $('list').innerHTML = ss.map(s =>
    `<button data-id="${s.id}"><span style="opacity:.6;font-size:12px">${esc(s.ago)}</span>
     ${esc(s.title.slice(0, 60))}</button>`).join('') ||
    '<div style="padding:10px;opacity:.5">сессий нет</div>';
  for (const b of $('list').querySelectorAll('button')) {
    b.onclick = () => addPane({ pane: uid(), project, session: b.dataset.id, next: 0 });
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
        // Тянем за восточную или южную — двигается только размер. За западную или
        // северную — вместе с размером сдвигается начало, поэтому противоположная
        // сторона остаётся на месте.
        if (edge.includes('e')) p.w = clamp(from.w + dc, 1, COLS - p.c + 1);
        if (edge.includes('s')) p.h = clamp(from.h + dr, 1, ROWS - p.r + 1);
        if (edge.includes('w')) {
          const c = clamp(from.c + dc, 1, from.c + from.w - 1);
          p.w = from.w + (from.c - c);
          p.c = c;
        }
        if (edge.includes('n')) {
          const r = clamp(from.r + dr, 1, from.r + from.h - 1);
          p.h = from.h + (from.r - r);
          p.r = r;
        }
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

const EDGES = ['n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'];

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
      <button class=stop title="остановить">стоп</button>
      <button class=close title="закрыть панель">×</button>
    </header>
    <div class=log></div>
    <form><textarea placeholder="промпт, Ctrl+Enter — отправить"></textarea><button>→</button></form>`;
  $('panes').append(el);
  el.querySelector('.who').textContent =
    p.project.split('/').pop() + (p.session ? ' · ' + p.session.slice(0, 8) : ' · новая');
  el.querySelector('.close').onclick = () => closePane(p);
  el.querySelector('.stop').onclick = () => post('api/cancel', { pane: p.pane }).catch(() => {});
  const form = el.querySelector('form');
  const ta = el.querySelector('textarea');
  form.onsubmit = (e) => { e.preventDefault(); send(p, ta); };
  ta.onkeydown = (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(p, ta); }
  };
  el.querySelector('header').classList.add('grip');
  wireHandles(p, el);
  el.onpointerdown = () => raise(el);
  fit(p);
  applyGeom(p);
}

function log(p, html) {
  document.querySelector('#pane-' + p.pane + ' .log').insertAdjacentHTML('beforeend', html);
}

async function send(p, ta) {
  const prompt = ta.value.trim();
  if (!prompt) return;
  ta.value = '';
  echoes.set(p.pane, prompt);
  log(p, `<div class="msg user"><span class=role>ты</span>${esc(prompt)}</div>`);
  try {
    const r = await post('api/prompt',
      { pane: p.pane, project: p.project, session: p.session || null, prompt });
    if (!r.session) { log(p, '<div class="msg err">claude не отдал id сессии</div>'); return; }
    if (!p.session) {
      // Новая сессия: id придумал claude, панель дочитывает уже созданный транскрипт.
      p.session = r.session; p.next = 0; save();
      document.querySelector('#pane-' + p.pane + ' .who').textContent =
        p.project.split('/').pop() + ' · ' + r.session.slice(0, 8);
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

  // Одиночный перевод строки — перенос. Вокруг блочных тегов переносы убираем, иначе
  // между списком и текстом зияет пустая строка.
  t = t.replace(/\n/g, '<br>')
    .replace(/<br>(?=<(?:h\d|ul|ol|table|blockquote)\b)/g, '')
    .replace(/(<\/(?:h\d|ul|ol|table|blockquote)>)<br>/g, '$1');

  // Ящик распаковываем последним: внутри него готовый HTML, правила его не касались.
  return t.replace(new RegExp(BOX + '(\\d+)' + BOX, 'g'), (_, i) => box[+i]);
}

function list(block, marker, tag) {
  const li = block.trimEnd().split('\n').filter(Boolean)
    .map(l => `<li>${inline(l.replace(marker, ''))}</li>`).join('');
  return `<${tag}>${li}</${tag}>`;
}

function inline(t) {
  return t
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<i>$2</i>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

// --- md:end ---

function renderItem(it) {
  if (it.role === 'tool') {
    return `<div class="msg tool">${esc(it.icon)} ${esc(it.name)}: ${esc(it.text)}</div>`;
  }
  // Промпт человека остаётся текстом: он набирал его руками, и случайная звёздочка не
  // должна оказаться курсивом. Разметку рисуем только у ответа.
  if (it.role === 'user') {
    return `<div class="msg user"><span class=role>ты</span>${esc(it.text)}</div>`;
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
  if (atEnd || first) box.scrollTop = 1e9;
}

let ticks = 0;

async function tick() {
  let st = { busy: [], errors: {} };
  try { st = await get('api/status'); } catch (e) { /* переживём до следующего тика */ }
  for (const p of panes) {
    const scope = 'web:' + p.pane;
    document.querySelector('#pane-' + p.pane + ' .dot')
      ?.classList.toggle('busy', st.busy.includes(scope));

    // Упавший прогон: показываем текст один раз, до следующего запуска в этой панели.
    const err = (st.errors || {})[scope];
    if (err) {
      if (shownErr.get(p.pane) !== err) {
        shownErr.set(p.pane, err);
        log(p, `<div class="msg err">❌ ${esc(err)}</div>`);
      }
    } else {
      shownErr.delete(p.pane);
    }

    await poll(p);
  }
  // Сессию могли начать в Telegram или в соседней панели — список слева должен это
  // увидеть сам, а не после перезагрузки страницы.
  if (++ticks % 5 === 0) loadSessions().catch(() => {});
}

$('proj').onchange = loadSessions;
$('new').onclick = () => addPane({ pane: uid(), project: $('proj').value, session: null, next: 0 });
loadPeers();
loadProjects().then(() => {
  panes.forEach(p => { p.next = 0; drawPane(p); });
  tick();
});
setInterval(tick, 3000);
</script></body></html>
"""
