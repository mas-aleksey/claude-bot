const $ = (id) => document.getElementById(id);

// Порог узкого экрана. То же число стоит в @media выше: вёрстка там раскладывает панели
// столбиком и перебивает сетку, а скрипт по этому же признаку отключает перетаскивание.
const NARROW = matchMedia('(max-width: 700px)');

// Экранная клавиатура: `Shift` на ней нажать нечем, поэтому Enter там переносит строку,
// а отправляет кнопка. Признак — указатель, а не ширина: телефон в альбомной шире 700px,
// а ноутбук с тачскрином остаётся `fine` (тач у него виден только в `any-pointer`).
const TOUCH = matchMedia('(pointer: coarse)');

// Вкладка стучится на сервер вечно, и хуже всего это выглядит при истёкшей сессии SSO:
// каждый запрос уходит редиректом на вход и выписывает там куку состояния. Довести вход
// из XHR всё равно нельзя: OIDC требует перехода верхнего уровня, то есть перезагрузки
// страницы. Поэтому после серии отказов вкладка встаёт и зовёт человека.
//
// Счётчик в `get`, а не в `tick`: через него ходят и статус, и список сессий, и поиск,
// и любой из них одинаково молотит впустую. Успех любого запроса сбрасывает серию —
// одиночный таймаут при живом сервере вкладку не роняет. Потоки SSE сюда не попадают,
// но `dead` гасит и их: браузер переподключает поток сам и остановить его больше нечем.
// --- dead:begin ---
const DEAD = 5;  // подряд неудачных запросов, примерно пятнадцать секунд
let fails = 0;
let dead = false;

// Ответ сервера или отказ. Редирект на вход `fetch` проходит молча и отдаёт 200 со
// страницей входа: по `r.ok` это успех, json там нет, и вкладка оставалась белой —
// данные не пришли, а сказать об этом было некому. Тип ответа тут единственный честный
// признак. Серии ждать незачем: html вместо json — это точно вход, а не помеха связи.
// Заголовок вкладки приходит из шаблона со стороны сервера — это имя инстанса. Держим
// его здесь, чтобы плашка обрыва могла вернуть имя на место.
const TITLE = document.title;

const payload = (r) => {
  if (!r.ok) throw r.status;
  if (!(r.headers.get('content-type') || '').includes('json')) {
    if (!dead) offline();
    throw 'вход';
  }
  return r.json();
};

const get = (u) => fetch(u).then(payload)
  .then((v) => { fails = 0; return v; },
        (e) => { if (++fails >= DEAD && !dead) offline(); throw e; });

// Кнопка, а не автоматическая перезагрузка: панель могла быть не пуста, а перезагрузка
// посреди набранного промпта — потеря работы.
function offline() {
  dead = true;
  // Имя инстанса в заголовке ставит сервер, поэтому берём то, что есть, а не пишем
  // «claude» заново: иначе после обрыва связи вкладка теряла бы, чья она.
  document.title = '⚠ ' + TITLE;
  $('dead').hidden = false;
}
// --- dead:end ---
const post = (u, body) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body) }).then(payload);

// POST, который сам объясняет отказ. Ручки дерева и списка пишут причину словами —
// «папка не пуста», «такое имя уже занято», «над сессией идёт прогон», — и показать
// вместо неё код значит потерять единственное, что объясняет отказ. Отсюда своя обёртка
// вместо `post`: тот отдаёт только статус и молчит о тексте.
//
// Неудача возвращает null и уже сказала человеку; вызывающему остаётся один `if`. До
// 2026-09-25 это были четыре копии, и все четыре разошлись в мелочах.
async function ask(url, body, what) {
  const r = await fetch(url, { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });
  const text = await r.text();
  if (!r.ok) { alert(text || (what + ': ' + r.status)); return null; }
  try { return JSON.parse(text); } catch (e) { return {}; }
}

// Буфер обмена одной строкой. Три места копируют в него (путь в шапке окна, путь в
// дереве, блок кода в логе), и у каждого был свой способ промолчать при отказе.
const copy = async (text) => {
  try { await navigator.clipboard.writeText(text); return true; }
  catch (e) { return false; }
};
const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));

// Усилие новых окон. `high` — осознанный выбор человека, а не дефолт CLI: тот не назван
// ни в `--help`, ни в событии init, и молча меняться может с версией.
const EFFORT = 'high';

// --- hue:begin ---
// Оттенок окна — середина самого широкого свободного промежутка на круге. Насыщенность
// и светлота заданы в CSS одинаковыми для всех, поэтому панели отличаются только тоном
// и выглядят одной семьёй.
//
// Список из шести тонов стоял тут до 2026-09-26 и держал две пары в 55° друг от друга —
// синий с пурпурным и бирюзовый с синим. Седьмое окно и вовсе повторяло чужой тон:
// список кончался, и брался остаток по числу панелей. Шаг золотого угла, стоявший
// следом, разносил хуже: он не смотрит, где именно пусто.
//
// Занятыми считаются цвета открытых окон и цвета закрытых сессий: карта `hues` живёт
// дольше окна, и вернувшаяся сессия обязана мигать тем же оттенком.
// MIN_GAP — с какого расстояния два окна считаются одноцветными. Не 40: восемь окон на
// круге столько не держат (там выходит 22), и порог повыше гонял бы перекраску по кругу.
const HUE0 = 250, MIN_GAP = 20;
const arc = (a, b) => { const d = Math.abs(a - b) % 360; return Math.min(d, 360 - d); };
const freeHue = () => {
  const used = [...panes.map(x => x.hue), ...Object.values(hues)]
    .filter(h => typeof h === 'number').sort((a, b) => a - b);
  if (!used.length) return HUE0;
  let best = (used[0] + 180) % 360, wide = -1;
  used.forEach((a, i) => {
    const gap = (((used[(i + 1) % used.length] - a) % 360) + 360) % 360 || 360;
    if (gap > wide) { wide = gap; best = Math.round(a + gap / 2) % 360; }
  });
  return best;
};
// Разъехавшиеся оттенки чиним один раз, на загрузке страницы. Раскладка живёт в
// localStorage, и цвета в ней бывают из прежней палитры, из копии между вкладками или
// просто повторяются, если их выдавал старый код. Позже перекрашивать нельзя: окно, у
// которого цвет сменился на глазах, человек теряет из виду.
//
// Первому из совпавших цвет оставляем, двигаем следующих: так меняется меньше окон, и
// то, на которое человек смотрел, остаётся прежним.
//
// Работает по `panes` и только по ним: `freeHue` смотрит туда же, и на чужом списке
// двое переназначенных могли бы получить один цвет — он бы их просто не видел.
function spreadHues() {
  const seen = [];
  for (const p of panes) {
    if (typeof p.hue !== 'number' || seen.some(h => arc(h, p.hue) < MIN_GAP)) {
      p.hue = undefined;
      p.hue = freeHue();
    }
    seen.push(p.hue);
  }
}
// --- hue:end ---

// Панели переживают F5: в них лежит id, который на сервере служит скоупом запуска,
// поэтому после перезагрузки «стоп» бьёт по своему прогону, а не по чужому.
// Аварийный выход: `?reset` в адресе стирает сохранённые окна и открывает панель
// чистой. Раскладка — единственное состояние, которое портит вид раньше, чем до кнопок
// можно дотянуться: 18.09.2026 белый экран пришлось лечить инспектором, другого пути
// не было. Стираем до чтения — ниже `panes` уже разобран, и сброс опоздал бы. Адрес
// чистим сразу: иначе следующий F5 стёр бы раскладку заново.
if (new URLSearchParams(location.search).has('reset')) {
  localStorage.removeItem('panes');
  history.replaceState(null, '', location.pathname);
}
let panes = JSON.parse(localStorage.getItem('panes') || '[]');
const save = () => localStorage.setItem('panes', JSON.stringify(panes));

// Отправленный промпт панель печатает сразу, а через секунды он же приезжает из
// транскрипта. Держим его тут, чтобы снять дубль. Не в самой панели: она уходит в
// localStorage, и после F5 залипшее эхо съело бы строку из истории.
const echoes = new Map();

// Занятость панели на прошлом тике и серая подсказка после прогона. Не в самой панели по
// той же причине, что и эхо выше: `panes` уходит в localStorage, и после F5 залипшее
// `busy` выдало бы подсказку на пустом месте, а залипшая подсказка вставлялась бы по Tab,
// не показавшись серым.
const wasBusy = new Set();
const hints = new Map();
// Последний ответ панели текстом: из него после прогона достаётся подсказка.
// Разметка лога для этого не годится — там уже HTML со ссылками и подсветкой.
// Имя не `said`: так зовут локальную переменную в цикле панелей, и одноимённый `const`
// ниже по блоку накрывал бы обращение отсюда мёртвой зоной.
const answers = new Map();

// --- echo:begin ---
// Сравниваем по схлопнутым пробелам: слеш-команда возвращается из транскрипта собранной
// заново из `<command-name>` и `<command-args>`, и лишний пробел или перенос между
// командой и текстом делал строки разными. Набранное человеком и пересобранное claude
// совпадают только с точностью до пробелов.
const norm = (s) => String(s).replace(/\s+/g, ' ').trim();

// Дубли своих промптов. Ищем по всей очереди, а не только в голове: промпт, который до
// транскрипта не доехал (отменён из очереди, съеден ошибкой), застревал первым и глушил
// сверку для всех следующих — с этого момента каждый промпт панели печатался дважды.
//
// Запись — `{text, at, nodes}`: время отправки нужно уборке ниже, узлы — вызывающему.
//
// Возвращаем совпавшие записи, а не отфильтрованные элементы. До 2026-09-29 было
// наоборот: из транскрипта выбрасывался промпт, а на экране оставалась локальная копия
// на своём месте — то есть в момент отправки. Три промпта подряд вставали тремя пузырями
// сверху, а ответы ложились в конец, и порядок чинился только перезагрузкой. Теперь место
// в логе задаёт транскрипт, а локальную копию снимает вызывающий.
//
// Функция осталась чистой: DOM трогает `absorb`, и её node-тест не тянет за собой
// подставную страницу.
function dropEcho(items, queue) {
  const hit = [];
  for (const it of items) {
    if (it.role !== 'user' || !queue.length) continue;
    const at = queue.findIndex(e => e.text === norm(it.text));
    if (at >= 0) hit.push(queue.splice(at, 1)[0]);
  }
  return hit;
}

// Уборка застрявших записей. Запись снимается встречей в транскрипте, а промпт, который
// туда не попал, оставался в памяти вкладки навсегда и однажды съедал законный повтор
// того же текста: на экране оставалась локальная копия, а после F5 не оставалось ничего.
//
// Чистим не по одному таймеру: промпт может ждать в очереди сколько угодно, и его строка
// появится только когда до него дойдёт очередь — сорок минут ожидания выглядели бы как
// «протухло». Поэтому два условия вместе: в панели ничего не идёт и очередь пуста, то
// есть всё, что могло попасть в транскрипт, уже попало, — и записи больше ECHO_IDLE.
// Задержка тут страховка от гонки: между отправкой и появлением прогона в `/api/status`
// проходит секунда-другая, и без неё эхо снималось бы раньше, чем начался запуск.
const ECHO_IDLE = 30000;
function sweepEchoes(pane, idle, now) {
  const queue = echoes.get(pane);
  if (!queue || !idle) return;
  const left = queue.filter(e => now - e.at < ECHO_IDLE);
  if (left.length) echoes.set(pane, left);
  else echoes.delete(pane);
}
// --- echo:end ---
// Показанная ошибка — чтобы не перерисовывать её на каждом тике.
const shownErr = new Map();
// Показанный ответ местной команды — по той же причине: он приходит в каждом ответе
// /api/status, пока в панели не начнут следующий запуск.
const shownLocal = new Map();
// Показанный итог прогона — по времени, а не по тексту: два одинаковых прогона подряд
// дают посимвольно равные строки, и сравнение текстов проглотило бы второй.
const shownStats = new Map();

