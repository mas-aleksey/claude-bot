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


def test_page_has_both_halves():
    """Разбор по тегам молча отдал бы пустую строку, и проверка выше стала бы холостой."""
    assert "function drawPane" in slice_out("script")
    assert "grid-column:var(--c,1)" in slice_out("style")


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_search_spans_markup_and_line_breaks(tmp_path):
    """Поиск идёт по плоскому тексту панели: фраза, разорванная тегом или переносом,
    обязана находиться — ровно этого не умел прежний поиск по отдельным узлам."""
    body = slice_out("script").split("// --- find:begin ---")[1].split("// --- find:end ---")[0]
    js = tmp_path / "find.js"
    js.write_text(body + """
console.log(JSON.stringify([
  hits('функция linkify чинит', 'функция linkify'),
  hits('строка один\\nстрока два', 'один строка'),
  hits('цена 5$ за (штуку)', '5$ за (штуку)'),
  hits('Слово и слово', 'СЛОВО'),
  hits('что угодно', '   '),
]));""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got == [[[0, 15]], [[7, 18]], [[5, 18]], [[0, 5], [8, 13]], []]


@pytest.mark.skipif(shutil.which("node") is None, reason="node нужен только для этой проверки")
def test_polling_stops_after_a_run_of_failures(tmp_path):
    """Вкладка без сессии SSO молотила вечно: каждый запрос — редирект на вход и новая
    кука состояния там. Серия отказов обязана останавливать опрос, а один отказ при
    живом сервере — нет."""
    body = slice_out("script").split("// --- dead:begin ---")[1].split("// --- dead:end ---")[0]
    js = tmp_path / "dead.js"
    js.write_text("""
let banner = true, title = '';
const $ = () => ({ set hidden(v) { banner = v; } });
const document = { set title(v) { title = v; } };
let answer = 'fail';
const fetch = () => answer === 'ok' ? Promise.resolve({ ok: true, json: () => 42 })
                                    : Promise.reject(new TypeError('failed to fetch'));
""" + body + """
const hit = async (mode) => { answer = mode; await get('x').catch(() => {}); };

(async () => {
  for (let i = 0; i < DEAD - 1; i++) await hit('fail');
  const beforeLimit = dead;         // серия ещё не добрана — опрос жив
  await hit('ok');                  // успех сбрасывает серию
  for (let i = 0; i < DEAD - 1; i++) await hit('fail');
  const afterReset = dead;          // значит до предела снова не хватает одного
  for (let i = 0; i < 1; i++) await hit('fail');
  console.log(JSON.stringify([beforeLimit, afterReset, dead, banner, title]));
})();
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [False, False, True, False, "⚠ claude"]


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
const document = { querySelectorAll: () => rows };
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

panes = [];                               // закрыли, ничего не идёт
trackRuns([]);
markList(); seen.push(state());
console.log(JSON.stringify(seen));
""", encoding="utf-8")
    done = subprocess.run(["node", str(js)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    running, finished, stored, opened, forgotten = json.loads(done.stdout)
    assert running == [[["busy"], 25], [[], None]]      # мигает, но не залита
    assert finished == [[["done"], 25], [[], None]]     # точка, цвет тот же
    assert stored == ["a"]                              # переживёт F5
    assert opened == [[["open"], 25], [[], None]]       # заливка, точка снята
    assert forgotten == [[[], None], [[], None]]        # цвет забыт, карта не растёт


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
