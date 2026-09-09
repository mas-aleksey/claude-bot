"""Читалка транскриптов в браузере: проекты, сессии, история сессии.

Отдельный процесс, а не поток внутри бота, и мимо netns dind. Причина не в нагрузке:
чтобы отдать порт в Traefik, контейнеру нужна сеть `proxy`, а бот сидит в namespace
dind — вместе с демоном docker, который образ `dind` всё равно поднимает на
`0.0.0.0:2375` без TLS (`--host` в compose только добавляется к его собственному,
подавить нельзя). Пустить туда лабораторную сеть значило бы отдать root в песочнице
любому контейнеру из `proxy`. Читалке хватает файлов, поэтому она берёт их томами
`:ro` — и запрет на запись держит docker, а не наши намерения.

Только чтение и по коду: ни одного пути к запуску claude. Промпты остаются в Telegram,
где их гейтит `runner.busy`, и слот запуска с ботом не делится.

Живой прогон дочитывается опросом с оффсетом — claude пишет транскрипт по ходу, и
достаточно отдавать хвост файла с указанной строки. SSE не нужен: нет ни
переподключений, ни второго потребителя событий из `runner`.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiohttp import web

import render
import sessions

log = logging.getLogger("claude_bot.webui")

# id сессии приходит от клиента и подставляется в имя файла. Пропускаем только то,
# чем claude их и называет — uuid: ни слешей, ни точек, ни `..`.
SESSION_RE = re.compile(r"[0-9a-fA-F-]{8,64}\Z")

# Строк за один ответ. Транскрипт бывает на десятки тысяч строк, а страница должна
# отрисоваться сразу — остальное доедет следующими опросами по тому же оффсету.
CHUNK = 3000


def transcript(project: str, session_id: str) -> Path:
    """Файл транскрипта по проекту и id.

    Оба параметра клиентские. `project` безопасен по построению: `_slug` заменяет
    каждый не-алфанумерик на `-`, так что каталог из него не выйдет. `id` проверяем
    сами, потом ещё раз — существованием файла.
    """
    if not SESSION_RE.match(session_id):
        raise web.HTTPBadRequest(text="плохой id сессии")
    path = sessions.TRANSCRIPTS / sessions._slug(project) / f"{session_id}.jsonl"
    if not path.is_file():
        raise web.HTTPNotFound(text="нет такой сессии")
    return path


def items(path: Path, start: int) -> tuple[int, list[dict]]:
    """Со строки `start`: (номер следующей строки, читаемые элементы).

    Оставляем три вида: промпт человека, текст ответа и шаг инструмента. Мысли и
    `tool_result` выброшены намеренно — первые длиннее самого ответа, вторые бывают
    на мегабайт, а в читалке нужен разговор, а не сырой поток. Незнакомое событие
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
    """`from` из query. Мусор — это ноль, а не 500: читалка не должна падать от
    правки адреса руками."""
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


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
        seen, found = await asyncio.to_thread(items, path, _int(req.query.get("from")))
        return web.json_response({"next": seen, "items": found})

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/peers", api_peers),
        web.get("/api/projects", api_projects),
        web.get("/api/sessions", api_sessions),
        web.get("/api/messages", api_messages),
    ])
    return app


async def start(port: int) -> None:
    site = web.AppRunner(build())
    await site.setup()
    await web.TCPSite(site, "0.0.0.0", port).start()
    log.info("webui на :%d", port)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await start(int(os.environ.get("WEB_PORT") or 9317))
    await asyncio.Event().wait()  # сервер живёт в фоне, процессу нужно чем-то держаться


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude — сессии</title>
<style>
:root { color-scheme: dark light }
* { box-sizing: border-box }
body { margin:0; font:14px/1.5 system-ui,sans-serif; display:flex; height:100vh }
aside { width:300px; flex:none; border-right:1px solid #8884; display:flex; flex-direction:column }
aside select { margin:8px; padding:6px }
#list { overflow:auto; flex:1 }
#list button { display:block; width:100%; text-align:left; padding:8px 10px; border:0;
  border-bottom:1px solid #8882; background:none; color:inherit; font:inherit; cursor:pointer }
#list button:hover { background:#8882 }
#list button[aria-current=true] { background:#8884; font-weight:600 }
#list .ago { opacity:.6; font-size:12px }
#peers { display:flex; gap:2px; padding:8px 8px 0 }
#peers a { flex:1; text-align:center; padding:5px; border:1px solid #8884; border-radius:4px;
  text-decoration:none; color:inherit; font-size:13px }