// Список моделей на всю вкладку: один запрос за её жизнь. Каталог на сервере живёт
// шесть часов, и опрашивать его чаще, чем человек жмёт F5, незачем.
// Запасная тройка нужна ровно на случай, когда каталог не прочитался: панель без выбора
// модели хуже, чем панель с устаревшим выбором.
let MODELS = [];
const FALLBACK = [{ id: 'opus', name: 'opus' }, { id: 'sonnet', name: 'sonnet' },
                  { id: 'haiku', name: 'haiku' }];

// Чем ходит бот, когда в панели ничего не выбрано: `{id, name}` из каталога. Панель
// показывает эту модель как обычную строку списка, а не отдельным пунктом «общая» — для
// человека тут одна вещь, какая модель отвечает, а не две.
let botModel = null;

// Сохранённое значение, которого в каталоге нет (снятая модель, незнакомый алиас),
// остаётся отдельной строкой: молча подменить выбор панели значит соврать про то, чем
// она ходит. Пустой пункт живёт ровно до первого ответа сервера — пока модель бота
// неизвестна, показывать в списке нечего.
function fillModels(p, sel) {
  const list = MODELS.length ? MODELS : FALLBACK;
  const value = p.model || botModel?.id || '';
  const known = list.some(m => m.id === value);
  sel.innerHTML = (value ? '' : '<option value="">…</option>') +
    (value && !known ? `<option value="${esc(value)}">${esc(value)}</option>` : '') +
    list.map(m => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
  sel.value = value;
}

// Перерисовать выпадашки всех панелей: приехал каталог или сменилась модель бота.
function refillModels() {
  for (const p of panes) {
    const sel = document.getElementById('pane-' + p.pane)?.querySelector('.model');
    if (sel) fillModels(p, sel);
  }
}

async function loadModels() {
  try { MODELS = await get('api/models'); } catch (e) { return; }
  refillModels();
}

// --- tree:begin ---
// Сайдбар — дерево проектов, а не список одного из них. Выпадашка стояла тут до
// 2026-09-25 и была единственной дорогой к чужой сессии: переключил — потерял из виду
// всё остальное. Теперь проекты лежат строками, раскрытые показывают свои сессии, а «+»
// в строке заводит сессию именно в этом проекте.
//
// Раскрытые помним между заходами: набор путей в localStorage. Первый заход раскрывает
// последний использованный проект — тот самый ключ `proj`, что помнила выпадашка.
const open = new Set(JSON.parse(localStorage.getItem('open') || '[]'));
const saveOpen = () => localStorage.setItem('open', JSON.stringify([...open]));

// Последний ответ сервера: по нему перерисовываем дерево на раскрытии и сворачивании,
// не дёргая сервер. Данные те же, меняется только что показано.
let TREE = { projects: [], lists: [] };

// Списки сессий тянем только у раскрытых проектов: на каждую строку сервер читает
// транскрипт ради заголовка (12–18 мс на проект, замер 2026-09-25), а дерево
// перечитывается каждые пятнадцать секунд. Свёрнутой строке хватает числа сессий — его
// отдаёт `api/projects` перечислением каталога, без чтения файлов.
async function loadTree() {
  const ps = await get('api/projects');
  // Первый заход раскрывает последний использованный проект — тот самый ключ `proj`,
  // что помнила выпадашка. Решаем это до запроса списков, иначе раскрытый на этом же
  // проходе проект остался бы без содержимого до следующего тика.
  if (!open.size) {
    const saved = localStorage.getItem('proj');
    const first = ps.some(x => x.path === saved) ? saved : ps[0]?.path;
    if (first) { open.add(first); saveOpen(); }
  }
  const want = ps.filter(x => open.has(x.path));
  const lists = await Promise.all(want.map(
    x => get('api/sessions?project=' + encodeURIComponent(x.path)).catch(() => [])));
  TREE = { projects: ps, lists: Object.fromEntries(want.map((x, i) => [x.path, lists[i]])) };
  drawProjects();
}

// Строка сессии. `data-project` на самой строке, а не в замыкании: те же строки рисует
// поиск, а у него каждая может быть из своего проекта.
const rowHTML = (s, project, withName) =>
  `<button data-id="${s.id}" data-project="${esc(project)}" data-title="${esc(s.title)}">` +
  `<span class=meta>` +
    `<span class=rename title="переименовать">\u270e\ufe0e</span>` +
    `<span class=rm title="удалить сессию">\u2715</span>` +
    `<span class=ago>${esc(s.ago)}</span></span>` +
  `${esc(s.title.length > 42 ? s.title.slice(0, 42) + '\u2026' : s.title)}` +
  (withName ? `<span class=pj>${esc(project.split('/').pop())}</span>` : '') +
  (s.snippet ? `<span class=snip>${esc(s.snippet)}</span>` : '') + '</button>';

function drawProjects() {
  const { projects: ps, lists } = TREE;
  $('list').innerHTML = ps.map((x) => {
    const on = open.has(x.path);
    // Число у свёрнутого — с сервера, у раскрытого — длина списка: он показывает
    // тридцать свежих, и писать рядом с ними большее число было бы неправдой.
    const rows = lists[x.path];
    return `<div class=node><button class=head data-path="${esc(x.path)}">` +
      `<span class=add title="новая сессия в ${esc(x.name)}">+</span>` +
      `<span class=n>${rows ? rows.length : (x.sessions ?? 0)}</span>` +
      `<span class=caret>${on ? '\u25be' : '\u25b8'}</span> ${esc(x.name)}</button>` +
      (on ? `<div class=kids>` +
            ((rows || []).map(s => rowHTML(s, x.path)).join('') ||
             `<div class=none>${rows ? 'сессий нет' : '\u2026'}</div>`) + '</div>' : '') +
      '</div>';
  }).join('') || '<div class=none>проектов нет</div>';

  for (const h of $('list').querySelectorAll('.head')) {
    const path = h.dataset.path;
    h.onclick = () => {
      const on = !open.has(path);
      on ? open.add(path) : open.delete(path);
      saveOpen();
      // Свернуть — просто перерисовать. Раскрыть — сходить за списком: у свёрнутого его
      // в TREE нет, и до ответа в ветке стоит многоточие.
      drawProjects();
      if (on) loadTree().catch(() => {});
    };
    // «+» внутри строки проекта: всплытие обрываем, иначе заведение сессии заодно
    // сворачивало бы проект, из которого её завели.
    h.querySelector('.add').onclick = (e) => {
      e.stopPropagation();
      localStorage.setItem('proj', path);
      addPane({ pane: uid(), project: path, session: null, next: 0 });
    };
  }
  wireRows();
}
// --- tree:end ---

function wireRows() {
  for (const b of $('list').querySelectorAll('button[data-id]')) {
    const project = b.dataset.project;
    b.onclick = () => addPane({ pane: uid(), project, session: b.dataset.id, next: 0,
                                title: b.dataset.title });
    // Карандаш живёт внутри кнопки, поэтому всплытие обрываем: иначе переименование
    // заодно открывало бы сессию в новой панели. Диалог ввода браузерный — своей формы
    // ради одной строки текста тут не надо. Обновляем через `runFind`, а не
    // `loadTree`: он сам знает, дерево сейчас на экране или результаты поиска.
    b.querySelector('.rename').onclick = async (e) => {
      e.stopPropagation();
      const name = prompt('имя сессии, пустое снимет', b.dataset.title);
      if (name === null) return;
      await post('api/name', { session: b.dataset.id, name });
      runFind();
    };
    // Удаление необратимо, поэтому спрашиваем именем сессии, а не «вы уверены?»:
    // строки в списке похожи, и промах мышью по соседней стоил бы транскрипта.
    // Панели этой сессии закрываем — читать им больше нечего.
    b.querySelector('.rm').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm('удалить сессию «' + b.dataset.title.slice(0, 60) + '»? это навсегда'))
        return;
      if (!await ask('api/drop', { project, session: b.dataset.id }, 'не удалить')) return;
      panes.filter(x => x.session === b.dataset.id).forEach(closePane);
      runFind();
    };
  }
  markList();
  syncTitles();
}

// --- mark:begin ---
// Строка сессии в списке слева несёт три разных вещи, и они складываются:
//   заливка   — эта сессия открыта в панели, вот она на экране;
//   мигание   — над сессией идёт запуск, чей угодно: панели, закрытой панели, Telegram;
//   точка     — запуск кончился, а окна не было, то есть ответ никто не видел.
// Метки кладём отдельным проходом, а не в разметку строки в `rowHTML`: дерево
// перерисовывается целиком каждый пятый тик, и вписанное в разметку живёт до него.
const busySessions = new Set();
// Цвет переживает панель: закрыли окно, а строка обязана мигать тем же оттенком. Карта
// маленькая по построению — цвет держим ровно пока он нужен, чистка в `trackRuns`.
const hues = JSON.parse(localStorage.getItem('hues') || '{}');
// Непросмотренные ответы переживают F5: метку снимает только открытие сессии.
const done = new Set(JSON.parse(localStorage.getItem('done') || '[]'));
const saveMarks = () => {
  localStorage.setItem('hues', JSON.stringify(hues));
  localStorage.setItem('done', JSON.stringify([...done]));
};

const canNotify = () => 'Notification' in window && Notification.permission === 'granted';

// Ответ пришёл в закрытое окно: на экране нет ни панели, ни таймера, только точка в
// строке списка — а её легко не заметить и на видимой вкладке. Поэтому проверки
// `document.hidden` тут нет, в отличие от `notifyDone` про открытые панели.
// Заголовок берём из строки списка: проект и название ушли вместе с панелью, а строка
// несёт своё название в `data-title`. Строки может не быть — выбран другой проект или
// сессия не попала в тридцать свежих. Тогда сообщаем без названия.
function notifyClosed(session) {
  if (!canNotify()) return;
  const row = document.querySelector('#list button[data-id="' + session + '"]');
  new Notification('claude · ответ готов',
    { body: row?.dataset.title?.slice(0, 80) || 'окно было закрыто', tag: session });
}

// Живые запуски сервер отдаёт целиком, с id сессии у каждого. Панели тут не при чём:
// сопоставление с ними ничего не даёт, а мигать должна любая занятая сессия.
function trackRuns(runs) {
  const live = new Set(runs.map(r => r.session).filter(Boolean));
  for (const id of live) {
    done.delete(id);                       // снова работает — прошлый ответ уже неважен
    if (!(id in hues)) hues[id] = freeHue();
  }
  // Занятость пропала, а окна нет: ответ пришёл в пустоту, о нём и сообщает точка.
  for (const id of busySessions)
    if (!live.has(id) && !panes.some(x => x.session === id)) { done.add(id); notifyClosed(id); }
  busySessions.clear();
  for (const id of live) busySessions.add(id);
  // Цвет забываем, как только он перестал быть нужен. Иначе карта растёт, а `freeHue`
  // видит все шесть оттенков занятыми и начинает выдавать совпадающие.
  for (const id of Object.keys(hues))
    if (!live.has(id) && !done.has(id) && !panes.some(x => x.session === id)) delete hues[id];
  saveMarks();
}

function markList() {
  for (const b of document.querySelectorAll('#list button[data-id]')) {
    const id = b.dataset.id;
    const p = panes.find(x => x.session === id);
    const hue = p ? (p.hue ?? HUE0) : hues[id];
    // Готовность зеркалим с самой панели, своей памяти не заводим: состояние одно, и
    // снимается оно в одном месте — `raise`, то есть кликом по окну. Развёрнутое во весь
    // экран окно закрывает соседей, и штриховка готового оказывается под ним; строка
    // списка — единственное место, где её видно.
    const el = p && document.getElementById('pane-' + p.pane);
    const ready = !!el && el.classList.contains('ready');
    const bad = ready && el.classList.contains('bad');
    b.classList.toggle('open', !!p);
    b.classList.toggle('busy', busySessions.has(id));
    b.classList.toggle('done', done.has(id));
    b.classList.toggle('ok', ready && !bad);
    b.classList.toggle('bad', bad);
    b.title = bad ? 'прогон упал'
            : ready ? 'прогон закончился'
            : done.has(id) ? 'ответ пришёл, пока окно было закрыто' : '';
    if (hue !== undefined) b.style.setProperty('--hue', hue);
    else b.style.removeProperty('--hue');
  }
}

