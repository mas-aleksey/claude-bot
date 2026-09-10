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
    try:
        async for ev in runner.run(prompt, project, session_id, store.get("model"),
                                   scope=scope):
            if not got.done() and (sid := ev.get("session_id") or sid):
                got.set_result(sid)
            if ev.get("type") == "_bot" and ev.get("kind") == "error":
                log.warning("scope=%s rc=%s %s", scope, ev.get("rc"),
                            (ev.get("text") or "")[:300])
    except Exception:
        log.exception("прогон из веба упал: scope=%s", scope)
    finally:
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
        """Какие панели заняты. Источник — те же `runner._runs`, что у Telegram,
        поэтому веб видит и чужие запуски, а не только свои."""
        return web.json_response({"busy": runner.active()})

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


PAGE = """<!doctype html>
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
/* Сетка, а не свободные окна: место панели — целые клетки, поэтому «прилипание»
   получается само, без пиксельной математики и без перекрытий. `dense` подтягивает
   плитки к началу, так что дырок после перетаскивания не остаётся.
   --rowh дублирует ROWH в скрипте: одно значение нужно и вёрстке, и расчёту клетки. */
#panes { flex:1; overflow:auto; display:grid; gap:8px; padding:8px; align-content:start;
  grid-template-columns:repeat(var(--cols,2), minmax(0,1fr));
  grid-auto-rows:var(--rowh,320px); grid-auto-flow:dense }
section { position:relative; display:flex; flex-direction:column; overflow:hidden;
  min-width:0; min-height:0; border:1px solid #8884; border-radius:6px;
  grid-column:span var(--w,1); grid-row:span var(--h,1) }
section.dragging { opacity:.35 }
section.target { outline:2px dashed #8ab; outline-offset:-2px }
.grip { cursor:grab; user-select:none }
.rs { position:absolute; right:0; bottom:0; width:16px; height:16px; cursor:nwse-resize;
  background:linear-gradient(135deg, transparent 50%, #8886 50%) }
header { display:flex; gap:6px; align-items:center; padding:6px 10px; border-bottom:1px solid #8884 }
header .who { flex:1; font-size:12px; opacity:.7; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap }
header .dot { width:8px; height:8px; border-radius:50%; background:#8886; flex:none }
header .dot.busy { background:#e90 }
.log { flex:1; overflow:auto; padding:12px 14px }
.msg { margin:0 0 12px; white-space:pre-wrap; overflow-wrap:anywhere }
.user { border-left:3px solid #4a9; padding-left:10px }
.assistant { border-left:3px solid #88f; padding-left:10px }
.tool { opacity:.65; font-size:13px; font-family:ui-monospace,monospace }
.err { color:#e55 }
.role { display:block; font-size:11px; text-transform:uppercase; opacity:.5 }
form { display:flex; gap:6px; padding:8px; border-top:1px solid #8884 }
textarea { flex:1; resize:none; height:52px; padding:6px; font:inherit;
  background:none; color:inherit; border:1px solid #8884; border-radius:4px }
#empty { grid-column:1/-1; margin:auto; opacity:.5 }
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

// Клетка сетки. COLW — порог, после которого влезает ещё одна колонка; ROWH обязан
// совпадать с --rowh в стилях, иначе расчёт размера при перетаскивании поедет.
const COLW = 420, ROWH = 320, GAP = 8, PAD = 8, MAXCOLS = 4, MAXH = 4;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const cols = () => +($('panes').style.getPropertyValue('--cols') || 2);

function setCols() {
  const w = $('panes').clientWidth - PAD * 2;
  $('panes').style.setProperty('--cols', clamp(Math.floor((w + GAP) / (COLW + GAP)), 1, MAXCOLS));
  panes.forEach(applyGeom);  // панель шире новой сетки — ужать, а не рвать вёрстку
}

function colStep() {
  const c = cols();
  return ($('panes').clientWidth - PAD * 2 - GAP * (c - 1)) / c + GAP;
}

function applyGeom(p) {
  const el = document.getElementById('pane-' + p.pane);
  if (!el) return;
  el.style.setProperty('--w', clamp(p.w || 1, 1, cols()));
  el.style.setProperty('--h', clamp(p.h || 1, 1, MAXH));
}

// Порядок в массиве = порядок в сетке. append переносит существующий узел, слушатели
// и содержимое при этом сохраняются.
function reflow() {
  panes.forEach(p => {
    const el = document.getElementById('pane-' + p.pane);
    if (el) $('panes').append(el);
  });
}

let dragged = null;

function wireDrag(p, el) {
  const head = el.querySelector('header');
  head.draggable = true;
  head.classList.add('grip');
  head.ondragstart = (e) => {
    dragged = p;
    el.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', p.pane);  // без данных Firefox не начнёт перенос
  };
  head.ondragend = () => { dragged = null; el.classList.remove('dragging'); };
  el.ondragover = (e) => {
    if (!dragged || dragged.pane === p.pane) return;
    e.preventDefault();  // без этого drop не случится вовсе
    el.classList.add('target');
  };
  el.ondragleave = () => el.classList.remove('target');
  el.ondrop = (e) => {
    e.preventDefault();
    el.classList.remove('target');
    if (!dragged || dragged.pane === p.pane) return;
    const i = panes.indexOf(dragged), j = panes.indexOf(p);
    if (i < 0 || j < 0) return;
    panes[i] = p; panes[j] = dragged; save();
    reflow();
  };
}

function wireResize(p, el) {
  const grip = document.createElement('div');
  grip.className = 'rs';
  grip.title = 'потянуть — размер по клеткам';
  el.append(grip);
  grip.onpointerdown = (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    const box = el.getBoundingClientRect();
    grip.onpointermove = (ev) => {
      p.w = clamp(Math.round((ev.clientX - box.left + GAP) / colStep()), 1, cols());
      p.h = clamp(Math.round((ev.clientY - box.top + GAP) / (ROWH + GAP)), 1, MAXH);
      applyGeom(p);
    };
    grip.onpointerup = () => { grip.onpointermove = null; save(); };
  };
}

function addPane(p) {
  if (p.session && panes.some(x => x.session === p.session)) return;  // уже открыта
  p.w = p.w || 1; p.h = p.h || 1;
  panes.push(p); save();
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
  wireDrag(p, el);
  wireResize(p, el);
  applyGeom(p);
}

function log(p, html) {
  document.querySelector('#pane-' + p.pane + ' .log').insertAdjacentHTML('beforeend', html);
}

async function send(p, ta) {
  const prompt = ta.value.trim();
  if (!prompt) return;
  ta.value = '';
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

function renderItem(it) {
  if (it.role === 'tool') {
    return `<div class="msg tool">${esc(it.icon)} ${esc(it.name)}: ${esc(it.text)}</div>`;
  }
  const who = it.role === 'user' ? 'ты' : 'claude';
  return `<div class="msg ${it.role}"><span class=role>${who}</span>${esc(it.text)}</div>`;
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
  // При первом опросе панель уже могла нарисовать отправленный промпт сама — он же
  // придёт из транскрипта. Дубль на один экран дешевле, чем сверять тексты.
  box.insertAdjacentHTML('beforeend', data.items.map(renderItem).join(''));
  if (atEnd || first) box.scrollTop = 1e9;
}

async function tick() {
  let busy = [];
  try { busy = (await get('api/status')).busy; } catch (e) { /* переживём */ }
  for (const p of panes) {
    document.querySelector('#pane-' + p.pane + ' .dot')
      ?.classList.toggle('busy', busy.includes('web:' + p.pane));
    await poll(p);
  }
}

$('proj').onchange = loadSessions;
$('new').onclick = () => addPane({ pane: uid(), project: $('proj').value, session: null, next: 0 });
loadPeers();
window.addEventListener('resize', setCols);
setCols();
loadProjects().then(() => {
  panes.forEach(p => { p.next = 0; drawPane(p); });
  setCols();
  tick();
});
setInterval(tick, 3000);
</script></body></html>
"""
