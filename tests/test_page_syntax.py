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
def test_each_tool_step_is_its_own_line(tmp_path):
    """Шаги не сворачиваются в пачку (с 2026-10-16): каждый вызов — своя строка лога.

    Результат вписывается в свой шаг по id: в `pre`, а не в последнего потомка (его
    успевала обернуть кнопка «копировать»), длительность — разность времени ответа и
    вызова. Упавший шаг помечается. Пустой успех приходит ради времени и окошка не
    открывает. Результат без шага — его срезал потолок лога — пропадает молча.
    """
    body = slice_out("script").split("// --- tools:begin ---")[1].split("// --- tools:end ---")[0]
    js = tmp_path / "steps.js"
    js.write_text("""
const CSS = { escape: (s) => s, supports: () => true };
const fmt = (sec) => Math.floor(sec / 60) + ':' + String(Math.round(sec % 60)).padStart(2, '0');
const renderItem = (it) => it.role;
// Шаг с двумя окошками: OUT (с `pre` и чужой обёрткой последним потомком) и подписью.
const mkStep = (id, ts) => {
  const pre = { textContent: '' }, u = { textContent: '\u2026' };
  const out = { hidden: true, cls: [], lastElementChild: { textContent: '' },
    querySelector: (q) => q === 'pre' ? pre : null,
    classList: { toggle(c, on) { if (on) out.cls.push(c); }, add() {} } };
  const step = { id, dataset: { ts: String(ts) }, cls: [], pre, u, out,
    querySelector: (q) => q === '.out' ? out : q === 'summary u' ? u : null,
    classList: { toggle(c, on) { if (on) step.cls.push(c); } } };
  return step;
};
const steps = { a: mkStep('a', 1000), b: mkStep('b', 5000) };
const kids = [];
const box = { insertAdjacentHTML: (_, h) => kids.push(h),
  querySelector: (q) => steps[(q.match(/data-tid="([^"]+)"/) || [])[1]] || null };
""" + body + """
pour(box, [
  { role: 'user', text: 'сделай' },
  { role: 'tool', id: 'a' }, { role: 'tool', id: 'b' },
  { role: 'out', id: 'a', text: '', err: false, ts: 1400 },
  { role: 'out', id: 'b', text: '1 failed', err: true, ts: 77000 },
  { role: 'out', id: 'gone', text: 'шаг срезан', ts: 9 },
  { role: 'assistant', text: 'готово' },
]);
const show = (s) => [s.out.hidden, s.out.cls, s.pre.textContent, s.cls, s.u.textContent];
console.log(JSON.stringify({ kids, a: show(steps.a), b: show(steps.b) }));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    # Каждый шаг — свой элемент лога, никакой пачки вокруг; результаты строк не дают.
    assert got["kids"] == ["user", "tool", "tool", "assistant"]
    # Пустой успех: окошко закрыто, длительность есть.
    assert got["a"] == [True, [], "", [], "<1 с"]
    # Ошибка: окошко открыто и красное, шаг помечен, текст в `pre`, 72 секунды — 1:12.
    assert got["b"] == [False, ["err"], "1 failed", ["bad"], "1:12"]


def test_copy_button_skips_tool_io():
    """Кнопка «копировать» — для кода в ответах claude, не для окошек IN и OUT.

    Пустой OUT рисуется заранее, и обёртка ловила его раньше вывода: кнопка запоминала
    пустую строку, а вывод потом стирал её вместе с обёрткой. Поймано на живом прогоне
    2026-10-13 — кнопка висела поверх вывода.
    """
    js = slice_out("script")
    body = js.split("function wireCopy")[1].split("\n}\n")[0]
    assert "pre:not([data-copy]):not(.io pre)" in body


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
    body = webui.PAGE.split("function upload")[1].split("\n// --- attach:begin ---")[0]
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
    """Разворот переезжает на окно, с которым работают, а прежнее возвращается в клетку.
    Развёрнута всегда ровно одна панель: стопку развёрнутых не видно, и сколько их под
    верхним окном, узнать нечем.

    Передаёт только `focus` — жест человека. Голый `raise` кладёт слой и ничего не
    трогает: через него идёт и отрисовка, а она поднимает каждое окно по очереди. Пока
    это была одна функция, восстановление после F5 перетасовывало развёрнутое и
    оставляло его на последнем нарисованном окне — на телефоне это низ стопки.
    """
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
const rememberFront = () => {};   // запись в localStorage тут ни при чём, см. соседний тест
function zoom(p) {
  if (p.prev) { Object.assign(p, p.prev); p.prev = null; }
  else { p.prev = { c: p.c, r: p.r, w: p.w, h: p.h }; p.c = p.r = 1; p.w = COLS; p.h = ROWS; }
}
let panes = [{ pane: 'a', c: 1, r: 1, w: 12, h: 8, prev: { c: 7, r: 1, w: 6, h: 4 } },
             { pane: 'b', c: 1, r: 5, w: 6, h: 4 }];
""" + body + """
focus(small);
const after = panes.map(p => [p.c, p.r, p.w, p.h, !!p.prev]);
// Касание самого развёрнутого его не сворачивает.
panes = [{ pane: 'a', c: 1, r: 1, w: 12, h: 8, prev: { c: 7, r: 1, w: 6, h: 4 } }];
focus(big);
const self_ = [panes[0].w, panes[0].h, !!panes[0].prev];
// Отрисовка поднимает каждое окно по очереди — и не должна трогать разворот ничьим.
panes = [{ pane: 'a', c: 1, r: 1, w: 12, h: 8, prev: { c: 7, r: 1, w: 6, h: 4 } },
         { pane: 'b', c: 1, r: 5, w: 6, h: 4 }];
raise(big); raise(small);
const drawn = panes.map(p => [p.c, p.r, p.w, p.h, !!p.prev]);
console.log(JSON.stringify([after, self_, drawn]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    after, self_focus, drawn = json.loads(done.stdout)

    assert after[0] == [7, 1, 6, 4, False]    # прежнее вернулось в свою клетку
    assert after[1] == [1, 1, 12, 8, True]    # разворот забрало то, с чем работают
    assert self_focus == [12, 8, True]        # своё касание разворот не снимает
    # Два подъёма подряд, как при восстановлении: развёрнутым осталось то же окно.
    assert drawn == [[1, 1, 12, 8, True], [1, 5, 6, 4, False]]


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
def test_picked_files_wait_in_chips_until_send(tmp_path):
    """Выбранные файлы ждут отправки в памяти вкладки, а не уезжают на сервер сразу.

    До 2026-10-13 `attach` грузил каждый файл в момент выбора, и передумать было нельзя:
    строку с путём из поля убрать можно, а файл оставался в песочнице. Проверяем всю
    цепочку — набор, снятие крестиком, загрузка на отправке и отказ.
    """
    body = slice_out("script").split("// --- attach:begin ---")[1].split("// --- attach:end ---")[0]
    js = tmp_path / "attach.js"
    js.write_text("""
// Подставной FileList: длина и обход читают одно хранилище, поэтому `clear()` виден
// уже начатому обходу — ровно как настоящий список после `pick.value = ''`.
function fileList(names) {
  const items = names.map(name => ({ name, size: 10, type: '' }));
  return {
    clear: () => { items.length = 0; },
    [Symbol.iterator]() {
      let i = 0;
      return { next: () => i < items.length ? { value: items[i++], done: false }
                                            : { value: undefined, done: true } };
    },
  };
}
// Строка карточек: `flushClips` пишет в неё ход отправки, поэтому нужны и children.
const chip = () => ({ classList: { add() {} }, querySelector: () => ({ textContent: '' }) });
const box = { hidden: true, innerHTML: '', children: [chip(), chip(), chip()] };
global.document = { querySelector: () => box };
const log = () => null;
const kb = () => '10 б';
const esc = (s) => s;
let mode = 'ok';
const upload = async (file) => mode === 'ok'
  ? { path: '/data/inbox/' + file.name }
  : Promise.reject(new Error('обрыв связи'));
""" + body + """
(async () => {
  const p = { pane: 'x' };
  const out = {};

  const list = fileList(['a.txt', 'b.txt', 'c.txt']);
  attach(p, list);
  list.clear();                          // ровно то, что делает `pick.value = ''`
  out.picked = (clips.get('x') || []).map(f => f.name);

  attach(p, fileList([]));         // пустой выбор и отсутствие списка не роняют
  attach(p, undefined);
  out.afterEmpty = (clips.get('x') || []).length;

  clips.get('x').splice(1, 1);           // то же, что делает крестик на второй карточке
  out.afterRemove = clips.get('x').map(f => f.name);

  mode = 'fail';
  out.failed = await flushClips(p);
  out.keptOnFail = (clips.get('x') || []).map(f => f.name);

  mode = 'ok';
  out.paths = await flushClips(p);
  out.leftover = (clips.get('x') || []).length;
  out.empty = await flushClips({ pane: 'none' });

  console.log(JSON.stringify(out));
})();
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    # Копия списка, а не сам FileList: он живой и к этому моменту уже пуст.
    assert got["picked"] == ["a.txt", "b.txt", "c.txt"]
    assert got["afterEmpty"] == 3
    assert got["afterRemove"] == ["a.txt", "c.txt"]
    # Один файл не доехал — не доехал весь промпт. Половина путей хуже, чем ничего:
    # разбираться в ней пришлось бы уже по ответу claude.
    assert got["failed"] is None
    assert got["keptOnFail"] == ["a.txt", "c.txt"], "карточки обязаны пережить отказ"
    assert got["paths"] == ["/data/inbox/a.txt", "/data/inbox/c.txt"]
    assert got["leftover"] == 0, "после отправки карточки снимаются"
    assert got["empty"] == []   # панель без файлов отправляется как обычно


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
// Двое суток — спокойное состояние с 2026-10-02: прогон продлевает только доступ, и
// жёлтое двое суток подряд перестаёт читаться как срочное.
const far = { ok: true, fresh: true, renewable: true, until: (NOW + 2 * DAY) / 1000 };
// Порог жёлтого — сутки. Берём половину, чтобы проверка не зависела от того, сколько
// миллисекунд прошло между построением фикстуры и сравнением с `Date.now()`.
const soon = { ok: true, fresh: true, renewable: true, until: (NOW + DAY / 2) / 1000 };
const dead = { ok: true, fresh: false, renewable: false, until: null };
const none = { ok: false, fresh: false, renewable: false };

console.log(JSON.stringify({
  alive: draw(lim, alive),
  soon: draw(lim, soon),
  far: draw(lim, far),
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
    assert got["alive"]["key"] == ["", "новый вход понадобится через 4д 0ч", "ключ активен", False]
    assert got["soon"]["key"] == ["warn", "новый вход понадобится через 12ч 0м",
                                  "ключ скоро кончится", True]
    # Двое суток — ещё спокойное: порог опущен до суток 2026-10-02, потому что прогон
    # продлевает только доступ, а жёлтое двое суток подряд перестаёт читаться как срочное.
    assert got["far"]["key"] == ["", "новый вход понадобится через 2д 0ч", "ключ активен", False]
    assert got["dead"]["key"] == ["hot", "обновить ключ нечем — нужен новый вход",
                                  "нужен вход", True]
    assert got["none"]["key"][0::2] == ["hot", "не авторизован"]
    assert got["nolim"]["key"][2] == "ключ активен"   # без полосок строка остаётся
    assert got["nothing"] == {"hidden": True}         # знать нечего — блока нет


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_hint_is_taken_by_tab_and_survives_a_draft(tmp_path):
    """Подсказку присылает бот (`st.suggest`), а поле только показывает её серым.
    Tab кладёт её в поле, отправку оставляет за Enter, а живёт она до самой отправки."""
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
    2026-09-29, когда подсказку доставали из ответа и обращение попадало в мёртвую зону.
    """
    js = slice_out("script")
    free = js.index("classList.toggle('busy', busy)")
    hint = js.index("(st.suggest || {})[p.session]")
    assert free < hint, "подсказка не должна стоять до снятия занятости"

    # Ставится на каждом тике, а не на переходе «занята → свободна»: после F5 перехода
    # не случится, а подсказка у бота уже есть.
    assert "wasBusy" not in js, "переход для подсказки больше не нужен"


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_field_height_ignores_the_hint(tmp_path):
    """У пустого поля `scrollHeight` считает и placeholder. Длинная подсказка растягивала
    композер до потолка, браузер подкручивал секцию к полю с фокусом, и шапка уезжала за
    верхний край — вернуть её было нечем, у секции `overflow:hidden`."""
    js_src = slice_out("script")
    body = "function grow" + js_src.split("function grow")[1].split("\n}\n")[0] + "\n}\n"
    js = tmp_path / "grow.js"
    # Проверяем ручной путь: `AUTOSIZE` — это ветка для браузера с `field-sizing`,
    # там высоту ставит CSS и ставить её скрипту нечем.
    assert "if (AUTOSIZE) return;" in js_src, "ручной расчёт обязан уступать нативному"
    js.write_text("const AUTOSIZE = false;\n" + body + """
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_front_pane_survives_a_reload(tmp_path):
    """После F5 впереди то же окно, что было поднято до неё.

    `act` держит слой, но живёт только в DOM: раньше передним назначалось `panes[0]`,
    то есть последнее открытое — новые окна кладутся `unshift`. Поднял окно постарше,
    перезагрузил — вперёд выходило чужое.
    """
    body = slice_out("script").split("// --- front:begin ---")[1].split("// --- front:end ---")[0]
    js = tmp_path / "front.js"
    js.write_text("""
const localStorage = { setItem: () => {} };
""" + body + """
const list = [{ pane: 'new' }, { pane: 'old' }, { pane: 'big', prev: { c: 1 } }];
console.log(JSON.stringify([
  frontPane(list, 'old').pane,      // запомненное окно
  frontPane(list, 'gone').pane,     // его закрыли — остаётся развёрнутое
  frontPane(list, null).pane,       // ключа нет вовсе
  frontPane([{ pane: 'one' }], 'gone').pane,   // развёрнутых нет — первое в списке
  frontPane([], 'gone'),            // окон нет вовсе
]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    assert json.loads(done.stdout) == ["old", "big", "big", "one", None]

    # Ключ обязан читаться до отрисовки: `drawPane` поднимает каждое окно и каждым
    # подъёмом перезаписывает `front` собой. Прочитанный после цикла, он указывал на
    # последнее нарисованное окно — и разворот после F5 доставался чужому.
    js = slice_out("script")
    assert js.index("const saved = localStorage.getItem('front')") \
        < js.index("panes.forEach(p => { p.next = 0; drawPane(p); })")


def test_prompts_stick_to_the_edges_of_the_log():
    """Чей это ход — полоской поверх лога сверху, а ждущие очереди — полосой под ним.

    Сам промпт закреплять нельзя: в логе он лежит полным текстом, бывает на пол-экрана,
    и закреплённый целиком не оставлял места ответу. Обрезать его на месте тоже нельзя —
    это меняет высоту строки в потоке и дёргает прокрутку.

    Очередь — наоборот, не поверх лога, а под ним, обычным блоком в колонке окна. Внутри
    лога её приходилось делать липкой и возвращать в конец на каждом тике (ответы
    дописываются после неё) — она скакала и ломала группировку инструментов; поверх лога
    она закрывала последние строки и спорила с кнопкой «вниз» за тот же край.
    """
    css = slice_out("style")
    # Обе полоски — одно и то же по смыслу, поэтому вид у них общий и лежит в одном
    # правиле. Разъедутся — одна правка поменяет половину пары.
    both = css.split(".pin, .user.pending {", 1)[1].split("}", 1)[0]
    assert "-webkit-line-clamp:2" in both, "полоска обязана быть в две строки"
    assert "Canvas" in both, "закреплённой полоске нужна непрозрачная подложка"
    # Фон — фон окна, а не голый Canvas: тот был белым или чёрным пятном на окне.
    assert "oklch(0.62 0.16 var(--hue,250) / .12)" in both, "фон полоски — тон окна"
    assert "#8882" not in both, "серого поверх фона окна нет"
    # С 2026-10-15 полоски — такие же блоки, как промпт в логе: контур в цвете окна.
    assert "border-radius:6px" in both, "полоска — блок, как промпт в логе"
    assert "oklch(0.62 0.20 var(--hue" in both, "контур в цвете своего окна"
    assert "165" not in both, "зелёной заливки больше нет"

    pin = css.split(".pin { ", 1)[1].split("}", 1)[0]
    assert "position:absolute" in pin and "top:6px" in pin
    # Зазор над полоской закрыт фоном: иначе в нём выглядывала бы строка лога.
    assert "box-shadow:0 0 0 6px color-mix(in srgb, oklch(0.62 0.16 var(--hue,250)) 12%, Canvas)" in pin
    assert "pointer-events:none" in pin, "выделение текста под полоской не должно упираться в неё"
    assert "opacity" not in both + pin, "полоска поверх текста, сквозь полупрозрачную он читается"

    user = css.split(".user { ", 1)[1].split("}", 1)[0]
    assert "position:sticky" not in user, "сам промпт в потоке не закрепляем"
    assert "background" not in user and "var(--hue" in user, "промпт — контур без заливки"
    # Блок очереди в потоке: ни липкий, ни абсолютный, ни со своим слоем. Всё это он уже
    # пробовал, и каждый раз спорил с логом за один и тот же нижний край.
    queue = css.split(".queue { ", 1)[1].split("}", 1)[0]
    for bad in ("position:", "z-index", "bottom:"):
        assert bad not in queue, f"блок очереди в потоке, {bad} ему не нужен"
    # Он сосед лога, а не его содержимое: внутри `.log` ответы дописываются после него.
    script = slice_out("script")
    assert "</div>\n    <div class=queue hidden></div>\n    <form>" in script
    # Кнопка «вниз» остаётся в логе и ни с чем не пересекается — свой слой ей больше не
    # нужен, но и мешать он не может: блок очереди теперь вне её родителя.
    assert "position:absolute" in css.split(".down { ", 1)[1].split("}", 1)[0]
    # Подписей ролей нет вовсе (2026-10-16): промпт узнаётся по контуру, ответ — по полосе.
    assert "class=role" not in slice_out("script") and ".role" not in css

    # Тот же класс и у локальной копии, и у строк блока очереди: выглядят они одинаково,
    # отличается только то, кто их рисует.
    assert '"msg user pending"' in slice_out("script")


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_pin_shows_the_prompt_of_the_turn_being_read(tmp_path):
    """В полоске — последний промпт, чей верх уже ушёл за край: ниже него идёт ответ,
    который и читают. Промотал выше — встаёт предыдущий, выше всех — полоска прячется.
    """
    body = slice_out("script").split("// --- pin:begin ---")[1].split("// --- pin:end ---")[0]
    js = tmp_path / "pin.js"
    js.write_text("""
const mk = (text, top) => ({ textContent: text,   // подписи «ты» нет с 2026-10-16
  getBoundingClientRect: () => ({ top }) });
const run = (tops) => {
  const box = { getBoundingClientRect: () => ({ top: 100 }),
                querySelectorAll: () => tops.map(([t, y]) => mk(t, y)) };
  const pin = {};
""" + body + """
  showPin(box, pin);
  return pin.hidden ? null : pin.textContent;
};
console.log(JSON.stringify([
  run([['первый', 20], ['второй', 60], ['третий', 300]]),   // третий ещё ниже края
  run([['первый', 20], ['второй', 300]]),                   // промотали выше
  run([['первый', 300]]),                                   // всё ниже края
]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    assert json.loads(done.stdout) == ["второй", "первый", None]


def test_zoomed_pane_on_a_phone_stays_in_the_flow():
    """На телефоне развёрнутое окно занимает всю область окон рядом, а не выходит из потока.

    `position:fixed; inset:0` стояло тут до 2026-10-04 и переживало только до
    перезагрузки: Safari на iOS считает `fixed` внутри прокручиваемого `#panes` по
    самому `#panes`, и после F5 окно получалось ростом со свою прежнюю клетку —
    состояние целое, а на вид разворот пропал.
    """
    phone = slice_out("style").split("@media (max-width: 700px) {", 1)[1]
    rule = phone.split("section.zoomed { ", 1)[1].split("}", 1)[0]
    assert "position:fixed" not in rule, "из потока окно выводить нельзя — см. выше"
    # Высота от экрана, а не от содержимого: при `auto` ряд рос по логу, и окно
    # становилось выше экрана телефона — прокручивалась вся страница вместо лога.
    assert "height:100dvh" in rule and "height:auto" not in rule

    box = phone.split("#panes:has(> section.zoomed) { ", 1)[1].split("}", 1)[0]
    assert "overflow:hidden" in box, "прокручивать нечего — окно ровно в экран"

    # Прежний `fixed` накрывал и полоску терминала. Без этого «во весь экран» на телефоне
    # перестало бы быть во весь экран.
    assert "body:has(#panes > section.zoomed) #term { display:none }" in phone

    # Полоски сайдбаров — по 24px с каждого края, и на телефоне это 12% ширины.
    assert "body.folded:has(#panes > section.zoomed) #fold" in phone
    assert "body.rfolded:has(#panes > section.zoomed) #rfold { display:none }" in phone
    # Условие на `folded` снимать нельзя: полоска — единственное, чем закрывается
    # открытый сайдбар, и без неё человек заперт с панелью на полэкрана.
    assert "body:has(#panes > section.zoomed) #fold" not in phone
    assert "body:has(#panes > section.zoomed) #rfold" not in phone


def test_page_is_exactly_as_tall_as_the_visible_screen():
    """Страница ростом в видимую часть экрана, иначе ей есть куда прокручиваться.

    `100vh` на iOS больше видимой высоты на съезжающие панели Safari. Клавиатура
    прокручивала страницу к полю ввода и, спрятавшись, оставляла её там — окно
    выглядело уехавшим вверх.
    """
    css = slice_out("style")
    rule = css.split("body { margin:0", 1)[1].split("}", 1)[0]
    assert "height:100dvh" in rule and "overflow:hidden" in rule


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_queue_block_follows_the_server_list(tmp_path):
    """Блок ждущих промптов целиком производный от статуса: появился, пересобрался по
    смене состава, исчез вместе с очередью. Своей памяти у вкладки тут нет — ради этого
    он и переехал на сервер, иначе перезагрузка стирала очередь с экрана.
    """
    body = slice_out("script").split("// --- queue:begin ---")[1].split("// --- queue:end ---")[0]
    js = tmp_path / "queue.js"
    js.write_text("""
const esc = (s) => s, linkify = (s) => s;
const q = { hidden: false, dataset: {}, innerHTML: '' };
const document = { querySelector: () => q };
const p = { pane: 'x' };
const seen = [];
const shot = () => seen.push([q.hidden, q.innerHTML]);
""" + body + """
drawQueue(p, []);                shot();   // пусто — блок спрятан
drawQueue(p, ['раз']);           shot();
const was = q.innerHTML;
q.innerHTML = 'ТРОНУЛИ';                   // тот же состав — не пересобираем
drawQueue(p, ['раз']);
const kept = q.innerHTML === 'ТРОНУЛИ';
q.innerHTML = was;
drawQueue(p, ['раз', 'два']);    shot();
drawQueue(p, []);                shot();   // очередь ушла — блок спрятан и пуст
console.log(JSON.stringify([seen, kept]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    seen, kept = json.loads(done.stdout)

    strip = '<div class="msg user pending">%s</div>'
    assert seen[0] == [True, ""]                          # пустая очередь — блок спрятан
    assert seen[1] == [False, strip % "раз"]
    assert kept, "тот же состав не должен пересобирать узлы — внутри выделяют текст"
    assert seen[2] == [False, (strip % "раз") + (strip % "два")]   # порядок исполнения
    assert seen[3] == [True, ""]                          # очередь ушла — блок пуст и скрыт


def test_style_comments_do_not_leak_prose_into_rules():
    """Комментарий, закрывшийся раньше текста, уносит с собой следующее правило.

    Так пропала кнопка «вниз» (2026-10-05): четыре строки пояснения оказались снаружи
    `*/`, браузер прочитал их как селектор и съел весь блок `.down` — кнопка потеряла
    `position:absolute` и уехала во флексбоксе вбок, а лог поделил с ней ширину.
    Ни `node --check`, ни тесты этого не видели: CSS никто не разбирал.

    Проверяем два следствия разом — баланс комментариев и отсутствие прозы в селекторах.
    Кириллица в заголовке правила бывает только одним способом: текст вытек наружу.
    """
    css = slice_out("style")
    assert "/*" in css and css.count("/*") == css.count("*/"), "комментарии не сошлись"

    naked = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "*/" not in naked and "/*" not in naked, "комментарий закрыт не там, где открыт"

    for chunk in naked.split("}"):
        head = chunk.rsplit("{", 1)[0] if "{" in chunk else chunk
        assert not re.search(r"[а-яА-ЯёЁ]", head), f"проза в селекторе: {head.strip()[:80]}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_attachment_paths_go_after_the_text(tmp_path):
    """Пути в конце промпта: claude узнаёт `/команду` только первым словом.

    До 2026-10-13 пути стояли первыми, и `/refine` с картинкой уходил обычным текстом —
    скилл не запускался. Вид тот же, что у подписи к файлу в Telegram (`_prompt` в app.py).
    """
    js = slice_out("script")
    fn = "const withPaths" + js.split("const withPaths")[1].split(";\n")[0] + ";"
    src = tmp_path / "paths.js"
    src.write_text(fn + """
console.log(JSON.stringify([
  withPaths('/refine поправь', ['/data/inbox/a.png', '/data/inbox/b.txt']),
  withPaths('', ['/data/inbox/a.png']),
  withPaths('просто текст', []),
]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(src)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    both, only, none = json.loads(done.stdout)

    assert both == "/refine поправь\n\n/data/inbox/a.png\n/data/inbox/b.txt"
    assert both.startswith("/refine")
    assert only == "/data/inbox/a.png"
    assert none == "просто текст"


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_diff_pairs_removed_and_added_lines(tmp_path):
    """Две колонки: подряд идущие `-` и `+` встают друг напротив друга, лишние — с пустой
    клеткой напротив, контекст в обеих. Номера строк — свои у каждой колонки."""
    js = slice_out("script")
    body = js.split("// --- diff:begin ---")[1].split("// --- diff:end ---")[0]
    src = tmp_path / "diff.js"
    src.write_text("const esc = (s) => s;\n" + body + """
const hunks = [{ o: 10, n: 20, lines: [' ctx', '-a', '-b', '+A', ' end', '\\\\ No newline'] },
               { o: 50, n: 61, lines: ['+x'] }];
const rows = diffRows(hunks).map(r => r === 'gap' ? 'gap'
  : r.map(c => c ? c[0] + ':' + c[1] + ':' + c[2] : '-'));
const wide = diffHTML({ hunks, more: 3, new: false }, false);
const narrow = diffHTML({ hunks, more: 0, new: false }, true);
const fresh = diffHTML({ hunks: [{ o: 0, n: 1, lines: ['+one'] }], more: 0, new: true }, false);
console.log(JSON.stringify({ rows, wide, narrow, fresh }));
""", encoding="utf-8")
    done = subprocess.run(["node", str(src)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    assert got["rows"] == [
        ["10:ctx:", "20:ctx:"],
        ["11:a:del", "21:A:add"],      # замена: строка против строки
        ["12:b:del", "-"],             # лишняя удалённая — пустая клетка справа
        ["13:end:", "22:end:"],
        "gap",                          # между кусками — разрыв
        ["-", "61:x:add"],
    ]
    assert "ещё 3 строк" in got["wide"] and 'class="dv"' in got["wide"]
    # Узко — одна колонка: контекст один раз, удалённое над добавленным.
    assert 'class="dv one"' in got["narrow"]
    assert got["narrow"].count(">ctx<") == 1
    assert got["narrow"].index(">a<") < got["narrow"].index(">A<")
    # Новый файл — одна зелёная колонка без пустой левой половины.
    assert 'class="dv one"' in got["fresh"] and "none" not in got["fresh"]


def test_clamped_io_has_no_peeking_next_line():
    """Отступ вокруг текста окошка — внешний. `line-clamp` задаёт высоту, но не прячет
    следующие строки, а `overflow:hidden` режет по краю внутреннего отступа: пока отступ
    был `padding`, в его нижние пиксели заглядывала верхушка второй строки IN.
    Раскрывается кликом только обрезанное или уже раскрытое окошко."""
    css, js = slice_out("style"), slice_out("script")
    rule = css.split(".io pre { ", 1)[1].split("}", 1)[0]
    assert "padding:0" in rule and "margin:6px 10px" in rule
    assert "-webkit-line-clamp:1" in rule
    assert ".io.out pre { -webkit-line-clamp:3" in css
    assert "closest('.io.more, .io.open')" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_busy_pane_shows_stop_and_queue(tmp_path):
    """Окно с живым прогоном и ждущим промптом: класс `busy` (он показывает «стоп» и
    таймер) и полоса очереди. Так видно и ожидание фонового агента — процесс жив, окно
    занято. 2026-10-14 `tickPane` падал на `running++`, оставшемся от цикла в `tick`:
    окно выглядело свободным, а промпт, вставший в очередь, исчезал с экрана.
    """
    js_src = slice_out("script")
    start = js_src.index("function tickPane(")
    body = js_src[start:js_src.index("\n}\n", start) + 3]
    js = tmp_path / "tick.js"
    js.write_text("""
const cls = new Set();
const el = { querySelector: (s) => s === 'header' ? {} : s === '.timer' ? timer : null,
             classList: { toggle: (c, on) => on ? cls.add(c) : cls.delete(c),
                          remove: (c) => cls.delete(c), contains: (c) => cls.has(c) } };
const timer = { textContent: '', title: '' };
const document = { getElementById: () => el, querySelector: () => null, hidden: false };
const EventSource = { CLOSED: 2 };
const streams = new Map([['x', { readyState: 1 }]]);
const hints = new Map(), lastElapsed = new Map();
const dead = false, BLANK = ' ';
const fmt = (s) => s + 'с';
let drawn = null;
const drawQueue = (p, q) => { drawn = q; };
const sweepEchoes = () => {}, showOnce = () => {};
const shownErr = 0, shownLocal = 0, shownStats = 0;
""" + body + """
const p = { pane: 'x', session: 's' };
const busy = tickPane(p, { runs: [{ scope: 'web:x', secs: 5, session: 's' }],
                           queued: { 'web:x': ['точно запущен?'] } });
console.log(JSON.stringify([busy, [...cls], drawn, timer.textContent]));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [True, ["busy"], ["точно запущен?"], "5с +1"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_log_does_not_yank_a_reader_down(tmp_path):
    """Дописанная строка прокручивает лог вниз только тому, кто внизу и был.

    Итог прогона (`✓ модель · время`) пишет тик, и раньше `log()` прокручивал вниз всегда:
    прогон кончался, пока человек читал историю выше, — и лог уезжал из-под глаз
    (2026-10-15). Свой промпт — исключение: отправил, значит хочешь видеть, что ушло.
    Убранное `trim` сверху в Safari сдвигало текст вверх — там позиция возвращается руками.
    """
    js = slice_out("script")
    at_end = "const atEnd" + js.split("const atEnd")[1].split(";\n")[0] + ";"
    log_fn = "function log" + js.split("function log(")[1].split("\n}\n")[0] + "\n}\n"
    log_fn = log_fn.replace("function log", "function log(", 1) if not log_fn.startswith("function log(") else log_fn
    trim_fn = "function trim" + js.split("function trim(")[1].split("\n}\n")[0] + "\n}\n"
    trim_fn = "function trim(" + trim_fn[len("function trim"):]
    src = tmp_path / "scroll.js"
    src.write_text("""
// Лог: сто строк по 20px, окно в 200px. Вставка и удаление меняют высоту содержимого.
const mk = (top) => ({ scrollTop: top, clientHeight: 200, n: 100,
  get scrollHeight() { return this.n * 20; },
  get children() { return { length: this.n }; },
  get firstElementChild() { const b = this; return { remove() { b.n--; } }; },
  insertAdjacentHTML() { this.n++; }, get lastElementChild() { return {}; } });
let box;
const document = { querySelector: () => box };
""" + at_end + "\n" + log_fn + """
const LOG_MAX = 100;
""" + "\nconst run = (anchors) => {\nconst ANCHORS = anchors;\n" +
        trim_fn.replace("function trim(", "const trim = function (") .replace("\n}\n", "\n};\n") + """
return trim; };
const out = {};
box = mk(1800); log({ pane: 'a' }, 'x'); out.bottom = box.scrollTop;            // был внизу
box = mk(400);  log({ pane: 'a' }, 'x'); out.reading = box.scrollTop;           // читал выше
box = mk(400);  log({ pane: 'a' }, 'x', true); out.own = box.scrollTop;        // свой промпт
box = mk(400); box.n = 103; run(false)(box); out.safari = box.scrollTop;        // 3 строки ушли
box = mk(400); box.n = 103; run(true)(box);  out.chrome = box.scrollTop;
console.log(JSON.stringify(out));
""", encoding="utf-8")
    done = subprocess.run(["node", str(src)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)

    assert got["bottom"] == 1e9          # внизу — едет за новым
    assert got["reading"] == 400         # читал выше — остаётся на месте
    assert got["own"] == 1e9             # свой промпт — вниз всегда
    assert got["safari"] == 400 - 3 * 20  # ушло 60px сверху — позиция возвращена
    assert got["chrome"] == 400          # браузер якорит сам — не трогаем


def test_just_sent_prompt_goes_where_it_belongs():
    """Копия только что отправленного промпта: у занятого окна — в блок очереди под логом,
    у свободного — обычным промптом в лог. До 2026-10-16 она всегда шла в лог с классом
    очереди: промпт мелькал в окне и прыгал вниз, а у свободного окна стоял в логе
    бледным обрезанным блоком очереди, хотя уже выполнялся."""
    js = slice_out("script")
    body = js.split("async function send(")[1].split("\n}\n")[0]
    assert "classList.contains('busy')" in body
    assert "querySelector(':scope > .queue')" in body
    # В очередь — с классом очереди, и ключ перерисовки сброшен, чтобы сервер её сменил.
    assert "delete q.dataset.j" in body
    # В лог — обычным промптом, без `pending`.
    assert 'log(p, `<div class="msg user">' in body
    assert 'log(p, `<div class="msg user pending">' not in body