// Заголовок панели был снимком на момент её открытия, а имя сессии живое: сервер берёт
// его из последнего промпта в транскрипте. Отсюда расхождение — строка слева менялась
// после каждого промпта, в панели висел текст, с которым её открыли.
//
// Догоняем на том же обновлении списка: другого источника свежего имени у панели нет,
// а список и так перечитывается с сервера каждый пятый тик. Отдельным проходом, а не
// внутри `markList`: тот кладёт метки на строки, а это обратное направление — из списка
// в панель.
function syncTitles() {
  let changed = false;
  for (const b of document.querySelectorAll('#list button[data-id]')) {
    const p = panes.find(x => x.session === b.dataset.id);
    if (!p || !b.dataset.title || p.title === b.dataset.title) continue;
    p.title = b.dataset.title;
    setWho(p);
    changed = true;
  }
  if (changed) save();  // один раз на проход: `panes` уезжает в localStorage целиком
}
// --- mark:end ---

// Поиск идёт по всем проектам сразу: вопрос «где я это обсуждал» иначе не отвечается,
// а именно он и гонял в выпадашку. Сервер сканирует транскрипты и отдаёт фрагмент вокруг
// попадания. Дебаунс, потому что скан хоть и быстрый, но не на каждую букву.
let findTimer = null;

function scheduleFind() {
  clearTimeout(findTimer);
  findTimer = setTimeout(runFind, 300);
}

