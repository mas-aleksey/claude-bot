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
    # Размер сетки берём из самого скрипта, а не повторяем числом: на 12 колонках пять
    # окон в ряд выходили 2,3,2,3,2 клетки, и проверка обязана ловить это на тех числах,
    # с которыми панель работает на самом деле.
    grid = re.search(r"const COLS = (\d+), ROWS = (\d+)", slice_out("script"))
    js.write_text(f"""
const COLS = {grid[1]}, ROWS = {grid[2]}, W = COLS / 2, H = ROWS / 2;
""" + """
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
// Разброс ширин в ряду: худшее отношение по числу окон от двух до семи.
const spread = [];
for (let n = 2; n <= 7; n++) {
  panes = Array.from({ length: n }, () => ({}));
  retile();
  const top = panes.filter(p => p.r === 1).map(p => p.w);
  spread.push(Math.max(...top) / Math.min(...top));
}
console.log(JSON.stringify([seen, count, was, big, back, three, rows, spread]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    seen, count, was, big, back, three, rows, spread = json.loads(done.stdout)

    # На каждом шаге от одного окна до девяти: без перекрытий, без выхода за сетку и
    # ровно 96 занятых клеток. Семь окон и были жалобой — прежняя раскладка теряла
    # остаток от деления и оставляла внизу две пустые полосы.
    cols, rows_n = int(grid[1]), int(grid[2])
    assert count == 9
    assert all(not over and not out for over, out, _ in seen)
    assert {area for _, _, area in seen} == {cols * rows_n}  # сетка занята целиком всегда
    assert big == [1, 1, cols, rows_n]    # развёрнутое занимает всю область
    assert back == was                    # и возвращается ровно откуда развернули
    # Три окна на широком экране — три колонки во всю высоту, а не два сверху и одно снизу.
    third = cols // 3
    assert three == [[1, 1, third, rows_n], [1 + third, 1, third, rows_n],
                     [1 + 2 * third, 1, third, rows_n]]
    # Шесть — ровно 3 + 3. Раскладка по средней клетке давала 4 + 2: четыре правильных
    # окна перевешивали два растянутых, и ряды выходили разной формы.
    assert rows == [3, 3]
    # Ширины в ряду ровные до семи окон. На 12 колонках пять окон давали 1.5x, семь — 2x:
    # 12 не делится ни на 5, ни на 7, и остаток раздавался каждому второму.
    assert max(spread) <= 1.2, spread


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_own_prompt_is_shown_once(tmp_path):
    """Свой промпт печатается сразу и через секунды приезжает из транскрипта. Сверка
    идёт по всей очереди и по схлопнутым пробелам: слеш-команду claude пересобирает из
    тегов, а застрявшая запись раньше глушила сверку для всех следующих промптов.

    Плюс уборка: промпт, не доехавший до транскрипта, оставался в памяти вкладки
    навсегда и однажды съедал законный повтор того же текста."""
    body = slice_out("script").split("// --- echo:begin ---")[1].split("// --- echo:end ---")[0]
    js = tmp_path / "echo.js"
    js.write_text("""
const echoes = new Map();
const NOW = 1000000;
// Запись эха несёт узлы своих строк в логе: их снимает absorb, когда тот же промпт
// приедет из транскрипта. Тут важно только, что они доезжают до вызывающего.
const rec = (text, age = 0) => ({ text, at: NOW - age, nodes: ['узел ' + text] });
const texts = (q) => q.map(e => e.text);
""" + body + """
const q1 = [rec('/refine текст')];
const same = dropEcho([{ role: 'user', text: '/refine  текст' }], q1);

// Застрявшее эхо (промпт не доехал до транскрипта) не должно глушить следующий.
const q2 = [rec('застряло'), rec('новый промпт')];
const after = dropEcho([{ role: 'user', text: 'новый промпт' }], q2);

// Чужая строка и ответ claude проходят как есть, очередь не трогают.
const q3 = [rec('моё')];
const rest = dropEcho([{ role: 'assistant', text: 'моё' },
                       { role: 'user', text: 'из телеграма' }], q3);

// Уборка. Панель занята или в очереди ждут — не трогаем: строка такого промпта ещё
// придёт, сколько бы он ни ждал. Свободна — снимаем то, что старше порога.
echoes.set('p1', [rec('старое', ECHO_IDLE + 1), rec('только что')]);
sweepEchoes('p1', false, NOW);
const busy = texts(echoes.get('p1'));
sweepEchoes('p1', true, NOW);
const idle = texts(echoes.get('p1'));
echoes.set('p2', [rec('единственное', ECHO_IDLE + 1)]);
sweepEchoes('p2', true, NOW);
const gone = echoes.has('p2');

console.log(JSON.stringify([texts(same), texts(q1), texts(after), texts(q2),
                            texts(rest), texts(q3), busy, idle, gone,
                            same[0] && same[0].nodes]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    same, q1, after, q2, rest, q3, busy, idle, gone, nodes = json.loads(done.stdout)

    # Возвращаются совпавшие записи: их узлы снимает absorb, а сам промпт рисуется из
    # транскрипта, на своём месте. Раньше было наоборот — выбрасывался транскрипт.
    assert [same, q1] == [["/refine текст"], []]   # лишний пробел совпадению не мешает
    assert nodes == ["узел /refine текст"]         # узлы доехали до вызывающего
    assert [after, q2] == [["новый промпт"], ["застряло"]]   # застрявшее осталось лежать
    assert [rest, q3] == [[], ["моё"]]             # ответ и чужой промпт не тронуты
    assert busy == ["старое", "только что"]        # панель занята — не трогаем ничего
    assert idle == ["только что"]                  # свободна — ушло только протухшее
    assert gone is False                           # пустая очередь снимается целиком


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
  const cls = new Set(), vars = {}, ago = { textContent: '2д' };
  return { dataset: { id, ago: '2д' }, cls, vars, ago, title: '',
    classList: { toggle: (k, on) => { on ? cls.add(k) : cls.delete(k); } },
    querySelector: () => ago,
    style: { setProperty: (k, v) => { vars[k] = v; },
             removeProperty: (k) => { delete vars[k]; } } };
}
const fmt = (sec) => 'T' + sec;
const rows = [btn('a'), btn('b')];
rows[0].dataset.title = 'про сетку';
// Панель как элемент: `markList` смотрит её классы, чтобы перенести готовность в строку.
const pane = (...cls) => ({ classList: { contains: (k) => cls.includes(k) } });
let panels = {};
const document = {
  querySelectorAll: () => rows,
  querySelector: (sel) => rows.find(r => sel.includes('"' + r.dataset.id + '"')) ?? null,
  getElementById: (id) => panels[id] ?? null,
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
const state = () => rows.map(r => [[...r.cls].sort(), r.vars['--hue'] ?? null,
                                  r.ago.textContent]);
const seen = [];

trackRuns([{ session: 'a', secs: 7 }]);   // запуск при закрытом окне
markList(); seen.push(state());

trackRuns([]);                            // кончился, окна так и не было
markList(); seen.push(state());
seen.push(JSON.parse(store.done));        // метка легла в localStorage

panes = [{ session: 'a', hue: 25 }];      // открыли сессию
done.delete('a');
markList(); seen.push(state());

panes = [{ session: 'a', hue: 25 }];      // прогон при открытом окне
trackRuns([{ session: 'a', secs: 7 }]);
trackRuns([]);                            // кончился на глазах — ни точки, ни звонка
markList(); seen.push(state());

// Прогон кончился в окне, на которое не смотрели: панель помечена, и строка списка
// обязана повторить её метку — развёрнутый сосед закрывает само окно целиком.
panes = [{ pane: 'p1', session: 'a', hue: 25 }];
panels = { 'pane-p1': pane('ready') };
markList(); seen.push(state());
panels = { 'pane-p1': pane('ready', 'bad') };
markList(); seen.push(state());
panels = { 'pane-p1': pane() };           // кликнули по окну — метка снята там и тут
markList(); seen.push(state());

panes = [];                               // закрыли, ничего не идёт
panels = {};
trackRuns([]);
markList(); seen.push(state());
console.log(JSON.stringify([...seen, sent]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    (running, finished, stored, opened, watched,
     ready, failed, clicked, forgotten, sent) = json.loads(done.stdout)
    assert running == [[["busy"], 25, "T7"], [[], None, "2д"]]   # мигает, тикает, не залита
    assert finished == [[["done"], 25, "2д"], [[], None, "2д"]]  # точка, возраст вернулся
    assert stored == ["a"]                              # переживёт F5
    assert opened == [[["open"], 25, "2д"], [[], None, "2д"]]    # заливка, точка снята
    assert watched == [[["open"], 25, "2д"], [[], None, "2д"]]   # смотрели сами — точки нет
    # Готовность панели переезжает в строку: галочка, у упавшего прогона — кружок.
    assert ready == [[["ok", "open"], 25, "2д"], [[], None, "2д"]]
    assert failed == [[["bad", "open"], 25, "2д"], [[], None, "2д"]]
    assert clicked == [[["open"], 25, "2д"], [[], None, "2д"]]   # клик по окну гасит и строку
    assert forgotten == [[[], None, "2д"], [[], None, "2д"]]     # цвет забыт, карта не растёт
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
// Повторы из localStorage: первому цвет оставляем, следующих разводим. Работаем по
// `panes` — `freeHue` смотрит туда же, и на чужом списке он переназначенных не увидит.
panes = [{ hue: 190 }, { hue: 195 }, { hue: 192 }, {}];
spreadHues();
const fixed = panes.map(p => p.hue);
const spreadOk = fixed.every((h, i) => fixed.every((g, j) => i === j || arc(h, g) >= MIN_GAP));

panes = [];
hues = { s1: got[0], s2: got[1] };
const next = freeHue();
console.log(JSON.stringify([got, worstSix, worst,
                            Math.min(arc(next, got[0]), arc(next, got[1])),
                            fixed[0], spreadOk]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got, worst_six, worst, from_closed, kept, spread_ok = json.loads(done.stdout)

    assert len(set(got)) == 8      # восемь окон — восемь разных тонов, без повторов
    assert worst_six >= 45         # до шести окон — не ближе сорока пяти градусов
    # Дальше упирается в круг и в то, что цвета раздаются по одному, без пересдачи:
    # восемь окон — восемь секторов, 45° в идеале, а вставка в самый широкий промежуток
    # даёт 22. Порог тут не мягкий, а помещающийся.
    assert worst >= 20
    assert from_closed >= 40       # цвет закрытой сессии тоже занят, новый его обходит
    # Раскладка из localStorage могла прийти с одинаковыми оттенками — старый код их
    # выдавал. Чиним на загрузке: первому цвет оставляем, следующих разводим.
    assert kept == 190
    assert spread_ok


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_raising_a_pane_takes_over_the_full_screen(tmp_path):
    """Разворот переезжает на поднятое окно, а прежнее возвращается в свою клетку.
    Развёрнута всегда ровно одна панель: стопку развёрнутых не видно, и сколько их под
    верхним окном, узнать нечем."""
    body = slice_out("script").split("// --- raise:begin ---")[1].split("// --- raise:end ---")[0]
    js = tmp_path / "raise.js"
    js.write_text("""
const mk = (id) => ({ id, cls: new Set(),
  classList: { add: (k) => mk.last.cls.add(k), remove: () => {} } });
const el = (id) => ({ id, cls: new Set(),
  classList: { add(k) { this.set.add(k); }, remove(...k) { k.forEach(x => this.set.delete(x)); },
               set: new Set() } });
const big = el('pane-a'), small = el('pane-b');
const nodes = { 'pane-a': big, 'pane-b': small };
const document = { getElementById: (id) => nodes[id] ?? null, querySelectorAll: () => [] };
const COLS = 12, ROWS = 8;
const applyGeom = () => {};
const drawZoom = () => {};
const save = () => {};
function zoom(p) {
  if (p.prev) { Object.assign(p, p.prev); p.prev = null; }
  else { p.prev = { c: p.c, r: p.r, w: p.w, h: p.h }; p.c = p.r = 1; p.w = COLS; p.h = ROWS; }
}
let panes = [{ pane: 'a', c: 1, r: 1, w: 12, h: 8, prev: { c: 7, r: 1, w: 6, h: 4 } },
             { pane: 'b', c: 1, r: 5, w: 6, h: 4 }];
""" + body + """
raise(small);
const after = panes.map(p => [p.c, p.r, p.w, p.h, !!p.prev]);
// Касание самого развёрнутого его не сворачивает.
panes = [{ pane: 'a', c: 1, r: 1, w: 12, h: 8, prev: { c: 7, r: 1, w: 6, h: 4 } }];
raise(big);
console.log(JSON.stringify([after, [panes[0].w, panes[0].h, !!panes[0].prev]]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    after, self_raise = json.loads(done.stdout)

    assert after[0] == [7, 1, 6, 4, False]     # прежнее вернулось в свою клетку
    assert after[1] == [1, 1, 12, 8, True]    # разворот забрало поднятое
    assert self_raise == [12, 8, True]        # своё касание разворот не снимает


def test_session_title_is_cut_with_an_ellipsis():
    """Название сессии — это первый промпт, и в узком сайдбаре длинное занимало три
    строки. Режем короче и ставим многоточие, чтобы обрезка была видна."""
    js = slice_out("script")
    assert "s.title.length > 42" in js
    assert "\\u2026" in js


def test_one_badge_for_every_unseen_answer():
    """«Ответ пришёл в закрытое окно» и «прогон закончился» — один смысл. Точка в цвет
    сессии стояла рядом с зелёным квадратом и выглядела вторым языком для того же."""
    css = slice_out("style")
    assert "button.done::after" not in css          # точки больше нет
    assert "#list button.done .ago::after" in css   # тот же квадрат, что у `ok`


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_every_picked_file_is_uploaded(tmp_path):
    """FileList у `<input type=file>` живой, и обработчик выбора чистит его сразу после
    первого `await` внутри attach. Пока цикл шёл по самому списку, из выбранной пачки
    доезжал ровно один файл: на второй итерации список уже пуст."""
    body = slice_out("script").split("// --- attach:begin ---")[1].split("// --- attach:end ---")[0]
    js = tmp_path / "attach.js"
    js.write_text("""
// Подставной FileList: длина и обход читают одно хранилище, поэтому `clear()` виден
// уже начатому циклу — ровно как настоящий список после `pick.value = ''`.
function fileList(names) {
  const items = names.map(name => ({ name, size: 10 }));
  return {
    clear: () => { items.length = 0; },
    [Symbol.iterator]() {
      let i = 0;
      return { next: () => i < items.length ? { value: items[i++], done: false }
                                            : { value: undefined, done: true } };
    },
  };
}
const log = () => null;
const kb = () => '10 б';
const grow = () => {};
const esc = (s) => s;
const upload = async (file) => ({ path: '/data/inbox/' + file.name });
""" + body + """
(async () => {
  const ta = { value: '', focus: () => {} };
  const list = fileList(['a.txt', 'b.txt', 'c.txt']);
  const done = attach({}, ta, list);
  list.clear();            // ровно то, что делает `pick.value = ''` в обработчике
  await done;

  // Выбор без файлов и отсутствие списка вовсе не должны ронять обработчик.
  const empty = { value: '', focus: () => {} };
  await attach({}, empty, fileList([]));
  await attach({}, empty, undefined);

  console.log(JSON.stringify([ta.value.trim().split('\\n'), empty.value]));
})();
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    paths, empty = json.loads(done.stdout)

    assert paths == ["/data/inbox/a.txt", "/data/inbox/b.txt", "/data/inbox/c.txt"]
    assert empty == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_plan_keeps_deadlines_in_tooltips(tmp_path):
    """Сроки живут в подсказках, в строках остаётся то, что меняет решение. Строка про
    ключ стоит всегда, кнопка входа — только когда она нужна."""
    body = slice_out("script").split("// --- plan:begin ---")[1].split("// --- plan:end ---")[0]
    js = tmp_path / "plan.js"
    js.write_text("""
const DAY = 86400e3;
const NOW = Date.now();
let box;
const $ = () => box;
const esc = (s) => String(s);
const since = () => '1 мин назад';
// `GONE` живёт рядом с настоящим `until`, вне вырезанного блока: подставляем оба вместе,
// иначе прошедший срок в блоке не с чем сравнить.
const GONE = 'gone';
const until = (iso) => {
  const h = Math.floor((new Date(iso) - NOW) / 3600e3);
  if (!iso) return '';
  return h > 0 ? (h >= 24 ? Math.floor(h / 24) + 'д ' + (h % 24) + 'ч' : h + 'ч 0м') : GONE;
};
const doLogin = () => {};
""" + body + """
// Разметку разбираем регулярками: DOM тут подставной, а проверяем мы ровно то, что
// уходит в innerHTML — текст строки, её подсказку и наличие кнопки.
function draw(lim, auth) {
  box = { dataset: {}, hidden: false, innerHTML: '', querySelector: () => null };
  setPlan(lim, auth);
  if (box.hidden) return { hidden: true };
  const key = box.innerHTML.match(/<div class="auth([^"]*)" title="([^"]*)"><em>([^<]*)<\\/em>/);
  const who = box.innerHTML.match(/<div class=who title="([^"]*)">([^<]*)</);
  const bars = [...box.innerHTML.matchAll(
    /<div class=lim title="([^"]*)"><em>([^<]*)<\\/em><span>([^<]*)</g)].map(m => [m[1], m[2], m[3]]);
  return {
    who: who && [who[1], who[2]],
    key: key && [key[1].trim(), key[2], key[3], box.innerHTML.includes('id=login')],
    bars,
  };
}

const lim = { email: 'me@x.dev', plan: 'max 5x', at: NOW / 1000,
              bars: [{ name: 'сессия', percent: 60, resets: NOW + 2 * 3600e3 },
                     { name: 'неделя', percent: 95, resets: NOW + 3 * 3600e3 },
                     { name: 'месяц', percent: 100, resets: NOW - 3600e3 }] };
const alive = { ok: true, fresh: true, renewable: true, until: (NOW + 4 * DAY) / 1000 };
const soon = { ok: true, fresh: true, renewable: true, until: (NOW + DAY) / 1000 };
const dead = { ok: true, fresh: false, renewable: false, until: null };
const none = { ok: false, fresh: false, renewable: false };

console.log(JSON.stringify({
  alive: draw(lim, alive),
  soon: draw(lim, soon),
  dead: draw(lim, dead),
  none: draw(lim, none),
  nolim: draw(null, alive),
  nothing: draw(null, null),
}));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    # Первая строка: возраст данных ушёл в подсказку, в строке только кто и по какому плану.
    assert got["alive"]["who"] == ["обновлено 1 мин назад", "me@x.dev · max 5x"]
    # Спокойная полоска: процент в строке, срок сброса только в подсказке.
    assert got["alive"]["bars"][0] == ["сброс лимитов через 2ч 0м", "сессия", "60%"]
    # Покрасневшая: срок возвращается в строку — тянуться за ним мышью уже поздно.
    # Вместо слова «через» стоит знак обновления: в узкой полосе сайдбара предлог
    # занимал место, а смысл нёс тот же.
    assert got["alive"]["bars"][1] == ["сброс лимитов через 3ч 0м", "неделя",
                                       "\u21bb 3ч 0м · 95%"]
    # Прошедший срок — это устаревшие цифры, а не незнание. До 2026-09-30 оба случая
    # давали у `until` пустую строку, и панель печатала «время сброса неизвестно» ровно
    # тогда, когда срок был известен и истёк.
    assert got["alive"]["bars"][2] == ["сброс уже прошёл, цифры сейчас обновятся",
                                       "месяц", "100%"]
    # Ключ: состояние словами, срок в подсказке, кнопка только когда она нужна.
    assert got["alive"]["key"] == ["", "рефреш через 4д 0ч", "ключ активен", False]
    assert got["soon"]["key"] == ["warn", "рефреш через 1д 0ч", "ключ скоро кончится", True]
    assert got["dead"]["key"] == ["hot", "обновить ключ нечем — нужен новый вход",
                                  "нужен вход", True]
    assert got["none"]["key"][0::2] == ["hot", "не авторизован"]
    assert got["nolim"]["key"][2] == "ключ активен"   # без полосок строка остаётся
    assert got["nothing"] == {"hidden": True}         # знать нечего — блока нет


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_hint_comes_from_the_answer(tmp_path):
    """Следующий промпт claude называет сам, в «ёлочках» последней строки — так требует
    стиль ответа. Оттуда подсказка и берётся, без вызова модели. Tab кладёт её в поле,
    отправку оставляет за Enter, а живёт она до самой отправки."""
    body = slice_out("script").split("// --- slash:begin ---")[1].split("// --- slash:end ---")[0]
    js = tmp_path / "hint.js"
    js.write_text("""
const shelf = {};
const localStorage = { getItem: (k) => k in shelf ? shelf[k] : null,
                       setItem: (k, v) => { shelf[k] = v; } };
const hints = new Map();
const esc = (s) => String(s);
const grow = () => {};
let sent = 0;
const send = () => { sent++; };
const kb = (n) => n + ' Б';
const TOUCH = { matches: false };
const get = async () => [];
const menu = { innerHTML: '', hidden: true };
const ghost = { innerHTML: '', scrollTop: 0 };
const ta = { value: '', selectionStart: 0, selectionEnd: 0, scrollTop: 0, focus() {} };
const el = { querySelector: (s) => s === '.menu' ? menu : ghost };
""" + body + """
const key = (k, mod = {}) => ta.onkeydown({ key: k, preventDefault() {}, ...mod });
const put = (text) => { ta.value = text; ta.selectionStart = ta.selectionEnd = text.length; };
const p = { pane: 'p1', project: '/projects/rp' };
wireSlash(p, el, ta);
const out = {};

// Обычный ответ: действие последней строкой, формулировка дословно.
out.plain = fromAnswer('Собрано, контейнер пересоздастся.\\n\\nСкажи «собери» — соберу образ.');
// Цитаты в начале ответа не в счёт: берём ту, что в хвосте.
out.tail = fromAnswer('Имя «claude-bot» это тег.\\nТут про «сети».\\nПусто.\\nПусто.\\n' +
                      'Скажи «раскатай на песочницы», и соберу оба.');
// Две в одной строке — берём последнюю, она и есть промпт.
out.two = fromAnswer('Вместо «wip» напиши «sync-repo».');
out.none = fromAnswer('Готово, ничего не нужно.');
out.empty = fromAnswer('');
out.missing = fromAnswer(undefined);
// Кавычка на пол-ответа промптом не бывает: потолок 200 символов.
out.huge = fromAnswer('скажи «' + 'я'.repeat(201) + '»');

// Tab на пустом поле подставляет подсказку. Отправки нет, подсказка живёт дальше.
hints.set('p1', 'собери');
put(''); ta.oninput();
out.grey = ta.placeholder;
key('Tab');
out.filled = [ta.value, hints.get('p1'), sent];

// Набрал своё и стёр — серая подсказка вернулась.
put('черновик'); ta.oninput();
key('Tab');
out.draft = ta.value;
put(''); ta.oninput();
out.back = ta.placeholder;

// Без подсказки серого нет, Tab ничего не делает.
hints.delete('p1');
put(''); ta.oninput();
key('Tab');
out.bare = [ta.value, ta.placeholder];

// На телефоне клавиши Tab нет, и подсказку берёт Enter. На десктопе он по-прежнему
// отправляет, поэтому ветка включается только при coarse-указателе.
hints.set('p1', 'собери');
TOUCH.matches = false;
put(''); key('Enter');
out.mouseEnter = [ta.value, sent];
TOUCH.matches = true;
put(''); key('Enter');
out.touchEnter = [ta.value, sent];
// Настоящая клавиатура у планшета: Shift+Enter остаётся переносом строки.
put(''); key('Enter', { shiftKey: true });
out.shiftEnter = ta.value;

// Экранная клавиатура: iOS не обещает `key: 'Enter'` в keydown, поэтому тот же ввод
// ловится намерением. Проверяем без keydown вовсе — как оно и приходит на телефоне.
const typed = (inputType) => {
  let stopped = false;
  ta.onbeforeinput({ inputType, preventDefault() { stopped = true; } });
  return stopped;
};
hints.set('p1', 'собери');
put('');
out.softEnter = [typed('insertLineBreak'), ta.value];
// Обычный набор букв подсказку не трогает.
hints.set('p1', 'собери');
put('');
out.softType = [typed('insertText'), ta.value];
// Мышь: намерение то же, но путь не наш — там Enter отправляет.
TOUCH.matches = false;
hints.set('p1', 'собери');
put('');
out.mouseSoft = [typed('insertLineBreak'), ta.value];
TOUCH.matches = true;

console.log(JSON.stringify(out));
""", encoding="utf-8")
    done = subprocess.run(["node", "--input-type=module", "-e", js.read_text(encoding="utf-8")],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    assert out["plain"] == "собери"
    assert out["tail"] == "раскатай на песочницы"      # цитаты из начала ответа не взяты
    assert out["two"] == "sync-repo"                   # в строке две — промпт последняя
    assert [out["none"], out["empty"], out["missing"]] == [None, None, None]
    assert out["huge"] is None                         # длинную кавычку не берём

    assert out["grey"] == "собери"                     # серым в пустом поле
    assert out["filled"] == ["собери", "собери", 0]    # подставлено, не отправлено
    assert out["draft"] == "черновик"                  # набранное Tab не трогает
    assert out["back"] == "собери"                     # стёр своё — подсказка вернулась
    # Пробел, а не пустая строка: на нём держится `:placeholder-shown`, гасящий кнопку.
    assert out["bare"] == ["", " "]

    # Мышь: Enter на пустом поле уходит в отправку, а она сама отбивает пустой промпт.
    assert out["mouseEnter"] == ["", 1]
    # Палец: тот же Enter кладёт подсказку в поле и ничего не отправляет.
    assert out["touchEnter"] == ["собери", 1]
    assert out["shiftEnter"] == ""      # перенос строки остаётся переносом строки

    # Экранная клавиатура: подсказка встаёт по намерению «ввод», без опоры на `key`.
    assert out["softEnter"] == [True, "собери"]
    assert out["softType"] == [False, ""]     # обычный набор ничего не подставляет
    assert out["mouseSoft"] == [False, ""]    # с мышью эта дорога не работает


def test_hint_runs_after_the_pane_is_marked_free():
    """Подсказка стоит после переключения `busy` и разблокировки «стоп».

    Всё, что в цикле панелей стоит до них, при исключении оставляет окно навсегда
    занятым: кнопка «стоп» нажата, отправка погашена, в консоли пусто. Так и было
    2026-09-29 — имя `answers` звалось `said`, одноимённый `const` ниже по блоку накрывал
    обращение мёртвой зоной, и ReferenceError вешал панель после каждого прогона.
    """
    js = slice_out("script")
    free = js.index("classList.toggle('busy', busy)")
    hint = js.index("fromAnswer(answers.get(p.pane))")
    assert free < hint, "подсказка не должна стоять до снятия занятости"

    # Имя хранилища ответов не должно объявляться второй раз: внутри цикла панелей это
    # и создало мёртвую зону. Проверяем по всему скрипту, а не по одному блоку.
    assert js.count("const answers") == 1
    assert "answers" not in js.split("const answers")[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_field_height_ignores_the_hint(tmp_path):
    """У пустого поля `scrollHeight` считает и placeholder. Длинная подсказка растягивала
    композер до потолка, браузер подкручивал секцию к полю с фокусом, и шапка уезжала за
    верхний край — вернуть её было нечем, у секции `overflow:hidden`."""
    js_src = slice_out("script")
    body = "function grow" + js_src.split("function grow")[1].split("\n}\n")[0] + "\n}\n"
    js = tmp_path / "grow.js"
    js.write_text(body + """
// Подставное поле: высота растёт и от значения, и от подсказки — как в браузере.
const field = (value, placeholder) => ({
  value, placeholder, style: {},
  get scrollHeight() { return 40 + (this.value.length + this.placeholder.length) * 4; },
});
const long = 'скажи «собери» — соберу образ и поставлю отложенный рестарт';
const empty = field('', long);
grow(empty);
const typed = field('привет', long);
grow(typed);
console.log(JSON.stringify([empty.style.height, empty.placeholder,
                            typed.style.height, typed.placeholder]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    bare, kept_bare, typed, kept_typed = json.loads(done.stdout)

    assert bare == "40px"          # пустое поле — одна строка, подсказка на высоту не влияет
    assert typed == "64px"         # набранное считается как раньше
    # Подсказку возвращаем на место, иначе серый текст пропадал бы на каждой букве.
    hint = "скажи «собери» — соберу образ и поставлю отложенный рестарт"
    assert kept_bare == kept_typed == hint


def test_page_is_assembled_from_the_three_files():
    """Страница собирается из `src/web/`, и потерянный при сборке файл — это живой бот с
    пустой панелью. Проверяем оба конца: файлы на месте и целиком доехали в `PAGE`.

    Отсутствие файла в колесе этим тестом не ловится — он читает `src/`, а не
    установленный пакет. Для этого есть проверка внутри образа при раскатке.
    """
    import page

    parts = {"style.css": "grid-column:var(--c,1)",
             "body.html": "<div id=panes>",
             "app.js": "function drawPane"}
    for name, anchor in parts.items():
        text = (page.WEB / name).read_text(encoding="utf-8")
        assert anchor in text, f"{name} не тот файл"
        assert text in page.PAGE, f"{name} не целиком попал в PAGE"

    # Один `<style>` и один `<script>`: страница отдаётся одним ответом, и тесты режут
    # её по этим тегам.
    assert page.PAGE.count("<style>") == page.PAGE.count("<script>") == 1


def test_finished_pane_is_veiled_in_the_colour_of_its_outcome():
    """Законченное окно затянуто пеленой в цвет исхода, а в списке от исхода остаётся
    только знак. Пелена заменила косую штриховку тоном окна: фактура говорила «что-то
    случилось», а какой именно исход, приходилось искать глазами в знаке.

    Заливку строки в списке пробовали дважды, бледную и плотную, и оба раза она спорила
    с фоном строки, занятым цветом окна. Мигание ушло оттуда же: в окне мигает одна
    панель, в списке мигали все занятые сразу.
    """
    css = slice_out("style")
    GREEN, RED = "oklch(0.60 0.19 145", "oklch(0.58 0.22 25"

    assert "repeating-linear-gradient" not in css      # штриховки больше нет
    assert f"border-color:{GREEN}) }}" in css
    assert f"border-color:{RED}) }}" in css
    # Тот же зелёный и красный, что у знака посреди окна — цвета не разъезжаются.
    assert "section.ready::after" in css and f"{GREEN} / .9)" in css
    assert "section.ready.bad::after" in css and f"{RED} / .9)" in css

    # Пелена — слой НАД содержимым, а не фон под ним: фоном текст не закрыть при любой
    # плотности. Кликабельная насквозь, иначе её нечем снять.
    assert "section.ready .logbox::after, section.ready form::after" in css
    assert "background:var(--veil); opacity:.80" in css
    # Цвет разбавлен фоном: бледный и плотный читается как «поверх всего», насыщенный
    # вполсилы — как подсветка текста. `Canvas` в примеси уводит пелену за темой.
    assert f"--veil:color-mix(in oklch, {GREEN}) 20%, Canvas)" in css
    assert f"--veil:color-mix(in oklch, {RED}) 20%, Canvas)" in css
    # Размытие закрывает читаемость вместо плотности краски. Префикс обязателен: без
    # него Safari старше 18 оставляет пелену простой заливкой.
    assert "-webkit-backdrop-filter:blur(3px); backdrop-filter:blur(3px)" in css

    # Заголовка пелена не касается вовсе: правила на него нет, и порядком слоёв это
    # больше не решается — в окне уже четыре разных z-index.
    assert "section.ready header" not in css
    # Композер накрыт вместе с полями, иначе вокруг него остаётся неокрашенный кант.
    assert "section.ready form::after { inset:-8px -20px -8px -8px }" in css

    # В списке от исхода остаётся только знак: заливку строки пробовали дважды и сняли.
    assert "#list button.ok, #list button.done:not(.open) { background:" not in css
    assert "#list button.ok .ago::after" in css

    # Мигание исхода и занятости не вернулось: мигали и строка списка, и заголовок окна,
    # и обе сразу. Единственная анимация в файле — бегущая штриховка внутри уже
    # нарисованной заливки контекста, и она обязана выключаться по `prefers-reduced-motion`.
    assert css.count("@keyframes") == 1 and "@keyframes ctxrun" in css
    assert "animation:ctxrun" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation:none" in css

    assert "inset 4px 0 0" not in css      # цветная рельса исхода снята
    assert "inset 3px 0 0" in css          # полоса «это окно открыто» осталась


def test_file_pane_has_no_idle_embed_and_scopes_the_preview_rule():
    """Две ловушки окна файла, обе стоили половины его высоты.

    `<embed>` не прячется атрибутом `hidden`: WebKit рисует плагин отдельным слоем, и
    пустой бокс с `flex:1` забирал полокна — под редактором в режиме исходника и над
    вёрсткой в режиме просмотра. Поэтому в разметке его нет вовсе, он создаётся под
    открытый PDF.

    Второе: класс `look` носят и блок вёрстки, и кнопка-переключатель. Незаякоренный
    селектор раздавал `flex:1` обоим, и кнопка растягивалась на треть строки.
    """
    js, css = slice_out("script"), slice_out("style")

    assert "<embed" not in js.split("function drawFile")[1][:1200], "embed в шаблоне окна"
    assert "document.createElement('embed')" in js      # создаётся под файл
    assert "dropDoc()" in js                            # и снимается при смене файла

    assert "div.look {" in css and "\n.look {" not in css
    assert "button.look" in css                         # кнопка стилизуется отдельно


def test_context_fill_does_not_eat_clicks():
    """Заливка контекста лежит поверх заголовка во всю его высоту и идёт последней в
    разметке — то есть рисуется над кнопками. Без `pointer-events:none` она собирает на
    себя и курсор, и клики: заполнился контекст — перестали нажиматься «закрыть» и
    «во весь экран». В списке строк это с самого начала было учтено, в заголовке — нет.
    """
    import page

    css = (page.WEB / "style.css").read_text(encoding="utf-8")
    for sel in ("header .ctx {", "#list .ctx {"):
        rule = css.split(sel, 1)[1].split("}", 1)[0]
        assert "pointer-events:none" in rule, f"{sel} ловит клики"