#peers a[aria-current=page] { background:#8884; font-weight:600 }
main { flex:1; overflow:auto; padding:16px 20px }
.msg { margin:0 0 14px; white-space:pre-wrap; overflow-wrap:anywhere }
.user { border-left:3px solid #4a9; padding-left:10px }
.assistant { border-left:3px solid #88f; padding-left:10px }
.tool { opacity:.65; font-size:13px; font-family:ui-monospace,monospace }
.role { display:block; font-size:11px; text-transform:uppercase; opacity:.5 }
#empty { opacity:.5 }
</style></head><body>
<aside>
  <nav id=peers></nav>
  <select id=proj></select>
  <div id=list></div>
</aside>
<main><div id=empty>выбери сессию слева</div><div id=log></div></main>
<script>
const $ = (id) => document.getElementById(id);
let cur = null, next = 0, timer = null;

const get = (url) => fetch(url).then(r => r.ok ? r.json() : Promise.reject(r.status));

async function loadPeers() {
  let ps;
  try { ps = await get('api/peers'); } catch (e) { return; }
  // Одна песочница — вкладка не нужна, она бы только занимала место.
  if (ps.length < 2) return;
  $('peers').innerHTML = ps.map(p => {
    const here = p.url.includes(location.host) ? ' aria-current=page' : '';
    return `<a href="${esc(p.url)}"${here}>${esc(p.name)}</a>`;
  }).join('');
}

async function loadProjects() {
  const ps = await get('api/projects');
  $('proj').innerHTML = ps.map(p => `<option value="${p.path}">${p.name}</option>`).join('');
  if (ps.length) loadSessions();
}

async function loadSessions() {
  const project = $('proj').value;
  const ss = await get('api/sessions?project=' + encodeURIComponent(project));
  $('list').innerHTML = ss.map(s =>
    `<button data-id="${s.id}"><span class=ago>${s.ago}</span> ${s.title.slice(0, 70)}</button>`
  ).join('') || '<div id=empty style=padding:10px>сессий нет</div>';
  for (const b of $('list').children) {
    if (b.tagName === 'BUTTON') b.onclick = () => open(b.dataset.id, b);
  }
}

function open(id, btn) {
  for (const b of $('list').children) b.removeAttribute('aria-current');
  if (btn) btn.setAttribute('aria-current', 'true');
  cur = id; next = 0;
  $('log').innerHTML = ''; $('empty').hidden = true;
  clearInterval(timer);
  poll();
  // Транскрипт пишется по ходу запуска, поэтому хвост дочитываем опросом.
  timer = setInterval(poll, 3000);
}

async function poll() {
  if (!cur) return;
  const q = new URLSearchParams({ project: $('proj').value, id: cur, from: next });
  let data;
  try { data = await get('api/messages?' + q); } catch (e) { return; }
  next = data.next;
  if (!data.items.length) return;
  const atEnd = Math.abs(window.scrollY) < 1 ||
    document.querySelector('main').scrollTop + window.innerHeight >=
    document.querySelector('main').scrollHeight - 40;
  $('log').insertAdjacentHTML('beforeend', data.items.map(render).join(''));
  if (atEnd) document.querySelector('main').scrollTop = 1e9;
}

const esc = (s) => s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

function render(it) {
  if (it.role === 'tool') {
    return `<div class="msg tool">${it.icon} ${esc(it.name)}: ${esc(it.text)}</div>`;
  }
  const who = it.role === 'user' ? 'ты' : 'claude';
  return `<div class="msg ${it.role}"><span class=role>${who}</span>${esc(it.text)}</div>`;
}

$('proj').onchange = () => { cur = null; clearInterval(timer); $('log').innerHTML = '';
  $('empty').hidden = false; loadSessions(); };
loadPeers();
loadProjects();
</script></body></html>
"""


if __name__ == "__main__":
    asyncio.run(main())