async function runFind() {
  const q = $('find').value.trim();
  if (!q) return loadTree();
  try {
    const found = await get('api/search?q=' + encodeURIComponent(q));
    // Результаты плоским списком, у каждой строки подпись проекта: раскладывать их
    // обратно по веткам значит прятать половину найденного под свёрнутыми узлами.
    $('list').innerHTML = found.map(s => rowHTML(s, s.project, true)).join('') ||
      '<div class=none>ничего не нашлось</div>';
    wireRows();
    markList();
    syncTitles();
  } catch (e) { /* следующий ввод попробует снова */ }
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

// --- place:begin ---
// Разворот на всю область и возврат. Прежний прямоугольник живёт в самой панели,
// поэтому переживает F5: развёрнутое окно и после перезагрузки знает, куда вернуться.
// Отдельного режима нет — это обычная геометрия, и перетащить развёрнутое окно или
// потянуть его за угол можно так же, как любое другое. Любая такая правка руками стирает
// память о прежнем размере: возвращать после неё некуда.
function zoom(p) {
  if (p.prev) { Object.assign(p, p.prev); p.prev = null; }
  else { p.prev = { c: p.c, r: p.r, w: p.w, h: p.h }; p.c = p.r = 1; p.w = COLS; p.h = ROWS; }
  applyGeom(p);
  drawZoom(p);
}

// Целевая пропорция окна, ширина к высоте: в окне чат, и узкое высокое читается лучше
// приземистого. Это калибровка, а не закон — число меняет всю раскладку разом.
const CELL = 0.8;

// Плитка на всех: столбцов столько, чтобы окно вышло ближе к CELL на этом экране.
// Нужна ровно там, где подбор места бессилен: четыре окна по четверти занимают сетку
// целиком, и пятому некуда встать, как его ни ужимай. Расставляет и уже открытые —
// молча ложиться поверх них хуже, чем подвинуть их один раз на глазах.
//
// Корень из числа окон, стоявший тут раньше, про экран не знал: на широком мониторе он
// клал приземистые окна, а остаток клеток терял вовсе. При семи окнах floor(8/3) давал
// высоту 2 на три ряда — снизу оставались две пустые полосы.
function retile() {
  const n = panes.length;
  if (!n) return;
  const box = $('panes');
  const aspect = (box.clientWidth || 1) / (box.clientHeight || 1);
  let cols = 1, best = Infinity;
  for (let k = 1; k <= Math.min(n, COLS); k++) {
    const rows = Math.ceil(n / k);
    const tail = n - k * (rows - 1);   // окон в последнем ряду, он бывает неполным
    const off = (wide) => Math.abs(Math.log(aspect * rows / wide / CELL));
    // Худшая клетка, а не средняя: неполный ряд растягивается на всю ширину, и его окна
    // выходят вдвое шире остальных. Средняя такой перекос прощала — шесть окон ложились
    // 4 + 2 вместо ровных 3 + 3, потому что четыре правильные клетки перевешивали две
    // кривые. Минимакс выбирает раскладку, где нет ни одного окна не в масть.
    const worst = Math.max(off(k), off(tail));
    if (worst < best) { best = worst; cols = k; }
  }
  cols = Math.max(cols, Math.ceil(n / ROWS));   // рядов больше, чем клеток, не бывает
  const rows = Math.ceil(n / cols);
  // Границы долями от сетки, а не шагом в целых клетках: остаток от 12 и 8 раздаётся
  // соседям, и правый край с нижним заняты до конца.
  const edge = (i, k, total) => 1 + Math.round(i * total / k);
  panes.forEach((x, i) => {
    const row = Math.floor(i / cols);
    // В последнем ряду окон может быть меньше — растягиваем их на всю ширину, иначе
    // справа зияет дыра в те самые клетки, которых не хватило.
    const wide = row === rows - 1 ? n - cols * row : cols;
    const at = i - cols * row;
    x.c = edge(at, wide, COLS); x.w = edge(at + 1, wide, COLS) - x.c;
    x.r = edge(row, rows, ROWS); x.h = edge(row + 1, rows, ROWS) - x.r;
    x.prev = null;
    applyGeom(x);
    drawZoom(x);
  });
}
// --- place:end ---

function drawZoom(p) {
  const el = document.getElementById('pane-' + p.pane);
  const btn = el?.querySelector('.max');
  if (!btn) return;
  el.classList.toggle('zoomed', !!p.prev);
  btn.title = p.prev ? 'вернуть прежний размер' : 'во весь экран';
}

// --- raise:begin ---
function raise(el) {
  // Развёрнутое во весь экран окно сворачивается, когда поднимают другое. Иначе оно
  // остаётся во всю область, а поверх него ложатся окна в клетках — каша, в которой
  // непонятно, что развёрнуто и почему соседи выглядят обрезками. Само развёрнутое от
  // касания не сворачивается: сравниваем элементы, а не панели.
  for (const x of panes) {
    if (!x.prev) continue;
    const other = document.getElementById('pane-' + x.pane);
    if (other && other !== el) { zoom(x); save(); }
  }
  document.querySelectorAll('#panes section.act').forEach(s => s.classList.remove('act'));
  el.classList.add('act');
  // Подняли окно — значит увидели его ответ. Снимаем здесь, а не по клику в лог: подъём
  // случается от любого касания окна, и другого определения «посмотрел» у нас нет.
  el.classList.remove('ready', 'bad');
}
// --- raise:end ---

// Одна механика на перенос и на растягивание: и то и другое меняет прямоугольник панели
// в клетках. `edge` пуст для переноса, иначе содержит буквы сторон, за которые тянут.
// Pointer events, а не HTML5 drag-and-drop: последний в Safari работает через пень-колоду,
// а пальцем не работает вообще.
function wireGrab(p, el, node, edge) {
  node.onpointerdown = (e) => {
    // Кнопки и ссылки в заголовке («стоп», «×», «скачать») не должны запускать перенос:
    // preventDefault ниже съедает их click. Ссылку сюда добавили не сразу, и скачивание
    // молча не работало — жест переноса отменял переход по ней.
    if (e.button || e.target.closest('button, a')) return;
    // На узком экране сетка перебита `!important`, и перенос там ничего не двигал —
    // зато молча писал новые `c`/`r` в панель, и перекос вылезал на большом экране.
    // Проверяем в момент жеста, а не при создании: окно поворачивают и меняют размер.
    if (NARROW.matches) return;
    // Цель запоминаем сейчас: `setPointerCapture` ниже переносит все последующие события
    // на сам заголовок, и в pointerup `ev.target` — уже он, а не то, по чему нажали.
    const hit = e.target;
    e.preventDefault();
    node.setPointerCapture(e.pointerId);
    raise(el);
    if (!edge) node.classList.add('moving');
    const from = { x: e.clientX, y: e.clientY, c: p.c, r: p.r, w: p.w, h: p.h };
    const step = { x: colStep(), y: rowStep() };
    let moved = false;

    node.onpointermove = (ev) => {
      if (Math.abs(ev.clientX - from.x) > 3 || Math.abs(ev.clientY - from.y) > 3) moved = true;
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

    node.onpointerup = node.onpointercancel = (ev) => {
      node.onpointermove = null;
      node.classList.remove('moving');
      // Заголовок — и ручка переноса, и кнопка. Отличаем одно от другого по факту
      // движения: `preventDefault` выше съедает click, поэтому своего события тут нет.
      // На узком экране перенос отключён целиком, и туда приходит обычный click.
      if (!moved && ev?.type === 'pointerup' && hit.closest('.who'))
        node.dispatchEvent(new CustomEvent('titletap', { bubbles: true }));
      // Окно подвинули руками — возвращать из разворота уже некуда.
      if (p.prev) { p.prev = null; drawZoom(p); }
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
  // Сессия уже открыта — не вторая панель, а подъём той, что есть. Раньше клик по
  // строке списка тут молча заканчивался, и это читалось как «кнопка не работает».
  const open = p.session && panes.find(x => x.session === p.session);
  if (open) {
    const el = document.getElementById('pane-' + open.pane);
    if (el) { raise(el); el.scrollIntoView({ block: 'nearest' }); }
    return;
  }
  if (p.session) { done.delete(p.session); saveMarks(); }
  p.w = p.w || W; p.h = p.h || H;
  p.hue = p.hue ?? freeHue();
  // В начало, а не в конец: порядок массива — это и есть порядок раскладки, и новое
  // окно встаёт слева-сверху, а прежние сдвигаются. Искать взглядом, куда оно упало,
  // не нужно.
  panes.unshift(p);
  // Новое окно перекладывает все: экран занят целиком, и третье на широком мониторе
  // встаёт колонкой справа, а не четвертью в углу. Подбор свободного места, стоявший
  // тут раньше, оставлял пустоты и всё равно кончался общей раскладкой на пятом окне.
  // Цена известна: расставленное руками новое окно сбрасывает. Восстановленные из
  // localStorage панели уже несут свои клетки и сюда не попадают.
  if (!p.c) retile();
  save();
  drawPane(p);
  markList();
}

function closePane(p) {
  // Цвет отдаём строке: панели больше нет, а мигать закрытая сессия обязана тем же.
  if (p.session) { hues[p.session] = p.hue ?? HUE0; saveMarks(); }
  unwatch(p);
  panes = panes.filter(x => x.pane !== p.pane);
  // Закрытое окно оставляло дыру: добавление перекладывает всех с прошлой правки, а
  // закрытие — нет. Раскладка до `save()`, чтобы новые клетки уехали тем же вызовом.
  retile();
  save();
  document.getElementById('pane-' + p.pane)?.remove();
  markList();
  $('empty').hidden = panes.length > 0;
}

function drawPane(p) {
  // Окно файла — другое тело при той же обвязке. Ветка здесь, потому что через drawPane
  // проходят оба пути: и открытие, и восстановление панелей после F5.
  if (p.file) return drawFile(p);
  $('empty').hidden = true;
  const el = document.createElement('section');
  el.id = 'pane-' + p.pane;
  el.innerHTML = `
    <header>
      <button class=ren title="переименовать сессию">\u270e\ufe0e</button>
      <span class=who></span>
      <span class=timer></span>
      <button class=tile title="разложить окна поровну, без перекрытий">\u25eb\ufe0e</button>
      <button class=max title="во весь экран"></button>
      <button class=foldbar title="свернуть окно в заголовок">▾</button>
      <button class=close title="закрыть панель">×</button>
      <i class=ctx></i>
    </header>
    <div class=logbox>
      <div class=log></div>
      <button class=down type=button hidden title="к последнему ответу">↓</button>
    </div>
    <form>
      <div class=menu hidden></div>
      <div class=ghost></div>
      <textarea placeholder=" " title="${TOUCH.matches ? 'кнопка ↑ — отправить'
        : 'Enter — отправить, Shift+Enter — перенос строки'}"></textarea>
      <div class=bar>
        <label class=clip title="прикрепить файлы">+<input type=file multiple></label>
        <select class=model title="модель этой панели"></select>
        <select class=effort title="усилие модели в этой панели: сколько она думает над ходом">
          <option value="low">low</option><option value="medium">med</option>
          <option value="high">high</option><option value="xhigh">xhigh</option>
          <option value="max">max</option>
        </select>
        <button class=send title="отправить; во время прогона — в очередь">↑</button>
        <button class=stop type=button title="остановить">■</button>
      </div>
    </form>`;
  $('panes').append(el);
  setWho(p, el);
  el.querySelector('.close').onclick = () => closePane(p);
  // Заголовок окна — то же переименование, что карандаш в списке слева: имя сессии
  // чаще всего хочется поправить, глядя на её ответы, а не на строку списка.
  const rename = async () => {
    if (!p.session) return;   // сессии ещё нет: имя не к чему привязать
    const name = prompt('имя сессии, пустое снимет', p.title || '');
    if (name === null) return;
    await post('api/name', { session: p.session, name });
    p.title = name.trim() || null;
    setWho(p, el);
    save();
    runFind();
  };
  el.addEventListener('titletap', rename);
  el.querySelector('.ren').onclick = rename;
  el.querySelector('.who').onclick = () => { if (NARROW.matches) rename(); };
  // Лог доводит себя до низа, только пока ты у низа (absorb ниже). Отлистал вверх —
  // и ответ дописывается молча, вернуться было нечем. Кнопку показываем по прокрутке,
  // а после вставки её обновляет absorb: прокрутки там не случается.
  const box = el.querySelector('.log');
  const down = el.querySelector('.down');
  // Действия с окном — разворот, сворачивание, плитка, углы — меняют размер лога, а
  // scrollTop остаётся прежним числом пикселей: текст перетекает, и вид уезжает в
  // середину истории или за её конец, где окно выглядит пустым. Наблюдатель за
  // размером нужен потому, что путей геометрии много (zoom, retile, wireGrab, смена
  // раскладки по ширине), а общее у них одно — этот блок меняет размер.
  // Низ возвращаем только тому, кто у низа и был: отлистанный вверх читает историю.
  // Признак считаем на прокрутке, а не внутри наблюдателя: там размер уже новый, и
  // «был ли внизу» по нему не узнать. Допуск в atEnd — те самые «очень близко к низу».
  let stick = true;   // новое окно открывается у низа
  box.onscroll = () => { if (box.clientHeight) { stick = atEnd(box); down.hidden = stick; } };
  // Свёрнутое окно прячет лог целиком (`display:none`), и размер обнуляется. Нулевую
  // высоту пропускаем в обе стороны, иначе сворачивание считалось бы уходом вверх и
  // разворот открывал бы начало истории.
  new ResizeObserver(() => { if (stick && box.clientHeight) box.scrollTop = 1e9; }).observe(box);
  down.onclick = () => { box.scrollTop = 1e9; };
  // Отклик в тот же кадр. Пока прогон гаснет, проходит до пяти секунд: `runner.cancel`
  // ждёт две между SIGTERM и SIGKILL, а класс `busy` снимает только tick() раз в три.
  // Всё это время кнопка выглядела ненажатой, и по ней жали второй раз. Состояние
  // снимаем не по ответу сервера, а когда tick увидит, что прогон кончился: ответ
  // приходит раньше, чем claude успевает дописать транскрипт.
  const stop = el.querySelector('.stop');
  stop.onclick = () => {
    if (stop.disabled) return;
    stop.disabled = true;
    el.classList.add('stopping');
    post('api/cancel', { pane: p.pane })
      .then(r => r.dropped && log(p, `<div class="msg note">из очереди отброшено: ${r.dropped}</div>`))
      .catch(() => {});
  };
  const form = el.querySelector('form');
  const ta = el.querySelector('textarea');
  // Выбор файлов — тот же путь, что у перетаскивания. `value = ''` нужен, чтобы второй
  // выбор того же файла тоже дал событие. Он же очищает `pick.files`, поэтому `attach`
  // копирует список первой строкой — иначе доедет только первый файл.
  const pick = el.querySelector('.clip input');
  pick.onchange = () => { attach(p, ta, pick.files); pick.value = ''; };
  form.onsubmit = (e) => { e.preventDefault(); send(p, ta); };
  wireSlash(p, el, ta);

  const model = el.querySelector('.model');
  fillModels(p, model);
  model.onchange = () => { p.model = model.value; save(); };

  // Усилие живёт в панели рядом с моделью и так же переживает F5. Пустого значения нет:
  // оно читалось как выбранный уровень, хотя означало «флаг не передавать». Новое окно
  // и окно из localStorage без записи берут EFFORT, и в панели написано ровно то, с чем
  // поедет claude.
  const effort = el.querySelector('.effort');
  p.effort = p.effort || EFFORT;
  effort.value = p.effort;
  effort.onchange = () => { p.effort = effort.value; save(); };

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
  wirePane(p, el);
  // Панель нарисована — можно подписываться. Отсюда, а не из addPane: после F5 панели
  // восстанавливает startup тем же вызовом, и второе место забыли бы синхронизировать.
  watch(p);
}

// Всё, что у окна не зависит от содержимого: место в сетке, цвет, перетаскивание,
// разворот и сворачивание. Вынесено, потому что окон стало два вида — сессия и файл, —
// а отличаются они только телом. Кнопка «закрыть» осталась снаружи: у файла она сначала
// спрашивает про несохранённые правки.
function wirePane(p, el) {
  const fold = el.querySelector('header .foldbar');
  const drawFold = () => {
    el.classList.toggle('rolled', !!p.roll);
    fold.textContent = p.roll ? '▾' : '▴';
    fold.title = p.roll ? 'развернуть окно' : 'свернуть окно в заголовок';
  };
  // Свёрнутое и развёрнутое — состояния взаимоисключающие, и держит это здесь, а не
  // вёрстка. Вместе они дают окно из одного заголовка, которому на телефоне положено
  // лежать в сетке, а Safari оставляет его в слое от прежнего `position:fixed` — оно
  // накрывает сайдбар и соседей. Развернуть свёрнутое значит показать его целиком,
  // свернуть развёрнутое — вернуть в сетку.
  // Раскладка глобальная: кнопка в любой шапке кладёт в сетку все окна разом. Жила в
  // сайдбаре и переехала сюда 2026-09-21 — это действие над окнами, и место ему среди
  // кнопок окна, а не среди списка сессий.
  el.querySelector('header .tile').onclick = () => { retile(); save(); };
  const max = el.querySelector('header .max');
  max.onclick = () => {
    if (p.roll) { p.roll = false; drawFold(); }
    zoom(p); save(); raise(el);
  };
  // Флаг лежит в самой панели, а она целиком уходит в localStorage — свёрнутая
  // остаётся свёрнутой и после F5, как остаётся её место в сетке.
  fold.onclick = () => {
    p.roll = !p.roll;
    if (p.roll && p.prev) zoom(p);
    save(); drawFold();
  };
  // Панель могла уйти в localStorage ещё в обоих состояниях разом — распрямляем при
  // первой же отрисовке, иначе она так и висит развёрнутой полоской заголовка.
  if (p.roll && p.prev) { p.roll = false; save(); }
  drawFold();
  el.style.setProperty('--hue', p.hue ?? HUE0);
  el.querySelector('header').classList.add('grip');
  wireHandles(p, el);
  el.onpointerdown = () => raise(el);
  // Клик по окну ставит курсор в промпт — мышью. Пальцем нет: тап по логу там нужен
  // выделению, а не фокусу, и перехват ломал бы «выделить и скопировать» ровно в том
  // месте, где текст и читают. Поле промпта на телефоне и так под пальцем внизу.
  // Клик по кнопке, полю или ссылке оставляем им, выделение мышью — тоже не рвём.
  el.addEventListener('click', (e) => {
    if (TOUCH.matches) return;
    if (e.target.closest('button, a, input, select, textarea, label, .menu')) return;
    if (!getSelection().isCollapsed) return;
    el.querySelector('form textarea')?.focus();
  });
  fit(p);
  applyGeom(p);
  drawZoom(p);
  // Новое окно — сверху. Без этого оно уходило под активную панель: `act` держит
  // z-index, а порядок в DOM его не перебивает.
  raise(el);
}

// --- files:begin ---
// Окно файла: то же окно сетки, вместо лога и композера — поле правки. Дерево при этом
// остаётся в правом сайдбаре: править код в полосе 280px негде, а окон с файлами нужно
// столько же, сколько с сессиями.
function drawFile(p) {
  $('empty').hidden = true;
  const el = document.createElement('section');
  el.id = 'pane-' + p.pane;
  el.innerHTML = `
    <header>
      <button class=ren title="копировать путь">\u2750\ufe0e</button>
      <span class=who></span>
      <a class=dl title="скачать файл" download>\u2913</a>
      <button class=tile title="разложить окна поровну, без перекрытий">\u25eb\ufe0e</button>
      <button class=max title="во весь экран"></button>
      <button class=foldbar title="свернуть окно в заголовок">▾</button>
      <button class=close title="закрыть окно">×</button>
    </header>
    <textarea class=edit spellcheck=false wrap=off></textarea>
    <img class=view hidden alt="">
    <div class=filebar>
      <button class=save>сохранить</button>
      <button class=reread title="перечитать с диска">↻</button>
      <span class=state></span>
    </div>`;
  $('panes').append(el);
  const who = el.querySelector('.who');
  const ta = el.querySelector('.edit');
  const state = el.querySelector('.state');
  const save_ = el.querySelector('.save');
  who.textContent = p.file.split('/').pop();
  who.title = p.file;
  const say = (text, bad) => { state.textContent = text; state.classList.toggle('bad', !!bad); };
  // Заголовок окна файла отдаёт полный путь в буфер: его вставляют в промпт соседнего
  // окна, и перенабирать руками — единственное, что тут делали до этой кнопки.
  const copyPath = async () => {
    const was = state.textContent;
    const ok = await copy(p.file);
    say(ok ? 'путь скопирован' : 'буфер недоступен', !ok);
    setTimeout(() => say(was), 1200);
  };
  el.addEventListener('titletap', copyPath);
  el.querySelector('.ren').onclick = copyPath;
  who.onclick = () => { if (NARROW.matches) copyPath(); };

  const view = el.querySelector('.view');
  // Скачивание идёт по той же ручке, что и картинка: файл уже отдаётся байтами, тут
  // нужен только адрес. `?v=` — версия из `api/file`, иначе браузер вернёт из кеша
  // прежнее содержимое. С телефона это единственный способ забрать файл себе.
  const dl = el.querySelector('.dl');
  dl.download = p.file.split('/').pop();
  let version = null, dirty = false;
  const load = () => get('api/file?path=' + encodeURIComponent(p.file)).then(f => {
    version = f.version;
    dl.href = 'api/raw?path=' + encodeURIComponent(p.file) + '&v=' + f.version;
    // Картинку сервер не читает — отдаёт тип и оставляет тег `<img>` тянуть байты самому.
    // Редактора у неё нет: править png в textarea нечем, и «сохранить» тут только вредит.
    if (f.image) {
      view.src = 'api/raw?path=' + encodeURIComponent(p.file) + '&v=' + f.version;
      view.hidden = false; ta.hidden = true; save_.hidden = true; dirty = false;
      return say(f.image.replace('image/', '') + ', ' + kb(f.size));
    }
    ta.value = f.text ?? '';
    // Отказ приходит полем `why`: файл больше мегабайта или не текст. Показываем имя,
    // размер и причину — пустое окно без объяснения читалось бы как поломка.
    ta.readOnly = !!f.why;
    save_.hidden = !!f.why;
    dirty = false;
    say(f.why ? f.why + ', ' + kb(f.size) : kb(f.size));
  }, (e) => { ta.readOnly = true; save_.hidden = true; say('не открыть (' + e + ')', true); });

  // Своя обёртка вместо общего `post`: тут нужен текст ошибки, а не только её код.
  // `:ro`-монтирование отвечает «Read-only file system», и это единственное, что
  // объясняет отказ — например у /root/.claude/CLAUDE.md, он примонтирован на чтение.
  const put = async () => {
    const r = await fetch('api/file', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: p.file, text: ta.value, version }) });
    const body = await r.text();
    if (!r.ok) throw new Error(r.status === 409 ? 'файл изменился на диске, перечитайте' : body);
    version = JSON.parse(body).version;
    dirty = false;
    say('сохранено ' + new Date().toLocaleTimeString());
  };

  ta.oninput = () => { dirty = true; say('не сохранено'); };
  save_.onclick = () => put().catch(e => say(String(e.message || e), true));
  el.querySelector('.reread').onclick = () => {
    if (dirty && !confirm('Правки не сохранены. Перечитать с диска?')) return;
    load();
  };
  // Ctrl+S привычнее кнопки, а браузерное «сохранить страницу» тут не нужно никому.
  ta.onkeydown = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save_.onclick(); }
  };
  el.querySelector('.close').onclick = () => {
    if (dirty && !confirm('Правки не сохранены. Закрыть окно?')) return;
    closePane(p);
  };
  wirePane(p, el);
  load();
}

