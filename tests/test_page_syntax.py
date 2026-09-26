"""Страница целиком лежит в строке PAGE, и опечатка в её JS не видна ни ruff, ни тестам:
образ соберётся, endpoint ответит 200, а панели просто не появятся. Ловим синтаксис
через node — он есть в базовом образе, потому что на нём работает claude-cli.
"""

import json
import re
import shutil
import subprocess

import pytest

import webui

# Обвязка вокруг блока `dead`: страница целиком в node не поедет, а этому куску нужны
# только плашка, заголовок вкладки и подставной `fetch` с тремя ответами — данные,
# страница входа и обрыв связи.
HARNESS = """
let banner = true;
const $ = () => ({ set hidden(v) { banner = v; } });
// Имя инстанса в заголовок вписывает сервер, скрипт только читает его при старте и
// возвращает на место, когда дописывает к нему значок обрыва.
const document = { title: 'demo' };
let answer = 'fail';
const reply = (type) => ({ ok: true, headers: { get: () => type }, json: () => 42 });
const fetch = () => answer === 'ok' ? Promise.resolve(reply('application/json'))
            : answer === 'login' ? Promise.resolve(reply('text/html; charset=utf-8'))
                                 : Promise.reject(new TypeError('failed to fetch'));
"""


def slice_out(tag: str) -> str:
    return webui.PAGE.split(f"<{tag}>")[1].split(f"</{tag}>")[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_page_script_parses(tmp_path):
    js = tmp_path / "page.js"
    js.write_text(slice_out("script"), encoding="utf-8")
    done = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_grid_size_matches_between_css_and_script():
    """Сетку рисует CSS, а шаг перетаскивания считает скрипт по своим COLS и ROWS.
    Разъедутся — панель будет прыгать не туда, и заметить это можно только мышью."""
    cols, rows = re.search(r"COLS = (\d+), ROWS = (\d+)", slice_out("script")).groups()
    css = slice_out("style")
    assert f"grid-template-columns:repeat({cols}," in css
    assert f"grid-template-rows:repeat({rows}," in css


def test_layout_reset_runs_before_panes_are_read():
    """`?reset` — единственный выход, когда раскладка испортила вид и до кнопок уже не
    добраться. Стирать её надо до разбора `panes`: ниже сброс опоздал бы и молча не
    сделал ничего."""
    js = slice_out("script")
    assert js.index("removeItem('panes')") < js.index("let panes = JSON.parse")


def test_page_has_both_halves():
    """Разбор по тегам молча отдал бы пустую строку, и проверка выше стала бы холостой."""
    assert "function drawPane" in slice_out("script")
    assert "grid-column:var(--c,1)" in slice_out("style")


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_polling_stops_after_a_run_of_failures(tmp_path):
    """Вкладка без сессии SSO молотила вечно: каждый запрос — редирект на вход и новая
    кука состояния там. Серия отказов обязана останавливать опрос, а один отказ при
    живом сервере — нет."""
    body = slice_out("script").split("// --- dead:begin ---")[1].split("// --- dead:end ---")[0]
    js = tmp_path / "dead.js"
    js.write_text(HARNESS + body + """
const hit = async (mode) => { answer = mode; await get('x').catch(() => {}); };

(async () => {
  for (let i = 0; i < DEAD - 1; i++) await hit('fail');
  const beforeLimit = dead;         // серия ещё не добрана — опрос жив
  await hit('ok');                  // успех сбрасывает серию
  for (let i = 0; i < DEAD - 1; i++) await hit('fail');
  const afterReset = dead;          // значит до предела снова не хватает одного
  for (let i = 0; i < 1; i++) await hit('fail');
  console.log(JSON.stringify([beforeLimit, afterReset, dead, banner, document.title]));
})();
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [False, False, True, False, "⚠ demo"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_login_page_instead_of_json_stops_the_tab_at_once(tmp_path):
    """Протухшая кука — это не обрыв связи: forwardAuth отвечает редиректом, `fetch`
    проходит его молча и отдаёт 200 со страницей входа. Раньше это считалось успехом,
    страница оставалась белой и не звала человека войти. Ждать серии тут нечего."""
    body = slice_out("script").split("// --- dead:begin ---")[1].split("// --- dead:end ---")[0]
    js = tmp_path / "login.js"
    js.write_text(HARNESS + body + """
(async () => {
  answer = 'login';
  await get('x').catch(() => {});   // одного ответа достаточно
  console.log(JSON.stringify([dead, banner, document.title]));
})();
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [True, False, "⚠ demo"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_every_new_pane_retiles_the_whole_grid(tmp_path):
    """Новое окно перекладывает все. Проверяем два обещания сразу: перекрытий нет ни на
    одном шаге, и сетка занята целиком — пустот внизу и справа не остаётся."""
    body = slice_out("script").split("// --- place:begin ---")[1].split("// --- place:end ---")[0]
    js = tmp_path / "place.js"
    js.write_text("""
const COLS = 12, ROWS = 8, W = COLS / 2, H = ROWS / 2;
const applyGeom = () => {};
const drawZoom = () => {};
// Широкий монитор: раскладка считает пропорцию окна по нему, а не по числу окон.
const $ = () => ({ clientWidth: 2400, clientHeight: 1200 });
let panes = [];
""" + body + """
const overlap = () => panes.some((a, i) => panes.slice(i + 1).some(b =>
  a.c < b.c + b.w && b.c < a.c + a.w && a.r < b.r + b.h && b.r < a.r + a.h));
const outside = () => panes.some(p =>
  p.c < 1 || p.r < 1 || p.c + p.w - 1 > COLS || p.r + p.h - 1 > ROWS);
const area = () => panes.reduce((n, p) => n + p.w * p.h, 0);
const seen = [];
for (let n = 1; n <= 9; n++) {
  panes.push({});
  retile();
  seen.push([overlap(), outside(), area()]);
}
// Разворот и возврат: прямоугольник обязан вернуться ровно тем же.
const count = panes.length, w0 = panes[0].w, h0 = panes[0].h;
const first = panes[0];
const was = [first.c, first.r, first.w, first.h];
zoom(first);
const big = [first.c, first.r, first.w, first.h];
zoom(first);
const back = [first.c, first.r, first.w, first.h];
panes = Array.from({ length: 3 }, () => ({}));
retile();
const three = panes.map(p => [p.c, p.r, p.w, p.h]);
// Ряды шести окон: по сколько в каждом. Раскладка обязана быть ровной.
panes = Array.from({ length: 6 }, () => ({}));
retile();
const rows = [...new Set(panes.map(p => p.r))].sort((a, b) => a - b)
  .map(r => panes.filter(p => p.r === r).length);
console.log(JSON.stringify([seen, count, was, big, back, three, rows]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    seen, count, was, big, back, three, rows = json.loads(done.stdout)

    # На каждом шаге от одного окна до девяти: без перекрытий, без выхода за сетку и
    # ровно 96 занятых клеток. Семь окон и были жалобой — прежняя раскладка теряла
    # остаток от деления и оставляла внизу две пустые полосы.
    assert seen == [[False, False, 96]] * 9
    assert count == 9
    assert big == [1, 1, 12, 8]           # развёрнутое занимает всю область
    assert back == was                    # и возвращается ровно откуда развернули
    # Три окна на широком экране — три колонки во всю высоту, а не два сверху и одно снизу.
    assert three == [[1, 1, 4, 8], [5, 1, 4, 8], [9, 1, 4, 8]]
    # Шесть — ровно 3 + 3. Раскладка по средней клетке давала 4 + 2: четыре правильных
    # окна перевешивали два растянутых, и ряды выходили разной формы.
    assert rows == [3, 3]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_own_prompt_is_shown_once(tmp_path):
    """Свой промпт печатается сразу и через секунды приезжает из транскрипта. Сверка
    идёт по всей очереди и по схлопнутым пробелам: слеш-команду claude пересобирает из
    тегов, а застрявшая запись раньше глушила сверку для всех следующих промптов."""
    body = slice_out("script").split("// --- echo:begin ---")[1].split("// --- echo:end ---")[0]
    js = tmp_path / "echo.js"
    js.write_text(body + """
const q1 = ['/refine текст'];
const same = dropEcho([{ role: 'user', text: '/refine  текст' }], q1);

// Застрявшее эхо (промпт не доехал до транскрипта) не должно глушить следующий.
const q2 = ['застряло', 'новый промпт'];
const after = dropEcho([{ role: 'user', text: 'новый промпт' }], q2);

// Чужая строка и ответ claude проходят как есть, очередь не трогают.
const q3 = ['моё'];
const rest = dropEcho([{ role: 'assistant', text: 'моё' },
                       { role: 'user', text: 'из телеграма' }], q3);
console.log(JSON.stringify([same.length, q1, after.length, q2, rest.length, q3]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    same, q1, after, q2, rest, q3 = json.loads(done.stdout)

    assert [same, q1] == [0, []]                  # лишний пробел совпадению не мешает
    assert [after, q2] == [0, ["застряло"]]       # снят свой, застрявшее осталось лежать
    assert [rest, q3] == [2, ["моё"]]             # ответ и чужой промпт не съедены


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_sidebar_start_state(tmp_path):
    """Первый заход решается шириной экрана, дальше — сохранённым выбором. Ловушка тут
    в строке '0': она истинна, и проверка на истинность прятала бы открытый сайдбар."""
    body = slice_out("script").split("// --- fold:begin ---")[1].split("// --- fold:end ---")[0]
    js = tmp_path / "fold.js"
    js.write_text(body + """
console.log(JSON.stringify([
  foldedAtStart(null, true),    // первый заход с телефона — прячем
  foldedAtStart(null, false),   // первый заход с большого экрана — показываем
  foldedAtStart('0', true),     // человек открыл его сам, узкий экран не спорит
  foldedAtStart('1', false),
]));""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [True, False, False, True]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_list_marks_follow_runs_not_panes(tmp_path):
    """Три состояния строки складываются из разных источников: заливка от панели,
    мигание от живого запуска, точка от запуска, кончившегося без окна. Проверяем весь
    путь: запуск при закрытом окне, ответ в пустоту, открытие, забывание цвета."""
    body = slice_out("script").split("// --- mark:begin ---")[1].split("// --- mark:end ---")[0]
    js = tmp_path / "mark.js"
    js.write_text("""
function btn(id) {
  const cls = new Set(), vars = {};
  return { dataset: { id }, cls, vars, title: '',
    classList: { toggle: (k, on) => { on ? cls.add(k) : cls.delete(k); } },
    style: { setProperty: (k, v) => { vars[k] = v; },
             removeProperty: (k) => { delete vars[k]; } } };
}
const rows = [btn('a'), btn('b')];
rows[0].dataset.title = 'про сетку';
const document = {
  querySelectorAll: () => rows,
  querySelector: (sel) => rows.find(r => sel.includes('"' + r.dataset.id + '"')) ?? null,
};
const sent = [];
function Notification(title, opts) { sent.push([title, opts.body, opts.tag]); }
Notification.permission = 'granted';
const window = { Notification };
const store = {};
const localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
const HUES = [250];
const freeHue = () => 25;
let panes = [];
// syncTitles живёт в том же блоке и трогает панель — заглушки, чтобы блок исполнился.
const setWho = () => {};
const save = () => {};
""" + body + """
const state = () => rows.map(r => [[...r.cls].sort(), r.vars['--hue'] ?? null]);
const seen = [];

trackRuns([{ session: 'a' }]);            // запуск при закрытом окне
markList(); seen.push(state());

trackRuns([]);                            // кончился, окна так и не было
markList(); seen.push(state());
seen.push(JSON.parse(store.done));        // метка легла в localStorage

panes = [{ session: 'a', hue: 25 }];      // открыли сессию
done.delete('a');
markList(); seen.push(state());

panes = [{ session: 'a', hue: 25 }];      // прогон при открытом окне
trackRuns([{ session: 'a' }]);
trackRuns([]);                            // кончился на глазах — ни точки, ни звонка
markList(); seen.push(state());

panes = [];                               // закрыли, ничего не идёт
trackRuns([]);
markList(); seen.push(state());
console.log(JSON.stringify([...seen, sent]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    running, finished, stored, opened, watched, forgotten, sent = json.loads(done.stdout)
    assert running == [[["busy"], 25], [[], None]]      # мигает, но не залита
    assert finished == [[["done"], 25], [[], None]]     # точка, цвет тот же
    assert stored == ["a"]                              # переживёт F5
    assert opened == [[["open"], 25], [[], None]]       # заливка, точка снята
    assert watched == [[["open"], 25], [[], None]]      # смотрели сами — точки нет
    assert forgotten == [[[], None], [[], None]]        # цвет забыт, карта не растёт
    # звонок ровно один: про закрытое окно, с названием сессии из строки списка
    assert sent == [["claude · ответ готов", "про сетку", "a"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_pane_title_follows_the_list(tmp_path):
    """Имя сессии на сервере — последний промпт, поэтому оно меняется по ходу разговора.
    Заголовок панели рисовался один раз при открытии и отставал навсегда."""
    body = slice_out("script").split("// --- mark:begin ---")[1].split("// --- mark:end ---")[0]
    js = tmp_path / "titles.js"
    js.write_text("""
const row = (id, title) => ({ dataset: { id, title }, classList: { toggle: () => {} },
  style: { setProperty: () => {}, removeProperty: () => {} }, title: '' });
const rows = [row('a', 'новое имя'), row('b', ''), row('c', 'чужая сессия')];
const document = { querySelectorAll: () => rows };
const store = {};
const localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
const HUES = [250];
const drawn = [];
const setWho = (p) => drawn.push(p.session);
let saves = 0;
const save = () => { saves++; };
let panes = [{ session: 'a', title: 'старое имя' }, { session: 'b', title: 'было' }];
""" + body + """
syncTitles();
const first = [panes.map(p => p.title), drawn.slice(), saves];
syncTitles();                       // второй проход — менять нечего
console.log(JSON.stringify([first, drawn.length, saves]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    (titles, drawn, saves), drawn_after, saves_after = json.loads(done.stdout)
    assert titles == ["новое имя", "было"]   # пустое имя из списка не затирает своё
    assert drawn == ["a"] and saves == 1     # перерисована одна панель, запись одна
    assert drawn_after == 1 and saves_after == 1  # повтор не трогает ни панель, ни диск


def test_sidebars_fold_independently():
    """Правило пишется на голый `aside`, а их теперь два: без `:not(.files)` полоска
    слева прятала бы заодно и дерево файлов справа."""
    css = slice_out("style")
    assert "body.folded aside:not(.files)" in css
    assert "body.rfolded aside.files { display:none }" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_tool_steps_fold_into_one_group(tmp_path):
    """Шаги инструментов обязаны складываться в одну группу, а ответ — её закрывать.
    Иначе панель снова превращается в ленту из полусотни строк, где ответ внизу."""
    body = slice_out("script").split("// --- tools:begin ---")[1].split("// --- tools:end ---")[0]
    js = tmp_path / "tools.js"
    js.write_text("""
// Минимальный узел: класс берём из разметки регуляркой, больше pour() ничего не трогает.
const node = (html) => ({
  html, kids: html.startsWith('<details') ? [node('summary')] : [],
  classList: { contains: (c) => (html.match(/class="([^"]*)"/)?.[1] || '').split(' ').includes(c) },
  get children() { return this.kids; },
  get lastElementChild() { return this.kids[this.kids.length - 1] || null; },
  get firstElementChild() { return this.kids[0] || null; },
  insertAdjacentHTML(_, h) { this.kids.push(node(h)); },
});
const renderItem = (it) => `<div class="${it.role}">${it.text}</div>`;
const toolHead = (it) => it.text;
""" + body + """
const box = node('<div>');
pour(box, [
  { role: 'user', text: 'сделай' },
  { role: 'tool', text: 'Read a' }, { role: 'tool', text: 'Bash b' },
  { role: 'assistant', text: 'готово' },
  { role: 'tool', text: 'Read c' },
]);
console.log(JSON.stringify([
  box.kids.map(k => k.classList.contains('tools') ? 'group' : k.html),
  box.kids[1].kids.length,
  box.kids[1].firstElementChild.innerHTML,
]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    kids, size, head = json.loads(done.stdout)
    # Промпт, группа из двух шагов, ответ, новая группа: ответ группу закрыл.
    assert kids == ['<div class="user">сделай</div>', "group",
                    '<div class="assistant">готово</div>', "group"]
    assert size == 3  # summary + два шага
    assert head == "2 · Bash b"  # счётчик и последний вызов


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_crumbs_survive_a_symlink_into_another_root(tmp_path):
    """Сервер отвечает путём после `resolve()`, и скилл из `/root/.claude/skills`
    оказывается в `/opt/skills`. Раньше такой путь не совпадал с выбранным корнем, и
    от крошек оставалась одна кнопка — жалоба «крошки иногда пропадают».

    Заодно проверяем `/data` против `/database`: сравнение по префиксу без слеша
    выбрало бы не тот корень.
    """
    body = webui.PAGE.split("// --- files:begin ---")[1].split("// --- files:end ---")[0]
    drawCrumb = body.split("function drawCrumb")[1].split("\nfunction drawTree")[0]
    js = tmp_path / "crumb.js"
    js.write_text("""
let ROOTS = [], picked = '', html = '';
const store = {};
const localStorage = { setItem: (k, v) => { store[k] = v; } };
const esc = (s) => String(s);
const crumb = {
  set innerHTML(v) { html = v; },
  querySelectorAll: () => [],
};
const $ = (id) => id === 'crumb' ? crumb : {
  get value() { return picked; },
  set value(v) { picked = v; },
  options: ROOTS.map(v => ({ value: v })),
};
const openDir = () => Promise.resolve();
const treeFail = () => {};

function drawCrumb""" + drawCrumb + """
const names = () => html.match(/>([^<]+)</g).map(s => s.slice(1, -1));

ROOTS = ['/root/.claude', '/opt/skills'];
picked = '/root/.claude';
drawCrumb('/opt/skills/10-base/end');
const symlink = [names(), picked];

ROOTS = ['/data', '/database'];
picked = '/data';
drawCrumb('/database/x');
const prefix = [names(), picked];

ROOTS = ['/projects/demo'];
picked = '/projects/demo';
drawCrumb('/projects/demo/src/app.py');
const plain = names();

console.log(JSON.stringify({ symlink, prefix, plain }));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    # Симлинк: крошки ведут по настоящему корню, выпадашка идёт следом за ними.
    assert got["symlink"] == [["skills", " / ", "10-base", " / ", "end"], "/opt/skills"]
    # `/database` не считается лежащим в `/data`.
    assert got["prefix"] == [["database", " / ", "x"], "/database"]
    # Обычный случай не изменился.
    assert got["plain"] == ["demo", " / ", "src", " / ", "app.py"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_upload_reports_progress_and_failures(tmp_path):
    """Ход отправки берётся из XHR: `fetch` его не отдаёт вовсе, и на мобильной сети
    большой файл уходил в тишину. Проверяем три исхода — проценты, успех и отказ."""
    body = webui.PAGE.split("function upload")[1].split("\nasync function attach")[0]
    js = tmp_path / "upload.js"
    js.write_text("""
let sent = null, mode = 'ok';
class FormData { append(k, v) { this.k = k; this.v = v; } }
class XMLHttpRequest {
  constructor() { this.upload = {}; this.status = 0; this.responseText = ''; }
  open(method, url) { this.url = url; }
  send(form) {
    sent = form;
    this.upload.onprogress({ lengthComputable: true, loaded: 5, total: 10 });
    this.upload.onprogress({ lengthComputable: false });
    if (mode === 'boom') return this.onerror();
    this.status = mode === 'ok' ? 200 : 400;
    this.responseText = mode === 'ok' ? '{"path":"/data/inbox/1-a.png"}' : 'нужен файл в поле file';
    this.onload();
  }
}

function upload""" + body + """
(async () => {
  const seen = [];
  const out = { ok: null, bad: null, dead: null };

  out.ok = (await upload({ name: 'a.png' }, (v) => seen.push(v))).path;
  mode = 'fail';
  out.bad = await upload({}, () => {}).catch(e => e.message);
  mode = 'boom';
  out.dead = await upload({}, () => {}).catch(e => e.message);

  console.log(JSON.stringify({ ...out, seen, field: sent.k }));
})();
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    assert got["ok"] == "/data/inbox/1-a.png"
    assert got["bad"] == "нужен файл в поле file"   # текст сервера, а не голый код
    assert got["dead"] == "обрыв связи"
    # Половина отправлена, дальше длина неизвестна — это не ноль, а «процента нет».
    assert got["seen"] == [0.5, None]
    assert got["field"] == "file"                   # имя поля то же, что ждёт api_upload


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_sidebar_tree_shows_all_projects_and_hides_closed_ones(tmp_path):
    """Сайдбар — дерево: все проекты видны сразу, сессии показывает только раскрытый.
    Выпадашка стояла тут до 2026-09-25 и прятала всё, кроме выбранного проекта."""
    body = slice_out("script").split("// --- tree:begin ---")[1].split("// --- tree:end ---")[0]
    js = tmp_path / "tree.js"
    js.write_text("""
const box = {};
let html = '';
const localStorage = { getItem: (k) => box[k] ?? null, setItem: (k, v) => { box[k] = v; } };
const $ = () => ({ set innerHTML(v) { html = v; }, querySelectorAll: () => [] });
const esc = (s) => String(s);
const wireRows = () => {};
const get = async () => [];
""" + body + """
// Списки приходят только у раскрытых: у свёрнутого их в TREE нет вовсе, и число сессий
// в его строке берётся из ответа `api/projects`.
TREE = {
  projects: [{ path: '/projects/a', name: 'a', sessions: 1 },
             { path: '/projects/b', name: 'b', sessions: 7 }],
  lists: { '/projects/a': [{ id: 's1', title: 'про докер', ago: '2ч' }] },
};
open.add('/projects/a');
drawProjects();
console.log(JSON.stringify([
  html.includes('data-path="/projects/a"'),
  html.includes('data-path="/projects/b"'),
  html.includes('data-project="/projects/a"'),
  html.includes('data-project="/projects/b"'),
  (html.match(/class=add/g) || []).length,
  box.open,
  html.includes('>7<'),
]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    a_head, b_head, a_row, b_row, adds, saved, count_b = json.loads(done.stdout)

    assert a_head and b_head          # оба проекта в дереве, переключать нечего
    assert a_row                      # сессия раскрытого проекта несёт свой путь
    assert not b_row                  # свёрнутый проект своих сессий не рисует
    assert count_b                    # но число сессий у него есть — счёт с сервера
    assert adds == 2                  # «+» в каждой строке проекта
    assert saved is None              # drawProjects только рисует, состояние пишет клик


def test_no_two_functions_share_a_name():
    """Второе объявление молча перекрывает первое, и падает не оно, а вызывающий. Так
    2026-09-25 легла вся панель: дерево проектов назвали `drawTree`, а это уже имя
    рендера дерева файлов — сайдбар позвал чужую функцию без аргумента, исключение убило
    запуск целиком, и пустыми остались и список, и окна."""
    names = re.findall(r"^function (\w+)", slice_out("script"), re.M)
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, dupes


def test_tab_title_always_keeps_the_instance_name():
    """Заголовок вкладки пишут три места: старт, плашка обрыва и тик со счётчиком
    прогонов. Имя инстанса в него подставляет сервер, поэтому каждое обязано строиться
    из TITLE. Тик этого не делал, и имя стиралось через три секунды после загрузки."""
    js = slice_out("script")
    writes = re.findall(r"document\.title = (.+)", js)
    assert writes, "заголовок вкладки никто не пишет — проверка холостая"
    assert all("TITLE" in w for w in writes), writes


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_window_hues_keep_their_distance(tmp_path):
    """Оттенки окон разносятся по кругу шагом золотого угла. Список из шести тонов держал
    синий в 55° от пурпурного, а седьмое окно повторяло чужой цвет — на бледной заливке
    оба случая читались как «два окна одного цвета»."""
    body = slice_out("script").split("// --- hue:begin ---")[1].split("// --- hue:end ---")[0]
    js = tmp_path / "hue.js"
    js.write_text("""
let panes = [], hues = {};
""" + body + """
const got = [];
const spread = (list) => {
  let worst = 360;
  for (let i = 0; i < list.length; i++)
    for (let j = i + 1; j < list.length; j++) worst = Math.min(worst, arc(list[i], list[j]));
  return worst;
};
for (let n = 0; n < 8; n++) { const h = freeHue(); panes.push({ hue: h }); got.push(h); }
const worstSix = spread(got.slice(0, 6));
const worst = spread(got);
// Цвет закрытой сессии тоже занят: карта `hues` живёт дольше окна.
panes = [];
hues = { s1: got[0], s2: got[1] };
const next = freeHue();
console.log(JSON.stringify([got, worstSix, worst,
                            Math.min(arc(next, got[0]), arc(next, got[1]))]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got, worst_six, worst, from_closed = json.loads(done.stdout)

    assert len(set(got)) == 8      # восемь окон — восемь разных тонов, без повторов
    assert worst_six >= 45         # до шести окон — не ближе сорока пяти градусов
    # Дальше упирается в круг и в то, что цвета раздаются по одному, без пересдачи:
    # восемь окон — восемь секторов, 45° в идеале, а вставка в самый широкий промежуток
    # даёт 22. Порог тут не мягкий, а помещающийся.
    assert worst >= 20
    assert from_closed >= 40       # цвет закрытой сессии тоже занят, новый его обходит