const kb = (n) => n < 1024 ? n + ' Б'
                : n < 1048576 ? Math.round(n / 1024) + ' КБ'
                : (n / 1048576).toFixed(1) + ' МБ';

// Файлы с точки показываем всегда: в /root/.claude половина интересного начинается
// именно с неё. Переключатель тут стоял и не пригодился ни разу — снят 2026-09-21.

async function loadRoots() {
  const list = await get('api/roots');
  const sel = $('root');
  sel.innerHTML = list.map(r => `<option value="${esc(r.path)}">${esc(r.path)}</option>`).join('');
  const saved = localStorage.getItem('root');
  sel.value = list.some(r => r.path === saved) ? saved : (list[0]?.path || '');
  // Каталог с прошлого раза мог исчезнуть вместе с веткой — тогда открываем корень.
  const at = localStorage.getItem('dir');
  const start = at && at.startsWith(sel.value) ? at : sel.value;
  return openDir(start).catch(() => openDir(sel.value));
}

let atDir = '';

async function openDir(path) {
  const data = await get('api/files?path=' + encodeURIComponent(path));
  atDir = data.path;
  localStorage.setItem('dir', data.path);
  drawCrumb(data.path);
  drawTree(data.entries);
}

const treeFail = (e) => { $('tree').innerHTML = '<div class=none>не открыть (' + esc(e) + ')</div>'; };

// Путь от корня кнопками. Выше корня подниматься нечем и не нужно: сервер такой путь
// всё равно отклонит, а в дереве видно только примонтированное.
//
// Корень ищем по самому пути, а не берём выбранный в списке: сервер отвечает путём
// после resolve(), и симлинк уводит в другой корень — `/root/.claude/skills/end` это
// на самом деле `/opt/skills/10-base/end`. Раньше такой путь не совпадал с выбранным
// корнем, `rest` оставался пустым, и от крошек оставалась одна кнопка корня.
// Сравниваем с `/` на конце: иначе `/data` считался бы корнем для `/database`.
function drawCrumb(path) {
  const roots = [...$('root').options].map(o => o.value);
  const root = roots.filter(r => path === r || path.startsWith(r + '/'))
                    .sort((a, b) => b.length - a.length)[0] || $('root').value;
  // Выпадашка идёт следом за путём — иначе она показывает один корень, а дерево стоит
  // в другом, и после перезагрузки страницы место теряется.
  if (root !== $('root').value && roots.includes(root)) {
    $('root').value = root;
    localStorage.setItem('root', root);
  }
  const rest = path.startsWith(root) ? path.slice(root.length).split('/').filter(Boolean) : [];
  let at = root;
  const parts = [`<button data-at="${esc(root)}">${esc(root.split('/').pop() || '/')}</button>`];
  for (const name of rest) {
    at += '/' + name;
    parts.push(`<button data-at="${esc(at)}">${esc(name)}</button>`);
  }
  $('crumb').innerHTML = parts.join('<span> / </span>');
  for (const b of $('crumb').querySelectorAll('button'))
    b.onclick = () => openDir(b.dataset.at).catch(treeFail);
}

function drawTree(entries) {
  $('tree').innerHTML = entries.map(e =>
    `<button class="${e.dir ? 'dir' : ''}" data-path="${esc(e.path)}" data-dir="${e.dir ? 1 : ''}">`
    // Размер первым среди плавающих вправо — он и встаёт к правому краю. Кнопки идут
    // следом, левее: место они держат всегда, видимы только под курсором, и размер от
    // них уезжал бы на полсотни пикселей от края.
    + (e.dir ? '' : `<span class=size>${kb(e.size)}</span>`)
    + `<span class=rm title="удалить">\u2715</span>`
    + `<span class=mv title="переименовать">\u270e\ufe0e</span>`
    + `<span class=cp title="копировать путь">\u2750\ufe0e</span>`
    + esc(e.name) + (e.dir ? '/' : '') + '</button>').join('')
    || '<div class=none>пусто</div>';
  for (const b of $('tree').querySelectorAll('button')) {
    b.onclick = () => b.dataset.dir ? openDir(b.dataset.path).catch(treeFail)
                                    : addPane({ pane: uid(), file: b.dataset.path });
    // Крестик внутри той же кнопки — всплытие обрываем, иначе удаление заодно открывало
    // бы файл. Непустую папку отобьёт сервер: рекурсии у него нет.
    // Путь в буфер: вставить его в промпт — самое частое, что делают с файлом из дерева,
    // и до этой кнопки его собирали по крошкам глазами.
    b.querySelector('.cp').onclick = async (e) => {
      e.stopPropagation();
      const cp = b.querySelector('.cp');
      cp.textContent = await copy(b.dataset.path) ? '\u2713' : '\u2717';
      setTimeout(() => { cp.textContent = '\u2750\ufe0e'; }, 1200);
    };
    // Переименование на месте: каталог не меняется, поэтому из дерева хватает имени.
    // Открытое окно этого файла переезжает вместе с ним — иначе оно осталось бы на
    // пути, которого уже нет, и сказало бы об этом только при сохранении.
    b.querySelector('.mv').onclick = async (e) => {
      e.stopPropagation();
      const was = b.dataset.path.split('/').pop();
      const name = prompt('новое имя', was);
      if (name === null || !name.trim() || name.trim() === was) return;
      const made = await ask('api/mv', { path: b.dataset.path, name: name.trim() },
                             'не переименовать');
      if (!made) return;
      panes.filter(x => x.file === b.dataset.path).forEach(x => {
        x.file = made.path;
        document.getElementById('pane-' + x.pane)?.remove();
        drawPane(x);
      });
      save();
      openDir(atDir).catch(treeFail);
    };
    b.querySelector('.rm').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm('удалить ' + b.dataset.path + '? это навсегда')) return;
      if (!await ask('api/rm', { path: b.dataset.path }, 'не удалить')) return;
      panes.filter(x => x.file === b.dataset.path).forEach(closePane);
      openDir(atDir).catch(treeFail);
    };
  }
}
// --- files:end ---

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
// Высота поля — по набранному, а не по подсказке. У пустого поля `scrollHeight` считает
// и placeholder, и длинная подсказка растягивала композер до потолка в 240px. Дальше
// браузер подкручивал секцию, чтобы показать поле с фокусом, и шапка уезжала за верхний
// край: у `section` стоит `overflow:hidden`, вернуть её колесом нельзя.
function grow(ta) {
  const hold = ta.placeholder;
  ta.placeholder = '';
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
  ta.placeholder = hold;
}

// --- slash:begin ---
// Скиллы для подсказки: один запрос на проект за жизнь вкладки. Список меняется, когда
// человек пишет скилл, — реже, чем жмёт F5, и обновление по таймеру тут было бы опросом
// ради нуля событий.
const skillCache = new Map();
function skillsFor(project) {
  if (!skillCache.has(project)) {
    skillCache.set(project, get('api/skills?project=' + encodeURIComponent(project))
      .catch(() => []));
  }
  return skillCache.get(project);
}

// Слеш-команду claude понимает только в начале промпта — поэтому и меню открывается
// только там: всё до курсора это `/` и слово без пробелов.
const HEAD_RE = /^\/(\S*)$/;
const NAME_RE = /^\/([\w-]+)/;

// Пробел, а не пустая строка: `:placeholder-shown` гасит кнопку отправки на пустом
// поле, и пустой placeholder этому селектору не подходит.
const BLANK = ' ';

// Следующий промпт claude называет сам, в «ёлочках» — этого требует стиль ответа:
// последняя строка обязана быть действием, и точная формулировка даётся дословно
// («собери», «sync-repo», «раскатай на песочницы»). Отсюда её и берём, без вызова
// модели: текст ответа уже пришёл в панель.
//
// Смотрим только в хвост. В начале ответа «ёлочки» — это обычные цитаты, а действие
// стоит последней строкой; три строки запаса на случай, когда за ним идёт приписка.
const QUOTED = /«([^»\n]{1,200})»/g;
function fromAnswer(text) {
  const tail = (text || '').split('\n').filter(x => x.trim()).slice(-3).join('\n');
  const all = [...tail.matchAll(QUOTED)].map(m => m[1].trim()).filter(Boolean);
  return all.length ? all[all.length - 1] : null;
}

// Поле ввода целиком: Enter, автодополнение по `/` и подсветка имени команды.
// Одной функцией, потому что клавиши у них общие — меню забирает Enter себе, и
// разнести это на два обработчика значит спорить за один и тот же `keydown`.
function wireSlash(p, el, ta) {
  const menu = el.querySelector('.menu');
  const ghost = el.querySelector('.ghost');
  let all = [], shown = [], sel = 0;
  skillsFor(p.project).then(list => { all = list; paint(); });

  // Голова строки — то, что слева от курсора. Меню живёт, только пока она совпадает:
  // курсор ушёл в другое место, и подставлять уже некуда.
  const head = () => ta.value.slice(0, ta.selectionStart).match(HEAD_RE);
  const close = () => { menu.hidden = true; };

  // Метку ставим только известному имени. Незнакомое `/фигня` остаётся обычным текстом
  // — это и есть сигнал об опечатке, до отправки, а не после.
  function paint() {
    ta.placeholder = hints.get(p.pane) || BLANK;
    const name = (ta.value.match(NAME_RE) || [])[1];
    ghost.innerHTML = name && all.some(s => s.name === name)
      ? `<mark>/${esc(name)}</mark>` : '';
    ghost.scrollTop = ta.scrollTop;
  }

  function open() {
    const m = head();
    if (!m) return close();
    const q = m[1].toLowerCase();
    shown = all.filter(s => s.name.toLowerCase().includes(q));
    if (!shown.length) return close();
    sel = 0;
    draw();
  }

  function draw() {
    menu.innerHTML = shown.map((s, i) =>
      `<div aria-selected=${i === sel} data-i=${i} title="${esc(s.desc)}">` +
      `<b>/${esc(s.name)}</b> <i>${esc(s.desc)}</i></div>`).join('');
    menu.hidden = false;
  }

  function accept(i) {
    const rest = ta.value.slice(ta.selectionStart).replace(/^\s+/, '');
    ta.value = '/' + shown[i].name + ' ' + rest;
    ta.selectionStart = ta.selectionEnd = shown[i].name.length + 2;
    close();
    grow(ta);
    paint();
    ta.focus();
  }

  // mousedown, а не click: клик уводит фокус из поля раньше, чем случится выбор, и
  // подставлять было бы уже некуда.
  menu.onmousedown = (e) => {
    const row = e.target.closest('[data-i]');
    if (!row) return;
    e.preventDefault();
    accept(+row.dataset.i);
  };

  // Enter отправляет, перенос строки — с Shift или Alt. Ctrl/Cmd+Enter оставлен: он
  // работал раньше, и пальцы помнят. При открытом меню Enter сначала выбирает команду.
  // На тач-устройстве Enter не отправляет вовсе: модификаторов на экранной клавиатуре
  // нет, и перенос строки набрать было нечем — любой Enter улетал промптом.
  ta.onkeydown = (e) => {
    if (!menu.hidden && head()) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        sel = (sel + (e.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
        return draw();
      }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); return accept(sel); }
      if (e.key === 'Escape') { e.preventDefault(); return close(); }
    }
    // Серая подсказка после прогона: Tab кладёт её в поле, отправку оставляет за Enter.
    // Предложение бывает не тем, и одна клавиша до отправки — это слишком коротко.
    if (e.key === 'Tab' && menu.hidden && !ta.value && hints.get(p.pane)) {
      e.preventDefault();
      ta.value = hints.get(p.pane);
      ta.selectionStart = ta.selectionEnd = ta.value.length;
      grow(ta);
      return paint();
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !TOUCH.matches) { e.preventDefault(); send(p, ta); }
  };
  ta.oninput = () => { grow(ta); paint(); open(); };
  // Курсор переехал мышью — меню либо открывается на новом месте, либо закрывается.
  ta.onclick = open;
  ta.onblur = close;
  ta.onscroll = () => { ghost.scrollTop = ta.scrollTop; };
}
// --- slash:end ---

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
      btn.textContent = await copy(text) ? 'скопировано' : 'не вышло';
      setTimeout(() => { btn.textContent = 'копировать'; }, 1200);
    };
    // Оборачиваем, а не кладём внутрь: кнопка обязана остаться на месте, когда код
    // листают вбок.
    const box = document.createElement('div');
    box.className = 'codebox';
    pre.replaceWith(box);
    box.append(pre, btn);
  }
}

// Загрузка файлов по одному: ответ сервера — путь в песочнице, его и дописываем в
// поле ввода. Отдельной строкой, чтобы промпт остался читаемым.
// Отправка файла ходом наружу. `fetch` хода отправки не отдаёт вовсе — только XHR, и
// это единственная причина держать его здесь. На мобильной сети и файле в несколько
// мегабайт без этого кажется, что нажатие вообще не сработало.
function upload(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', 'api/upload');
    // lengthComputable бывает false на потоковом теле — тогда процента просто нет, и
    // строка остаётся на «идёт отправка».
    xhr.upload.onprogress = (e) => onProgress(e.lengthComputable ? e.loaded / e.total : null);
    xhr.onload = () => xhr.status === 200
      ? resolve(JSON.parse(xhr.responseText))
      : reject(new Error(xhr.responseText || ('код ' + xhr.status)));
    xhr.onerror = () => reject(new Error('обрыв связи'));
    xhr.send(form);
  });
}

// --- attach:begin ---
async function attach(p, ta, files) {
  // Копия списка, а не сам FileList: он живой. `pick.value = ''` в обработчике выбора
  // очищает его на первом же `await` ниже, и цикл заканчивался после первого файла —
  // из десяти выбранных доходил один.
  for (const file of [...(files || [])]) {
    // Строка в логе, а не отдельная плашка: лог и так на виду, а место под панелью
    // занято полем промпта. textContent вместо разметки — имя файла приходит от
    // человека и экранировать его иначе пришлось бы руками.
    const row = log(p, '<div class="msg note"></div>');
    const size = kb(file.size);
    const show = (part) => {
      if (row) row.textContent = part === null
        ? `отправляю ${file.name} (${size})…`
        : `отправляю ${file.name} (${size}) — ${Math.round(part * 100)}%`;
    };
    show(0);
    try {
      const { path } = await upload(file, show);
      row?.remove();   // путь уже в поле промпта, строке в логе больше нечего сказать
      ta.value = (ta.value ? ta.value.replace(/\s*$/, '\n') : '') + path + '\n';
      grow(ta);
      ta.focus();
    } catch (e) {
      const text = `файл не загрузился: ${String(e).slice(0, 200)}`;
      if (row) { row.className = 'msg err'; row.textContent = text; }
      else log(p, `<div class="msg err">${esc(text)}</div>`);
    }
  }
}
// --- attach:end ---

// Вставка мимо потока: свой промпт и красные строки. Прокрутка тут обязательна —
// без неё длинный промпт уезжал за нижний край, и absorb() дальше считал панель
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

// Лимиты подписки. Место — низ списка, а не шапка панели: лимит общий на аккаунт,
// и в каждом окне это была бы одна и та же полоска. Значения приезжают со статусом,
// перерисовываем только когда они правда сменились — раз в минуту, а не раз в тик.
// Возраст чисел словами. Полоски переживают рестарт бота и живут в базе, поэтому
// «только что» и «час назад» надо различать: при протухшем токене или сбое API числа
// остаются на экране, и без подписи они выглядят свежими.
// Сколько осталось до сброса лимита. Дата сброса приходит с каждой полоской и до сих
// пор лежала только в подсказке — до неё не дотянуться ни пальцем, ни взглядом, а это
// главное число после самого процента: упёрся в лимит и решаешь, ждать или менять план.
const until = (iso) => {
  const sec = (new Date(iso) - Date.now()) / 1000;
  if (!iso || !(sec > 0)) return '';
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60);
  return h >= 24 ? `${Math.floor(h / 24)}д ${h % 24}ч` : h ? `${h}ч ${m}м` : `${m}м`;
};

const since = (sec) =>
  sec < 60 ? 'только что'
    : sec < 3600 ? `${Math.floor(sec / 60)} мин назад`
    : sec < 86400 ? `${Math.floor(sec / 3600)} ч назад`
    : `${Math.floor(sec / 86400)} дн назад`;

// Вход по подписке из панели. Флоу тот же, что у `/login` в Telegram: сервер поднимает
// `claude auth login` в pty, отдаёт ссылку, а код из браузера возвращается второй
// ручкой. Ключ один на контейнер, поэтому вход из панели — это вход и для бота.
async function doLogin() {
  const dlg = $('auth'), say = $('authsay'), code = $('authcode');
  say.textContent = 'поднимаю вход…';
  code.value = '';
  $('authurl').removeAttribute('href');
  dlg.showModal();
  let started;
  try { started = await post('api/login', {}); }
  catch (e) { say.textContent = 'не начать вход: ' + e; return; }
  // Ссылка кликабельна и заодно уезжает в буфер: на телефоне вход часто открывают в
  // другом браузере, а не в той же вкладке.
  $('authurl').href = started.url;
  await copy(started.url);
  say.textContent = 'ссылка скопирована';
  code.focus();
}

$('authno').onclick = () => $('auth').close();
$('authok').onclick = async () => {
  const code = $('authcode').value.trim();
  if (!code) return $('authcode').focus();
  $('authsay').textContent = 'проверяю…';
  const r = await ask('api/login/code', { code }, 'не войти');
  if (!r) return;
  $('authsay').textContent = r.ok ? 'вошли' : ('не вышло: ' + (r.detail || '').slice(0, 200));
  // Закрываем только при успехе: текст отказа нужно успеть прочитать.
  if (r.ok) setTimeout(() => $('auth').close(), 900);
};
$('authcode').onkeydown = (e) => { if (e.key === 'Enter') $('authok').click(); };

// --- plan:begin ---
function setPlan(lim, auth) {
  const box = $('plan');
  // Подпись возраста меняется сама по себе, без нового ответа сервера, поэтому входит
  // в ключ сравнения: иначе блок перерисовался бы только раз в две минуты и врал бы
  // «только что» всё это время.
  const label = lim?.at ? since(Date.now() / 1000 - lim.at) : '';
  const j = JSON.stringify(lim || null) + '|' + label + '|' + JSON.stringify(auth || null) +
    '|' + (lim?.bars || []).map(b => until(b.resets)).join();
  if (box.dataset.j === j) return;
  box.dataset.j = j;
  const bars = (lim?.bars || []);
  // Строка ключа стоит всегда, пока про ключ вообще что-то известно. Прятать её, пока всё
  // хорошо, значило показывать кнопку входа ровно в тот момент, когда она впервые
  // понадобилась, — а искать незнакомую кнопку на сломанном ключе поздно. Под цену
  // строки в сайдбаре подогнан текст: в спокойном состоянии это «ключ 6д 9ч», а не
  // фраза на три слова, которую обрезало многоточием.
  const soon = auth?.until ? auth.until * 1000 - Date.now() : 0;
  const need = !!auth && (!auth.ok || !auth.renewable);
  const warn = !!auth && auth.ok && auth.renewable && soon > 0 && soon < 2 * 86400e3;
  // Блок остаётся на экране и без полосок: без ключа лимитов не бывает вовсе, а кнопка
  // входа нужна ровно в этот момент. Раньше он просто прятался.
  if (!bars.length && !auth) { box.hidden = true; return; }
  box.hidden = false;
  // Сроки живут в подсказках, а не в строках. Четыре длительности подряд читались как
  // сплошная лента цифр, а нужна из них в каждый момент одна: остальное — справка, за
  // которой человек наводит мышь. В строке остаётся только то, что меняет решение:
  // проценты у лимитов и состояние у ключа.
  const who = [lim?.email, lim?.plan].filter(Boolean).join(' · ');
  const left = auth?.until ? until(auth.until * 1000) : '';
  // Кнопка только там, где она нужна: на живом ключе жать её незачем, а без неё строка
  // занимает всю ширину.
  const state = need ? (auth.ok ? 'нужен вход' : 'не авторизован')
              : warn ? 'ключ скоро кончится' : 'ключ активен';
  const hint = need ? 'обновить ключ нечем — нужен новый вход'
             : left ? `рефреш через ${left}` : 'ключ активен';
  box.innerHTML =
    (who ? `<div class=who title="${esc(label ? 'обновлено ' + label : who)}">` +
           `${esc(who)}</div>` : '') +
    (auth ? `<div class="auth${need ? ' hot' : warn ? ' warn' : ''}"` +
            ` title="${esc(hint)}"><em>${esc(state)}</em>` +
            (need || warn ? '<button id=login>войти</button>' : '') + '</div>' : '') +
    bars.map((b) => {
    const p = Math.max(0, Math.min(100, b.percent));
    const cls = (b.severity && b.severity !== 'normal') || p >= 90 ? ' hot' : p >= 75 ? ' warn' : '';
    const left = until(b.resets);
    const when = left ? `сброс лимитов через ${left}` : 'время сброса неизвестно';
    // Срок сброса возвращается в строку, как только полоска пожелтела или покраснела.
    // На спокойной это справка, за которой наводят мышь; на тревожной — то самое, ради
    // чего в блок и смотрят, и прятать его туда, куда надо тянуться, незачем.
    return `<div class=lim title="${esc(when)}"><em>${esc(b.name)}</em>` +
      `<span>${cls && left ? `через ${esc(left)} · ` : ''}${p}%</span>
      <div class=track><i class="fill${cls}" style="width:${p}%"></i></div></div>`;
  }).join('');
  const btn = box.querySelector('#login');
  if (btn) btn.onclick = doLogin;
}
// --- plan:end ---

// У низа ли лог. Сорок пикселей допуска: докрутить вплотную выходит не всегда, а
// «почти внизу» читается как «внизу» — и дальше лог снова едет за ответом сам.
const atEnd = (box) => box.scrollTop + box.clientHeight >= box.scrollHeight - 40;

function log(p, html) {
  const box = document.querySelector('#pane-' + p.pane + ' .log');
  if (!box) return null;
  box.insertAdjacentHTML('beforeend', html);
  box.scrollTop = 1e9;
  return box.lastElementChild;
}

async function send(p, ta) {
  const prompt = ta.value.trim();
  if (!prompt) return;
  ta.value = '';
  // Подсказку снимаем до перерисовки: она была про прошлый прогон, а начался новый.
  hints.delete(p.pane);
  ta.oninput();  // не только высота: с текстом уходит подсветка команды и подсказка
  // Момент отправки — единственный жест пользователя, на котором браузер позволяет
  // спросить разрешение. На загрузке страницы Safari и Chrome такой запрос игнорируют.
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
  // Пузырь печатаем до запроса: ответ на отправку ждёт id сессии до полутора минут, и
  // без него панель всё это время выглядела бы проглотившей промпт. Узлы кладём в запись
  // эха — снимет их `absorb`, когда тот же промпт приедет из транскрипта на своё место.
  const line = log(p, `<div class="msg user"><span class=role>ты</span>${linkify(esc(prompt))}</div>`);
  const echo = { text: norm(prompt), at: Date.now(), nodes: [line] };
  echoes.set(p.pane, [...(echoes.get(p.pane) || []), echo]);
  try {
    const r = await post('api/prompt', { pane: p.pane, project: p.project,
      session: p.session || null, prompt, model: p.model || null,
      effort: p.effort || null });
    // Панель занята: промпт принят и ждёт. Сессию, если она ещё не заведена, панель
    // подберёт в tick() из /api/status — к ответу на отправку её просто нет.
    // Строка про ожидание живёт ровно столько же, сколько локальный пузырь: промпт
    // пошёл — ждать больше нечего, и висеть ей в логе незачем.
    if (r.queued) {
      echo.nodes.push(log(p, `<div class="msg note">в очереди: впереди ${r.queued}</div>`));
      return;
    }
    if (!r.session) { log(p, '<div class="msg err">claude не отдал id сессии</div>'); return; }
    if (r.session !== p.session) {
      // Новая сессия: id придумал claude, панель дочитывает уже созданный транскрипт.
      // Название берём из промпта — сервер даст своё только при следующем обновлении
      // списка, а подпись нужна сразу.
      // Сравнение с прежним id, а не проверка на пустоту: `/clear` начинает новую
      // сессию у живой панели, и без этого она осталась бы читать старый транскрипт,
      // а ответы уходили бы в тот, которого никто не видит.
      p.session = r.session; p.next = 0;
      p.title = p.title || prompt.slice(0, 60);
      save();
      watch(p);
      setWho(p);
      loadTree();
    }
  } catch (code) {
    // Промпт до claude не доехал, значит из транскрипта он не вернётся. Оставленная
    // запись в `echoes` встала бы в голову очереди навсегда, и каждый следующий промпт
    // этой панели печатался бы дважды — локально и из транскрипта.
    // Снимаем одну запись, а не все совпадения: тот же текст мог быть отправлен и
    // раньше, успешно, и его эхо в очереди законное.
    const queue = echoes.get(p.pane) || [];
    const text = norm(prompt);
    for (let i = queue.length - 1; i >= 0; i--)
      if (queue[i].text === text) { queue.splice(i, 1); break; }
    if (!queue.length) echoes.delete(p.pane);
    // Текст возвращаем только в пустое поле: за время запроса (до 90 секунд ожидания
    // id сессии) человек мог начать набирать следующий, и затирать его нельзя. Тогда
    // строка в логе остаётся — иначе промпт исчез бы совсем, откуда его не скопировать.
    const back = !ta.value;
    if (back) { ta.value = prompt; ta.oninput(); line?.remove(); }
    log(p, `<div class="msg err">не отправилось (${esc(code)})` +
           `${back ? ', промпт вернулся в поле' : ''}</div>`);
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
// ponytail: таблицы только простые — труба внутри `код` считается разделителем колонок.
// Начнёт калечить вывод — вендорить marked.js в образ, а не наращивать регулярки.
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

  // Список целиком, вместе с вложенным и продолжениями пунктов. В ящик, как таблицу:
  // готовый html внутри, и разбивка на абзацы ниже не заглянет внутрь. Без этого её
  // нежадный поиск закрывающего тега останавливался на первом `</ul>` — то есть на
  // конце вложенного списка, а не внешнего.
  t = t.replace(LIST_RE, (m) => stash(list(m)));

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

// Блок списка: начинается строкой с маркером, дальше — такие же строки, строки с
// отступом (продолжение пункта или вложенный пункт) и пустые строки перед отступом.
// До 2026-09-29 маркер искался только в нулевой колонке, и любая отступленная строка
// рвала список на куски: вложенный пункт выпадал абзацем с видимым дефисом, а `<ol>`
// начинался заново и сбрасывал нумерацию на единицу.
const LIST_RE = new RegExp(
  '^[ \\t]*(?:[-*]|\\d+[.)])[ \\t]+.*(?:\\n|$)' +
  '(?:^[ \\t]*(?:[-*]|\\d+[.)])[ \\t]+.*(?:\\n|$)' +
  '|^[ \\t]+\\S.*(?:\\n|$)' +
  '|^[ \\t]*\\n(?=[ \\t]+\\S))*', 'gm');

// Глубже claude не пишет, а кривой отступ не должен строить дерево на сто уровней.
const LIST_MAX = 4;

function list(block) {
  const flat = [];
  for (const line of block.replace(/\n+$/, '').split('\n')) {
    const m = line.match(/^([ \t]*)(?:[-*]|(\d+)[.)])[ \t]+(.*)$/);
    // Таб считаем за два пробела: важен порядок отступов между собой, а не их размер.
    if (m) flat.push({ pad: m[1].replace(/\t/g, '  ').length,
                       ordered: m[2] !== undefined, lines: [m[3]], kids: [] });
    else if (line.trim() && flat.length) flat[flat.length - 1].lines.push(line.trim());
  }
  if (!flat.length) return '';

  // Стек предков по возрастанию отступа: пункт с меньшим или равным отступом закрывает
  // всё, что глубже, и встаёт соседом.
  const root = [], stack = [];
  for (const it of flat) {
    while (stack.length && it.pad <= stack[stack.length - 1].pad) stack.pop();
    if (stack.length >= LIST_MAX) stack.length = LIST_MAX - 1;
    (stack.length ? stack[stack.length - 1].kids : root).push(it);
    stack.push(it);
  }

  // Вид списка задаёт первый пункт уровня: `-` и `1.` вперемешку на одном уровне claude
  // не пишет, а гадать по каждому пункту значило бы рвать список ровно там, где раньше.
  const draw = (nodes) => {
    const tag = nodes[0].ordered ? 'ol' : 'ul';
    const li = nodes.map(n => `<li>${n.lines.map(inline).join('<br>')}` +
                              `${n.kids.length ? draw(n.kids) : ''}</li>`).join('');
    return `<${tag}>${li}</${tag}>`;
  };
  return draw(root);
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

// Строка вызова. В заголовке группы ссылку не делаем: клик по ней и разворачивал бы
// пачку, и уводил на страницу разом.
const toolHead = (it, link) => {
  const arg = esc(it.text);
  return `${esc(it.icon)} ${esc(it.name)}: ${link ? linkify(arg) : arg}`;
};

function renderItem(it) {
  // Подставленный контекст: факт и объём, без содержимого.
  if (it.role === 'note') {
    return `<div class="msg note">↳ ${esc(it.text)}</div>`;
  }
  if (it.role === 'tool') {
    const head = toolHead(it, true);
    // Длинный шаг раскрывается кликом. `details` нативный: разворот, фокус с клавиатуры
    // и Ctrl+F браузера достаются даром, своей кнопки и состояния в JS не нужно.
    return it.full ? `<details class="tool"><summary>${head}</summary>` +
                     `<pre>${esc(it.full)}</pre></details>`
                   : `<div class="tool">${head}</div>`;
  }
  // Промпт человека остаётся текстом: он набирал его руками, и случайная звёздочка не
  // должна оказаться курсивом. Разметку рисуем только у ответа.
  if (it.role === 'user') {
    return `<div class="msg user"><span class=role>ты</span>${linkify(esc(it.text))}</div>`;
  }
  return `<div class="msg assistant"><span class=role>claude</span>` +
         `<div class=body>${md(it.text)}</div></div>`;
}

// Шаги инструментов идут пачкой между промптом и ответом, и их бывает по полсотни —
// ответ уезжает за экран, а листать приходится мимо того, что и так уже случилось.
// Пачка сворачивается в один `details`: в заголовке счётчик и последний вызов, поэтому
// на бегущем прогоне видно, чем claude занят сейчас, а раскрытие остаётся нативным.
// Группу закрывает любое другое сообщение: следующий шаг начнёт новую. Дописываем
// именно в последнего потомка — пачка приезжает батчами по CHUNK и живым потоком, и
// разрыв между ними не должен рвать группу надвое.
// --- tools:begin ---
function pour(box, items) {
  for (const it of items) {
    if (it.role !== 'tool') { box.insertAdjacentHTML('beforeend', renderItem(it)); continue; }
    let g = box.lastElementChild;
    if (!g || !g.classList.contains('tools')) {
      box.insertAdjacentHTML('beforeend', '<details class="msg tools"><summary></summary></details>');
      g = box.lastElementChild;
    }
    g.insertAdjacentHTML('beforeend', renderItem(it));
    g.firstElementChild.innerHTML = `${g.children.length - 1} · ${toolHead(it, false)}`;
  }
}
// --- tools:end ---

// Поток на панель: один EventSource — один транскрипт, и сервер помнит по нему свой
// оффсет сам. Открывается вместе с панелью, закрывается вместе с ней; переоткрывать
// приходится только когда панель меняет сессию, потому что адрес потока задан при
// создании и поменять его на лету EventSource не даёт.
const streams = new Map();

function watch(p) {
  unwatch(p);
  if (!p.session || dead) return;
  const q = new URLSearchParams({ project: p.project, id: p.session, from: p.next });
  const es = new EventSource('api/stream?' + q);
  es.onmessage = (e) => absorb(p, JSON.parse(e.data));
  // Переподключение после обрыва браузер делает сам. Но у вкладки с истёкшей сессией
  // SSO обрыв вечный: каждая попытка уходит редиректом на вход. Останавливает её тот же
  // счётчик отказов, что и опрос статуса — он ставит `dead`, а мы закрываем поток.
  // Случай «сервер ответил не 200» сюда не относится: его EventSource считает фатальным
  // и не повторяет вовсе, поднимает такой поток сторож в tick().
  es.onerror = () => { if (dead) unwatch(p); };
  streams.set(p.pane, es);
}

function unwatch(p) {
  streams.get(p.pane)?.close();
  streams.delete(p.pane);
}

function absorb(p, data) {
  const first = p.next === 0 || data.reset;
  p.next = data.next; save();
  // Транскрипт переписали, и сервер читает его заново: показанное относится к прежнему
  // содержимому. Стираем лог, иначе история встанет в панель дважды.
  if (data.reset) {
    const box = document.querySelector('#pane-' + p.pane + ' .log');
    if (box) box.innerHTML = '';
  }
  setCtx(p, data.ctx);
  if (!data.items.length) return;
  const box = document.querySelector('#pane-' + p.pane + ' .log');
  if (!box) return;
  const wasEnd = atEnd(box);
  // Свой промпт приехал из транскрипта — снимаем локальную копию и строку «в очереди»
  // вместе с ней. Место в логе задаёт транскрипт: локальная копия стоит в порядке
  // отправки, а он совпадает с настоящим только пока промпты идут по одному.
  // Совпадение снимаем по одному на отправку: тот же текст мог быть отправлен и раньше,
  // в истории он законный.
  const queue = echoes.get(p.pane) || [];
  for (const e of dropEcho(data.items, queue)) e.nodes.forEach(n => n?.remove());
  for (const it of data.items) if (it.role === 'assistant' && it.text) answers.set(p.pane, it.text);
  if (!queue.length) echoes.delete(p.pane);
  pour(box, data.items);
  wireCopy(box);
  if (wasEnd || first) box.scrollTop = 1e9;
  // Вставка прокрутки не вызывает, поэтому кнопку двигаем руками: лог вырос, и низ
  // уехал даже у того, кто не трогал колесо.
  const down = document.querySelector('#pane-' + p.pane + ' .down');
  if (down) down.hidden = atEnd(box);
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
  if (!canNotify()) return;
  if (!document.hidden) return;
  const where = p.project.split('/').pop();
  new Notification(`claude · ${where}`,
    { body: seconds ? `ответ готов за ${fmt(seconds)}` : 'ответ готов', tag: p.pane });
}

let ticks = 0;

async function tick() {
  if (dead) return;
  let st = { runs: [], errors: {} };
  try { st = await get('api/status'); } catch (e) { /* переживём до следующего тика */ }
  // Модель бота сменили командой `/model` — панели без своего выбора идут за ней.
  if (st.model && st.model.id !== botModel?.id) {
    botModel = st.model;
    refillModels();
  }
  // Только по живому ответу: у запасного `st` выше поля `limits` нет вовсе, и один
  // неудачный опрос — рестарт бота, моргнувший Traefik — гасил полоски до следующего
  // тика. Выглядело как «панель лимитов периодически прячется».
  if ('limits' in st) setPlan(st.limits, st.auth);
  trackRuns(st.runs || []);
  let running = 0;
  for (const p of panes) {
    const scope = 'web:' + p.pane;
    let el = document.getElementById('pane-' + p.pane);
    // Окно без заголовка — пустая секция: от неё остаются две рамки, на телефоне это
    // полоска в несколько пикселей поперёк экрана. Как она получается, поймать не
    // удалось (2026-09-26), поэтому чиним по факту: секция без шапки перерисовывается
    // из той же записи в `panes`, где лежит всё нужное — проект, сессия, оффсет.
    if (el && !el.querySelector('header')) {
      el.remove();
      drawPane(p);
      el = document.getElementById('pane-' + p.pane);
    }
    // Свой запуск — либо начатый этой панелью, либо любой другой над той же сессией:
    // из топика Telegram или из соседней панели. Скоупы у них разные, транскрипт один,
    // и без сопоставления по сессии панель молчала, пока в неё сыпались ответы.
    const mine = (st.runs || []).find(
      (r) => r.scope === scope || (p.session && r.session === p.session));
    const busy = !!mine;
    if (busy) running++;

    // Промпт вставал в очередь без сессии, claude придумал её уже в этом прогоне.
    // Ответ на отправку её не содержал, зато содержит статус — отсюда и берём.
    if (!p.session && mine && mine.scope === scope && mine.session) {
      p.session = mine.session;
      p.next = 0;
      save();
      watch(p);
      setWho(p);
      loadTree().catch(() => {});
    }

    // Сторож потока. EventSource переподключается сам только после разрыва живого
    // соединения; ответ не 200 — например 502 от Traefik, пока бот перезапускается —
    // он по спецификации считает фатальным и закрывается навсегда. Панель при этом
    // молчит, а баннер «офлайн» не появляется: /api/status отвечает как ни в чём не
    // бывало. Тик и так ходит раз в три секунды, поэтому проверка стоит сравнения.
    const es = streams.get(p.pane);
    if (p.session && !dead && (!es || es.readyState === EventSource.CLOSED)) watch(p);

    el?.classList.toggle('busy', busy);
    // Прогон кончился — «стоп» снова живая. Снимаем здесь, а не по ответу на отмену:
    // сервер отвечает раньше, чем процесс успевает умереть, и кнопка вернулась бы в
    // рабочий вид при всё ещё мигающем окне.
    if (el && !busy) {
      el.classList.remove('stopping');
      const btn = el.querySelector('.stop');
      if (btn) btn.disabled = false;
    }
    // Прогон только что кончился — в пустое поле встаёт серым промпт из ответа. Именно
    // переход, а не «панель свободна»: второе верно каждые три секунды, и подсказка
    // возвращалась бы поверх стёртого.
    //
    // Стоит ПОСЛЕ переключения `busy` и разблокировки «стоп» намеренно. Всё, что в этом
    // цикле стоит до них, при исключении оставляет окно навсегда занятым, с нажатой
    // кнопкой и без единой строки в консоли. Подсказка такой цены не стоит.
    if (wasBusy.has(p.pane) && !busy) {
      const hint = fromAnswer(answers.get(p.pane));
      hints.set(p.pane, hint);
      const box = el?.querySelector('textarea');
      if (box && !box.value) box.placeholder = hint || BLANK;
    }
    if (busy) wasBusy.add(p.pane); else wasBusy.delete(p.pane);

    const timer = el?.querySelector('.timer');
    if (timer) {
      const foreign = busy && mine.scope !== scope;
      // «+2» рядом с таймером: сколько промптов ждут своей очереди в этой панели.
      // Строка в логе о них тоже есть, но она не переживает перезагрузку страницы.
      const queued = (st.queued || {})[scope] || 0;
      timer.textContent = (busy ? (foreign ? '↗ ' : '') + fmt(mine.secs) : '')
                        + (queued ? ` +${queued}` : '');
      timer.title = foreign ? 'запуск начат не из этой панели' : '';
    }

    // Застрявшее эхо: панель свободна, очередь пуста — значит всё, что могло приехать
    // из транскрипта, приехало, и оставшиеся записи уже не встретятся никогда.
    sweepEchoes(p.pane, !busy && !((st.queued || {})[scope] || 0), Date.now());

    // Переход «занята → свободна» — единственный момент, когда есть что сообщить.
    if (busy) {
      lastElapsed.set(p.pane, mine.secs);
    } else if (lastElapsed.has(p.pane)) {
      notifyDone(p, lastElapsed.get(p.pane));
      lastElapsed.delete(p.pane);
      // Метим окно и держим метку до клика. Не метим единственный случай — окно впереди
      // и вкладка на экране: там ответ видно своими глазами. Раньше условие было только
      // про «впереди», и прогон, закончившийся в свёрнутой вкладке, следа не оставлял:
      // человек возвращался к панели, где всё выглядит как до запуска.
      // Точка в списке слева на этот случай не встаёт — она только для сессий без окна,
      // а системное уведомление уходит лишь при свёрнутой вкладке.
      const watched = el && el.classList.contains('act') && !document.hidden;
      if (el && !watched) {
        el.classList.add('ready');
        // Упал прогон или дошёл до конца — знак разный. Ошибку сервер держит по скоупу
        // до следующего запуска, поэтому она ещё здесь в тот же тик.
        el.classList.toggle('bad', !!(st.errors || {})[scope]);
      }
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

    // Ответ местной команды (`/cost`, `/model`, `/context`): claude отвечает на них сам
    // и в транскрипт ответ не пишет, поэтому поток его не принесёт — только статус.
    // Рисуем обычным ответом claude, потому что это он и есть.
    const said = (st.local || {})[scope];
    if (said) {
      if (shownLocal.get(p.pane) !== said) {
        shownLocal.set(p.pane, said);
        log(p, renderItem({ role: 'assistant', text: said }));
        wireCopy(document.querySelector('#pane-' + p.pane + ' .log'));
      }
    } else {
      shownLocal.delete(p.pane);
    }

    // Итог прогона: модель, время, цена, токены. Панель до сих пор просто гасила таймер,
    // хотя всё это лежит в том же `result`, из которого берётся текст ошибки.
    const stat = (st.stats || {})[scope];
    if (stat) {
      if (shownStats.get(p.pane) !== stat.at) {
        shownStats.set(p.pane, stat.at);
        log(p, `<div class="msg note">✓ ${esc(stat.text)}</div>`);
      }
    } else {
      shownStats.delete(p.pane);
    }
  }
  markList();
  // Число работающих панелей в заголовке вкладки: видно, даже когда браузер свёрнут.
  // Дописываем к TITLE, а не к слову «claude»: имя инстанса ставит сервер, и tick,
  // затирающий заголовок раз в три секунды, стирал его через секунды после загрузки.
  document.title = running ? `● ${running} · ${TITLE}` : TITLE;

  // Сессию могли начать в Telegram или в соседней панели — список слева должен это
  // увидеть сам, а не после перезагрузки страницы.
  // Каждый пятый тик перечитываем и проекты: папка, заведённая в дереве файлов,
  // становится проектом сама, без F5.
  if (++ticks % 5 === 0 && !$('find').value.trim()) loadTree().catch(() => {});
}

// Сайдбар: состояние переживает перезагрузку, но записывается только по клику. Первый
// заход решается шириной экрана — на телефоне 280 пикселей из 390 забирал список сессий,
// и на панель оставалось меньше трети. Записывай мы и этот выбор, один заход с телефона
// оставил бы сайдбар скрытым и на большом экране.
// --- fold:begin ---
// Сравнение со строкой, а не проверка на истинность: сохранённый '0' истинен сам по
// себе, и «показать» превратилось бы в «спрятать» на первой же перезагрузке.
const foldedAtStart = (saved, narrow) => saved === null ? narrow : saved === '1';
// --- fold:end ---
document.body.classList.toggle('folded',
  foldedAtStart(localStorage.getItem('folded'), NARROW.matches));
$('fold').onclick = () => {
  const on = !document.body.classList.contains('folded');
  document.body.classList.toggle('folded', on);
  localStorage.setItem('folded', on ? '1' : '0');
};

// Терминал. iframe создаётся при первом открытии и дальше только прячется: скрытый
// держит и websocket, и экран tmux, а новый начинал бы с пустого места. Сессия tmux
// одна и с постоянным именем, поэтому F5 возвращает тот же экран.
const term = $('term');
const showTerm = (on) => {
  document.body.classList.toggle('term', on);
  localStorage.setItem('term', on ? '1' : '0');
  if (on && !term.firstChild) term.innerHTML = '<iframe src="term/" title="терминал"></iframe>';
};
term.style.setProperty('--th', (localStorage.getItem('termh') || Math.round(innerHeight * 0.4)) + 'px');
if (localStorage.getItem('term') === '1') showTerm(true);

// Полоска и переключает, и тянет — разводим по расстоянию: сдвиг до четырёх пикселей
// считаем кликом, дальше начинается высота. Порог нужен пальцу, мышь и так точна.
$('termbar').onpointerdown = (e) => {
  if (e.button) return;
  e.preventDefault();
  const bar = $('termbar');
  bar.setPointerCapture(e.pointerId);
  const y0 = e.clientY, h0 = term.getBoundingClientRect().height || innerHeight * 0.4;
  let dragged = false;
  bar.onpointermove = (ev) => {
    if (!dragged && Math.abs(ev.clientY - y0) < 4) return;
    dragged = true;
    if (!document.body.classList.contains('term')) showTerm(true);
    // Пределы: ниже 60px от терминала нет толку, выше окна минус 80px исчезают панели.
    const h = Math.min(innerHeight - 80, Math.max(60, h0 + (y0 - ev.clientY)));
    term.style.setProperty('--th', h + 'px');
    localStorage.setItem('termh', Math.round(h));
  };
  bar.onpointerup = bar.onpointercancel = () => {
    bar.onpointermove = null;
    if (!dragged) showTerm(!document.body.classList.contains('term'));
  };
};

// Правый сайдбар спрятан по умолчанию, в отличие от левого: сессии нужны каждый заход,
// а дерево файлов — под задачу. Открыли хоть раз — состояние запоминается, и правило
// дефолта больше не действует.
document.body.classList.toggle('rfolded', localStorage.getItem('rfolded') !== '0');
$('rfold').onclick = () => {
  const on = !document.body.classList.contains('rfolded');
  document.body.classList.toggle('rfolded', on);
  localStorage.setItem('rfolded', on ? '1' : '0');
};
// Создание в открытом каталоге: файл открывается редактором, папка открывается в
// дереве — в обоих случаях оказываешься там, где только что создал. Удаляет крестик на
// строке дерева, но только файл и пустую папку; переименование по-прежнему к claude в
// соседней панели.
const create = (folder) => async () => {
  const name = prompt((folder ? 'имя папки в ' : 'имя файла в ') + (atDir || '?'));
  if (!name || !name.trim()) return;
  const made = await ask('api/new', { dir: atDir, name: name.trim(), folder }, 'не создать');
  if (!made) return;
  if (folder) return openDir(made.path).catch(treeFail);
  await openDir(atDir).catch(treeFail);
  addPane({ pane: uid(), file: made.path });
};

// Дерево перечитывает только себя: claude создаёт и удаляет файлы у себя в панели, и
// без этого приходилось перезагружать всю страницу, теряя раскладку окон.
$('rescan').onclick = () => openDir(atDir || $('root').value).catch(treeFail);
$('newfile').onclick = create(false);
$('newdir').onclick = create(true);

$('root').onchange = () => {
  localStorage.setItem('root', $('root').value);
  openDir($('root').value).catch(treeFail);
};

$('reload').onclick = () => location.reload();
$('find').oninput = scheduleFind;
$('empty').querySelector('.list').onclick = () => $('fold').click();
loadModels();
loadRoots().catch(treeFail);
loadTree().then(() => {
  // Оттенки панелей из localStorage: раздаём отсутствующие и разводим совпавшие.
  spreadHues();
  save();
  panes.forEach(p => { p.next = 0; drawPane(p); });
  // Кто впереди после F5. `act` держит z-index, и достаётся он последнему нарисованному,
  // то есть последнему в массиве — а там с переходом на `unshift` лежит самое старое
  // окно. Развёрнутое при этом уезжало за спину соседей: своего z-index у него нет,
  // на весь экран его растягивает геометрия, а не слой.
  const front = panes.find(x => x.prev) || panes[0];
  if (front) raise(document.getElementById('pane-' + front.pane));
  tick();
});
setInterval(tick, 3000);
