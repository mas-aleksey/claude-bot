"""Страница рабочего пространства целиком: разметка, стили и скрипт одной строкой.

Отдельным модулем, а не файлами в статике: сборки у панели нет и не заводится —
страница лежит в образе и отдаётся одним ответом. В `webui.py` этот литерал занимал
две трети файла и прятал за собой роутинг.

Синтаксис скрипта проверяет `tests/test_page_syntax.py` через `node --check`: опечатка
в JS иначе не видна ни ruff, ни тестам — образ соберётся, endpoint ответит 200, а
панели просто не появятся.
"""

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
body { margin:0; font:14px/1.5 system-ui,sans-serif; display:flex; flex-direction:column;
  height:100vh }
/* Верхний ряд: список, его полоска и поле панелей. Отдельной обёрткой, потому что под
   ним теперь живёт ящик терминала, и делить высоту им надо колонкой. */
#top { position:relative; flex:1; min-height:0; display:flex }
aside { width:280px; flex:none; border-right:1px solid #8884; display:flex; flex-direction:column }
body.folded aside:not(.files) { display:none }  /* дерево справа прячется своей полоской */
/* Полоса на левом краю области панелей. Видна всегда, в том числе когда сайдбар убран:
   иначе его нечем было бы вернуть. Стрелка — через `content`, чтобы состояние рисовал
   CSS, а не переписывал скрипт. */
#fold { flex:none; width:14px; padding:0; border:0; border-right:1px solid #8884;
  border-radius:0; opacity:.45; font-size:11px }
#fold:hover { opacity:1; background:#8882 }
#fold::before { content:'\2039' }
body.folded #fold::before { content:'\203A' }
/* Терминал — ящик у нижнего края, полоска над ним устроена как `#fold` слева: клик
   открывает и закрывает, а если её потянуть, ящик растёт вверх или вниз. Высота лежит
   в `--th` на самом ящике, поэтому её двигает один стиль, без перерисовки. */
#termbar { flex:none; width:100%; height:14px; padding:0; border:0;
  border-top:1px solid #8884; border-radius:0; opacity:.45; font-size:11px;
  cursor:ns-resize; touch-action:none }
#termbar:hover { opacity:1; background:#8882 }
#termbar::before { content:'\2303' }
body.term #termbar::before { content:'\2304' }
#term { flex:none; height:var(--th,40vh); min-height:0 }
body:not(.term) #term { display:none }
#term iframe { display:block; width:100%; height:100%; border:0 }
/* Правый сайдбар — дерево файлов. Устроен зеркально левому: своя полоска, свой класс
   на body, своя память в localStorage. Флаг отдельный, а не общий: списки прячутся
   независимо, и один класс схлопывал бы оба разом. */
aside.files { border-right:0; border-left:1px solid #8884 }
body.rfolded aside.files { display:none }
#rfold { flex:none; width:14px; padding:0; border:0; border-left:1px solid #8884;
  border-radius:0; opacity:.45; font-size:11px }
#rfold:hover { opacity:1; background:#8882 }
#rfold::before { content:'\203A' }
body.rfolded #rfold::before { content:'\2039' }
/* Хлебные крошки: путь от корня кнопками, каждая возвращает на свой уровень. Отдельной
   кнопки «наверх» поэтому нет. */
#crumb { padding:8px 8px 0; font-size:12px; word-break:break-all; opacity:.8 }
#crumb button { border:0; padding:1px 2px; border-radius:2px }
#crumb button:hover { background:#8882 }
#tree { overflow:auto; flex:1; margin-top:8px }
#tree .dir { font-weight:600 }
#tree .size { float:right; opacity:.5; font-size:11px }
#tree .none { padding:8px 10px; opacity:.5; font-size:12px }
#peers { display:flex; gap:2px; padding:8px 8px 0 }
#peers a { flex:1; text-align:center; padding:5px; border:1px solid #8884; border-radius:4px;
  text-decoration:none; color:inherit; font-size:13px }
#peers a[aria-current=page] { background:#8884; font-weight:600 }
aside select, aside button.new, aside input { margin:8px 8px 0; padding:6px }
aside .row { display:flex }
aside .row button.new { flex:1 }
aside .row button.new + button.new { margin-left:0 }
aside input { background:none; color:inherit; border:1px solid #8884; border-radius:4px;
  font:inherit }
#list .snip { display:block; font-size:11px; opacity:.6; margin-top:2px;
  overflow:hidden; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical }
#list { overflow:auto; flex:1; margin-top:8px }
#list button, #tree button { display:block; width:100%; text-align:left; padding:8px 10px; border:0;
  border-bottom:1px solid #8882; background:none; color:inherit; font:inherit; cursor:pointer }
#list button:hover, #tree button:hover { background:#8882 }
/* Открытая сессия залита цветом своей панели ровно как её заголовок, теми же числами,
   и мигает теми же кадрами `blink`. Строка слева и заголовок наверху — одно и то же
   окно, разный цвет заливки развёл бы их по ощущению.
   Заливка перебивает `:hover` выше, специфичность та же, а правило ниже. Поэтому у
   открытой строки свой ховер: тот же оттенок, гуще. Иначе она перестала бы отзываться
   на курсор, а серая подсветка поверх цвета панели всё равно врала бы про него. */
#list button.open { background:oklch(0.62 0.18 var(--hue,250) / .30) }
#list button.open:hover { background:oklch(0.62 0.18 var(--hue,250) / .45) }
#list button.busy { animation:blink 1.2s ease-in-out infinite }
/* Закрытая сессия мигает от прозрачного к цвету, открытая — внутри своей заливки: один
   и тот же `blink`, разная нижняя точка. Поэтому «идёт работа» и «вот это на экране»
   читаются порознь, без второго цвета и второй анимации.
   Точка — ответ пришёл в закрытое окно. Уплывает вправо следом за размером сессии и
   гаснет, как только сессию открыли. */
#list button.done::after { content:'\25CF'; float:right; margin-left:6px; font-size:10px;
  color:oklch(0.62 0.20 var(--hue,250)) }
#plan { flex:none; padding:8px 10px; border-top:1px solid #8884; font-size:12px }
#plan .who { opacity:.6; white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
#plan .lim { margin-top:6px }
#plan .lim em { font-style:normal; opacity:.75 }
#plan .lim span { float:right; opacity:.6 }
#plan .track { height:4px; margin-top:3px; border-radius:2px; background:#8883 }
#plan .fill { display:block; height:100%; border-radius:2px; background:oklch(0.62 0.18 250) }
#plan .fill.warn { background:#e90 }
#plan .fill.hot { background:#e55 }
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
header .timer { font-size:11px; opacity:.75; font-variant-numeric:tabular-nums; flex:none }

/* Занятость показывает сам заголовок: полоса во всю ширину панели. Точка 8x8 стояла
   тут раньше и не читалась вовсе, а рядом с мигающим заголовком была третьим сигналом
   после него и таймера. Насыщенность в ярком кадре умеренная: в заголовке лежит текст,
   и заливка в полную силу его бы утопила. */
section.busy header { animation:blink 1.2s ease-in-out infinite }
@keyframes blink { 50% { background:oklch(0.68 0.21 var(--hue,250) / .70) } }
/* Без движения подсказка обязана остаться: раньше правило просто убирало анимацию, и
   занятость становилась совсем невидимой. Теперь заголовок просто горит ярко. */
@media (prefers-reduced-motion: reduce) {
  #list button.busy { animation:none;
    background:oklch(0.68 0.21 var(--hue,250) / .70) }
  section.busy header { animation:none;
    background:oklch(0.68 0.21 var(--hue,250) / .70) }
}
/* Занятый контекст — полоска в нижней кромке заголовка: места не занимает, а через всю
   сетку видно, какая панель подошла к пределу. Без чисел и подсказки: текст в узкой
   панели вытеснил бы название сессии, а title на полоске в два пикселя недостижим —
   имя сессии растянуто на всю ширину и перекрывает его своим, даже пустым. */
header .ctx { position:absolute; left:0; bottom:0; height:2px; width:0;
  background:oklch(0.62 0.18 var(--hue,250)) }
header .ctx.full { background:#e55 }
header button { padding:1px 6px; line-height:1.2 }
/* Пальцем в кнопку высотой 21 пиксель не попасть — растим сами кнопки, заголовок
   поднимается следом. Признак — `pointer: coarse`, а не ширина экрана: на планшете в
   альбомной ориентации экран широкий, а палец тот же. По этому же признаку скрипт не
   отправляет промпт по Enter. Перенос окна от этого не страдает: `wireGrab` и так
   пропускает жест, начатый на кнопке. */
@media (pointer: coarse) {
  header { padding:6px 8px; gap:8px }
  header button { min-width:40px; min-height:40px; padding:4px 10px }
}
/* Значок разворота — через `content`, чтобы состояние окна рисовал CSS, а не переписывал
   скрипт: та же механика, что у полоски сайдбара. */
header .max::before { content:'\2922' }
section.zoomed header .max::before { content:'\2921' }
/* Обёртка нужна только как система координат для кнопки «вниз»: внутри самого лога
   абсолютная кнопка уехала бы вместе с прокруткой, а снаружи ей не на что опереться —
   высота лога известна только здесь. */
.logbox { position:relative; flex:1; min-height:0; display:flex }
.log { flex:1; overflow:auto; padding:12px 14px }
/* Полоска во всю ширину лога, устроена как #termbar у окна: узкая, в тоне панели,
   поверх текста. Полупрозрачная нарочно — сквозь неё видно последнюю строку, поэтому
   низ лога не читается как обрыв, а промахнуться по ней нельзя даже пальцем.
   Видна, только пока лог отлистан от низа. */
.down { position:absolute; left:0; right:0; bottom:0; height:18px; padding:0;
  display:grid; place-items:center; border:0; border-radius:0; font-size:11px;
  line-height:1; opacity:.75; background:oklch(0.62 0.18 var(--hue,250) / .35) }
.down:hover { opacity:1; background:oklch(0.62 0.18 var(--hue,250) / .55) }
.down[hidden] { display:none }
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
pre .copy { position:sticky; float:right; top:0; right:0; opacity:0;
  font:inherit; font-size:11px; padding:1px 5px; cursor:pointer; color:inherit;
  background:Canvas; border:1px solid #8884; border-radius:3px }
pre:hover .copy, pre .copy:focus { opacity:.9 }
.body code { font-family:ui-monospace,monospace; font-size:.92em }
.body :not(pre) > code { background:#8882; padding:.1em .3em; border-radius:3px }
.body table { border-collapse:collapse; margin:.4em 0; font-size:.95em }
.body th, .body td { border:1px solid #8884; padding:2px 6px; text-align:left }
.body a { color:#7ad }
.body blockquote { margin:.4em 0; padding-left:.8em; border-left:3px solid #8884; opacity:.85 }
.assistant { border-left:3px solid #88f; padding-left:10px }
.tool { opacity:.65; font-size:13px; font-family:ui-monospace,monospace }
/* Раскрытый шаг: аргумент столбиком, как он и был набран. Маркер остаётся штатный —
   свой треугольник рисовать незачем. */
details.tool summary { cursor:pointer }
/* Пачка вызовов: одна строка со счётчиком и последним вызовом. Заголовок не переносим —
   иначе свёрнутая группа занимает столько же места, сколько развёрнутая. */
.tools > summary { cursor:pointer; opacity:.65; font-size:13px;
  font-family:ui-monospace,monospace; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis }
.tools > :not(summary) { margin:2px 0 0 1.2em }
details.tool pre { margin:4px 0 0 1.2em; padding:6px; background:#8881; border-radius:4px;
  overflow:auto; white-space:pre-wrap }
.note { opacity:.45; font-size:12px; font-style:italic }
.err { color:#e55 }
.role { display:block; font-size:11px; text-transform:uppercase; opacity:.5 }
/* Композер: рамка одна на всё, поле внутри без своей, под ним ряд управления. Раньше
   тут стояли в ряд три рамки разной высоты — селект, скрепка и поле.
   Правый отступ 20px — под ручку .h-se: она лежит в том же углу, и кнопка отправки
   вплотную к краю её бы накрыла. */
form { position:relative; display:flex; flex-direction:column; gap:4px;
  margin:8px 20px 8px 8px; padding:6px;
  border:1px solid #8886; border-radius:12px;
  background:oklch(0.62 0.16 var(--hue,250) / .05) }
form:focus-within { border-color:oklch(0.62 0.20 var(--hue,250) / .7) }
/* Сворачивание в заголовок — мобильное, правила лежат в медиаблоке внизу. На большом
   экране оно бессмысленно: окна стоят в явных клетках сетки, освободившиеся ряды никто
   не занимает, и свёрнутое оставляло бы под собой дыру. Кнопка спрятана здесь, а не
   показана там, чтобы состояние `roll` могло пережить переход между экранами: панель
   с телефона открывают на десктопе целой, а не полоской заголовка без кнопки. */
header .foldbar { display:none }
/* Поле правки во всю высоту окна. Потолок в 240px ниже поставлен композеру, здесь он
   не нужен — окно тянется само, и файл должен занимать его целиком.
   `white-space:pre` и `wrap=off`: перенос длинной строки сдвинул бы нумерацию строк в
   голове у читающего, а код чаще смотрят по строкам, чем читают сплошняком. */
.edit { flex:1; min-height:0; max-height:none; margin:8px; padding:6px;
  border:1px solid #8886; border-radius:8px; font:12px/1.45 ui-monospace,monospace;
  white-space:pre; overflow:auto }
.edit:focus { border-color:oklch(0.62 0.20 var(--hue,250) / .7) }
.filebar { display:flex; gap:8px; align-items:center; margin:0 8px 8px }
.filebar .state { flex:1; font-size:12px; opacity:.6; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis }
.filebar .state.bad { color:#e55; opacity:1 }
textarea { position:relative; resize:none; min-height:40px; max-height:240px;
  padding:4px 4px 0; font:inherit; background:none; color:inherit; border:0; outline:none }
/* Подсветка слеш-команды: залить текст внутри textarea нельзя, поэтому под полем лежит
   слой с той же геометрией, и в нём — одна метка на первое слово. Остального текста в
   слое нет специально: метка стоит в начале, её место не зависит от того, что дальше,
   и переносы повторять не нужно. Прокрутку поля слой повторяет за скриптом. */
.ghost { position:absolute; left:6px; right:6px; top:6px; max-height:240px;
  overflow:hidden; padding:4px 4px 0; font:inherit; color:transparent;
  white-space:pre-wrap; pointer-events:none }
.ghost mark { color:transparent; border-radius:4px;
  background:oklch(0.62 0.16 var(--hue,250) / .25) }
/* Подсказка по `/`: над композером, поверх лога. Выбранная строка — заливка того же
   тона, что и панель. */
.menu { position:absolute; left:0; right:0; bottom:100%; z-index:5; margin-bottom:4px;
  max-height:200px; overflow:auto; background:Canvas; border:1px solid #8886;
  border-radius:8px; box-shadow:0 6px 20px #0005 }
.menu[hidden] { display:none }
.menu div { padding:4px 8px; cursor:pointer; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis }
.menu div[aria-selected=true] { background:oklch(0.62 0.16 var(--hue,250) / .25) }
.menu b { font-weight:600 }
.menu i { opacity:.55; font-style:normal; font-size:12px }
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
/* Пустая область — не надпись, а два действия: открыть список и завести сессию. Текст
   тут раньше указывал «слева», а сайдбар на узком экране свёрнут по умолчанию и лежит
   поверх панелей — указывать было не на что, а вернуть его можно только полоской в 14
   пикселей. Кнопка списка не нужна, когда список и так открыт. */
#empty { grid-column:1/-1; margin:auto; display:flex; flex-direction:column; gap:8px }
/* Своё `display` перебивает `hidden` из стилей браузера — та же ловушка, что у баннера
   ниже: с открытой панелью кнопки оставались на экране под ней. */
#empty[hidden] { display:none }
#empty button { padding:8px 14px }
body:not(.folded) #empty .list { display:none }
/* Поверх всего и по центру верха: опрос встал, и пока человек не обновит страницу,
   ничего живого в панелях больше не появится. */
#dead { position:fixed; z-index:50; top:12px; left:50%; transform:translateX(-50%);
  display:flex; gap:10px; align-items:center; padding:10px 14px; border-radius:8px;
  background:#e5533a; color:#fff; box-shadow:0 6px 24px #0006 }
/* Своё `display` перебивает `hidden` из стилей браузера — без этой строки баннер
   висел бы на экране с самого открытия страницы. */
#dead[hidden] { display:none }
#dead button { border-color:#fff8 }
/* Узкий экран: доли области дали бы панель в 30px шириной. Раскладываем столбиком и
   отключаем ручки — тянуть тут всё равно нечего.
   Сайдбар ложится поверх панелей, а не отнимает у них колонку: на 390 пикселях он
   забирал 280 и окно оставалось в сотню. Поля области убраны совсем — окно идёт от
   края до края, и единственное, что у экрана отъедено, это полоска возврата к списку.
   Ряды по содержимому, а высота задана самому окну: свёрнутое в заголовок иначе
   держало бы под собой пустые 70vh своего ряда.
   align-content:start — не украшение: по умолчанию grid растягивает auto-ряды, раздавая
   остаток высоты между ними поровну. Окно при этом своего размера не меняет, и довесок
   ряда вылезает пустотой под ним — свёрнутое в заголовок выглядело так, будто оно и не
   свернулось. */
@media (max-width: 700px) {
  #top { --aw:min(280px, 85vw) }
  aside { position:absolute; z-index:10; left:0; top:0; height:100%;
    width:var(--aw); background:Canvas; box-shadow:0 0 24px #0007 }
  /* Открытый сайдбар лежит поверх панелей и накрывает собой полоску возврата: на
     телефоне спрятать список было нечем. Пока он открыт, полоска уезжает к его правому
     краю и поднимается над ним. Ширина вдвое против настольной — 14px пальцем не берутся. */
  #fold { width:24px }
  body:not(.folded) #fold { position:absolute; z-index:11; left:var(--aw); top:0; height:100% }
  aside.files { left:auto; right:0 }
  #rfold { width:24px }
  body:not(.rfolded) #rfold { position:absolute; z-index:11; left:auto; right:var(--aw);
    top:0; height:100% }
  #panes { overflow:auto; padding:0; gap:6px; grid-template-columns:1fr;
    grid-template-rows:none; grid-auto-rows:auto; align-content:start }
  section { grid-column:1/-1 !important; grid-row:auto !important;
    height:min(70vh, 480px); border-radius:0; border-left:0; border-right:0 }
  /* Окно сворачивается в свой заголовок: остаётся полоса с именем сессии, таймером и
     кнопками, а всё, что лежало ниже, поднимается вплотную. Состояние живёт в панели и
     переживает F5. Ручки в списке скрытого нет — на этом экране её и так нет. */
  header .foldbar { display:revert }
  section.rolled { height:auto }
  section.rolled .logbox, section.rolled form { display:none }
  section.rolled .edit, section.rolled .filebar { display:none }
  /* «Во весь экран» тут не про клетки сетки — их перебивает `!important` выше, и кнопка
     раньше просто ничего не делала. Окно выходит из потока и накрывает экран целиком,
     включая полоску терминала: это единственный способ растянуть лог, раз ресайза на
     телефоне нет. z-index выше сайдбаров (10) и их полосок (11), но ниже плашки `#dead`.
     Свёрнутое разворачивать некуда — там нечего показывать, кроме заголовка. */
  section.zoomed { position:fixed; inset:0; z-index:20; height:auto }
  /* Пока окно развёрнуто, соседей не просто не видно — их нет в отрисовке. Safari на iOS
     уводит `position:fixed` внутри прокручиваемого #panes в отдельный слой, и соседнее
     окно всплывает поверх, хотя z-index у него меньше (первым — заголовок свёрнутого,
     единственное, что от него осталось). Спорить со слоями бесполезно, а скрытое не
     всплывает. Прячем всех, кроме самого развёрнутого: соседу мало не иметь класса
     `zoomed`, он всплывал и свёрнутым, с ним заодно. */
  #panes:has(> section.zoomed) > section { display:none }
  #panes:has(> section.zoomed) > section.zoomed { display:flex }
  #tile { display:none }   /* раскладка по клеткам, а клеток тут нет */
  .grip { touch-action:auto; cursor:default }   /* жест по заголовку — прокрутка, не перенос */
  form { margin:8px }   /* правый отступ был под ручку, а её тут нет */
  .h { display:none }
  /* Safari на iOS зумит страницу при фокусе в поле с текстом мельче 16px и обратно уже
     не отъезжает: композер уезжает вправо, кнопка отправки — за край экрана. Порог ровно
     16px, и лечится он размером шрифта, а не `maximum-scale` в viewport: тот отнял бы у
     страницы и ручной зум. `.ghost` в списке обязателен — слой подсветки слеш-команды
     обязан совпадать с полем по геометрии, иначе метка съедет. */
  textarea, input, select, .edit, .ghost { font-size:16px }
}
</style></head><body>
<div id=top>
<aside>
  <nav id=peers></nav>
  <select id=proj></select>
  <button class=new id=new>+ новая сессия</button>
  <button class=new id=tile title="расставить открытые окна поровну, без перекрытий">
    разложить окна</button>
  <input id=find type=search placeholder="поиск по сессиям проекта">
  <button class=new id=purge title="удалить старые сессии во всех проектах">
    очистить старше 2 дней</button>
  <div id=list></div>
  <div id=plan hidden></div>
</aside>
<button id=fold title="список сессий" aria-label="скрыть или показать список сессий"></button>
<div id=panes><div id=empty>
  <button class=list>список сессий</button>
  <button class=fresh>+ новая сессия</button>
</div></div>
<button id=rfold title="дерево файлов" aria-label="скрыть или показать дерево файлов"></button>
<aside class=files>
  <select id=root title="корень дерева"></select>
  <div class=row>
    <button class=new id=dots title="показывать файлы с точкой в начале">скрытые: вкл</button>
    <button class=new id=newfile title="создать файл в открытом каталоге">+ файл</button>
  </div>
  <div id=crumb></div>
  <div id=tree></div>
</aside>
</div>
<button id=termbar title="терминал: клик открывает и закрывает, потянуть — высота"
  aria-label="терминал"></button>
<div id=term></div>
<div id=dead hidden>бот не отвечает или кончилась сессия входа
  <button id=reload>обновить страницу</button></div>
<script>
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
  document.title = '⚠ claude';
  $('dead').hidden = false;
}
// --- dead:end ---
const post = (u, body) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body) }).then(payload);
const esc = (s) => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));

// Оттенки для панелей: шесть штук по кругу, светлота и насыщенность заданы в CSS.
// Берём первый незанятый, чтобы соседние панели не совпали по цвету.
// Первые четыре разнесены максимально: синий, красно-оранжевый, зелёный, пурпурный.
// Дальше циан и янтарный. Насыщенность и светлота заданы в CSS одинаковыми для всех,
// поэтому панели отличаются только тоном и выглядят одной семьёй.
const HUES = [250, 25, 145, 305, 195, 60];
const freeHue = () => {
  const used = new Set([...panes.map(x => x.hue), ...Object.values(hues)]);
  return HUES.find(h => !used.has(h)) ?? HUES[panes.length % HUES.length];
};

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
// ponytail: застрявшая запись остаётся в очереди навсегда и однажды съест законный
// повтор того же текста. Начнёт мешать — хранить рядом время отправки и выбрасывать
// старше нескольких минут.
function dropEcho(items, queue) {
  return items.filter((it) => {
    if (it.role !== 'user' || !queue.length) return true;
    const at = queue.indexOf(norm(it.text));
    if (at < 0) return true;
    queue.splice(at, 1);
    return false;
  });
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
  markList();
  syncTitles();
}

// --- mark:begin ---
// Строка сессии в списке слева несёт три разных вещи, и они складываются:
//   заливка   — эта сессия открыта в панели, вот она на экране;
//   мигание   — над сессией идёт запуск, чей угодно: панели, закрытой панели, Telegram;
//   точка     — запуск кончился, а окна не было, то есть ответ никто не видел.
// Метки кладём отдельным проходом, а не в разметку строки в `fillList`: список
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
  for (const b of document.querySelectorAll('#list button')) {
    const id = b.dataset.id;
    const p = panes.find(x => x.session === id);
    const hue = p ? (p.hue ?? HUES[0]) : hues[id];
    b.classList.toggle('open', !!p);
    b.classList.toggle('busy', busySessions.has(id));
    b.classList.toggle('done', done.has(id));
    b.title = done.has(id) ? 'ответ пришёл, пока окно было закрыто' : '';
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
  for (const b of document.querySelectorAll('#list button')) {
    const p = panes.find(x => x.session === b.dataset.id);
    if (!p || !b.dataset.title || p.title === b.dataset.title) continue;
    p.title = b.dataset.title;
    setWho(p);
    changed = true;
  }
  if (changed) save();  // один раз на проход: `panes` уезжает в localStorage целиком
}
// --- mark:end ---

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
// Размеры новой панели по убыванию: четверть области, полоса, столбец, восьмая. Пятая
// сессия раньше ложилась поверх первой — свободной четверти уже не было, и место
// подбиралось только под один размер. Теперь окно ужимается, пока не встанет рядом:
// шестушками в сетку 12x8 влезает шестнадцать штук.
const SIZES = [[W, H], [W, H / 2], [W / 2, H], [W / 2, H / 2], [3, 2]];

function place(p) {
  const busy = (c, r) => panes.some(x => x !== p && x.c <= c && c < x.c + x.w &&
                                                    x.r <= r && r < x.r + x.h);
  const free = (c, r) => {
    for (let i = 0; i < p.w; i++)
      for (let j = 0; j < p.h; j++)
        if (busy(c + i, r + j)) return false;
    return true;
  };
  for (const [w, h] of SIZES) {
    p.w = w; p.h = h;
    for (let r = 1; r <= ROWS - h + 1; r++)
      for (let c = 1; c <= COLS - w + 1; c++)
        if (free(c, r)) { p.c = c; p.r = r; return; }
  }
  retile();  // свободного места нет вовсе — раскладываем всё заново, поровну
}

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

// Плитка на всех: столбцов — корень из числа окон, дальше по рядам. Нужна ровно там,
// где подбор места бессилен: четыре окна по четверти занимают сетку целиком, и пятому
// некуда встать, как его ни ужимай. Расставляет и уже открытые — молча ложиться поверх
// них хуже, чем подвинуть их один раз на глазах.
function retile() {
  const cols = Math.ceil(Math.sqrt(panes.length));
  const rows = Math.ceil(panes.length / cols);
  const w = Math.max(1, Math.floor(COLS / cols)), h = Math.max(1, Math.floor(ROWS / rows));
  panes.forEach((x, i) => {
    x.w = w; x.h = h;
    x.c = 1 + (i % cols) * w;
    x.r = 1 + Math.floor(i / cols) * h;
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
    // На узком экране сетка перебита `!important`, и перенос там ничего не двигал —
    // зато молча писал новые `c`/`r` в панель, и перекос вылезал на большом экране.
    // Проверяем в момент жеста, а не при создании: окно поворачивают и меняют размер.
    if (NARROW.matches) return;
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
  panes.push(p);
  if (!p.c) place(p);
  save();
  drawPane(p);
  markList();
}

function closePane(p) {
  // Цвет отдаём строке: панели больше нет, а мигать закрытая сессия обязана тем же.
  if (p.session) { hues[p.session] = p.hue ?? HUES[0]; saveMarks(); }
  unwatch(p);
  panes = panes.filter(x => x.pane !== p.pane); save();
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
      <span class=who></span>
      <span class=timer></span>
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
      <textarea placeholder="промпт" title="${TOUCH.matches ? 'кнопка ↑ — отправить'
        : 'Enter — отправить, Shift+Enter — перенос строки'}"></textarea>
      <div class=bar>
        <label class=clip title="прикрепить файлы">+<input type=file multiple></label>
        <select class=model title="модель этой панели"></select>
        <button class=send title="отправить">↑</button>
        <button class=stop type=button title="остановить">■</button>
      </div>
    </form>`;
  $('panes').append(el);
  setWho(p, el);
  el.querySelector('.close').onclick = () => closePane(p);
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
  el.querySelector('.stop').onclick = () => post('api/cancel', { pane: p.pane })
    .then(r => r.dropped && log(p, `<div class="msg note">из очереди отброшено: ${r.dropped}</div>`))
    .catch(() => {});
  const form = el.querySelector('form');
  const ta = el.querySelector('textarea');
  // Выбор файлов — тот же путь, что у перетаскивания. `value = ''` нужен, чтобы второй
  // выбор того же файла тоже дал событие.
  const pick = el.querySelector('.clip input');
  pick.onchange = () => { attach(p, ta, pick.files); pick.value = ''; };
  form.onsubmit = (e) => { e.preventDefault(); send(p, ta); };
  wireSlash(p, el, ta);

  const model = el.querySelector('.model');
  fillModels(p, model);
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
  el.style.setProperty('--hue', p.hue ?? HUES[0]);
  el.querySelector('header').classList.add('grip');
  wireHandles(p, el);
  el.onpointerdown = () => raise(el);
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
      <span class=who></span>
      <button class=max title="во весь экран"></button>
      <button class=foldbar title="свернуть окно в заголовок">▾</button>
      <button class=close title="закрыть окно">×</button>
    </header>
    <textarea class=edit spellcheck=false wrap=off></textarea>
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

  let version = null, dirty = false;
  const load = () => get('api/file?path=' + encodeURIComponent(p.file)).then(f => {
    version = f.version;
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

// Скрытые файлы показаны по умолчанию: в /root/.claude половина интересного начинается
// с точки. Переключатель их прячет, память — в localStorage.
const showDots = () => localStorage.getItem('dots') !== '0';

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
  const rows = entries.filter(e => showDots() || !e.name.startsWith('.'));
  $('tree').innerHTML = rows.map(e =>
    `<button class="${e.dir ? 'dir' : ''}" data-path="${esc(e.path)}" data-dir="${e.dir ? 1 : ''}">`
    + esc(e.name) + (e.dir ? '/' : `<span class=size>${kb(e.size)}</span>`) + '</button>').join('')
    || '<div class=none>пусто</div>';
  for (const b of $('tree').querySelectorAll('button'))
    b.onclick = () => b.dataset.dir ? openDir(b.dataset.path).catch(treeFail)
                                    : addPane({ pane: uid(), file: b.dataset.path });
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
function grow(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
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

function setPlan(lim) {
  const box = $('plan');
  // Подпись возраста меняется сама по себе, без нового ответа сервера, поэтому входит
  // в ключ сравнения: иначе блок перерисовался бы только раз в две минуты и врал бы
  // «только что» всё это время.
  const label = lim?.at ? since(Date.now() / 1000 - lim.at) : '';
  const j = JSON.stringify(lim || null) + '|' + label +
    '|' + (lim?.bars || []).map(b => until(b.resets)).join();
  if (box.dataset.j === j) return;
  box.dataset.j = j;
  if (!lim || !(lim.bars || []).length) { box.hidden = true; return; }
  box.hidden = false;
  const who = [lim.email, lim.plan, label].filter(Boolean).join(' · ');
  box.innerHTML = `<div class=who title="${esc(who)}">${esc(who)}</div>` + lim.bars.map((b) => {
    const p = Math.max(0, Math.min(100, b.percent));
    const cls = (b.severity && b.severity !== 'normal') || p >= 90 ? ' hot' : p >= 75 ? ' warn' : '';
    const when = b.resets ? 'сброс ' + new Date(b.resets).toLocaleString() : 'время сброса неизвестно';
    const left = until(b.resets);
    return `<div class=lim title="${esc(when)}"><em>${esc(b.name)}</em>` +
      `<span>${left ? `через ${esc(left)} · ` : ''}${p}%</span>
      <div class=track><i class="fill${cls}" style="width:${p}%"></i></div></div>`;
  }).join('');
}

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
  ta.oninput();  // не только высота: с текстом уходит и подсветка команды
  // Момент отправки — единственный жест пользователя, на котором браузер позволяет
  // спросить разрешение. На загрузке страницы Safari и Chrome такой запрос игнорируют.
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
  echoes.set(p.pane, [...(echoes.get(p.pane) || []), norm(prompt)]);
  const line = log(p, `<div class="msg user"><span class=role>ты</span>${linkify(esc(prompt))}</div>`);
  try {
    const r = await post('api/prompt', { pane: p.pane, project: p.project,
      session: p.session || null, prompt, model: p.model || null });
    // Панель занята: промпт принят и ждёт. Сессию, если она ещё не заведена, панель
    // подберёт в tick() из /api/status — к ответу на отправку её просто нет.
    if (r.queued) { log(p, `<div class="msg note">в очереди: впереди ${r.queued}</div>`); return; }
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
      loadSessions();
    }
  } catch (code) {
    // Промпт до claude не доехал, значит из транскрипта он не вернётся. Оставленная
    // запись в `echoes` встала бы в голову очереди навсегда, и каждый следующий промпт
    // этой панели печатался бы дважды — локально и из транскрипта.
    // Снимаем одну запись, а не все совпадения: тот же текст мог быть отправлен и
    // раньше, успешно, и его эхо в очереди законное.
    const queue = echoes.get(p.pane) || [];
    const at = queue.lastIndexOf(norm(prompt));
    if (at >= 0) queue.splice(at, 1);
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
  // Свой же промпт, уже напечатанный локально, из транскрипта не берём — иначе он
  // стоит в панели дважды. Снимаем по одному совпадению на отправку: тот же текст мог
  // быть отправлен и раньше, в истории он законный.
  const queue = echoes.get(p.pane) || [];
  const shown = dropEcho(data.items, queue);
  if (!queue.length) echoes.delete(p.pane);
  pour(box, shown);
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
  if ('limits' in st) setPlan(st.limits);
  trackRuns(st.runs || []);
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

    // Промпт вставал в очередь без сессии, claude придумал её уже в этом прогоне.
    // Ответ на отправку её не содержал, зато содержит статус — отсюда и берём.
    if (!p.session && mine && mine.scope === scope && mine.session) {
      p.session = mine.session;
      p.next = 0;
      save();
      watch(p);
      setWho(p);
      loadSessions().catch(() => {});
    }

    // Сторож потока. EventSource переподключается сам только после разрыва живого
    // соединения; ответ не 200 — например 502 от Traefik, пока бот перезапускается —
    // он по спецификации считает фатальным и закрывается навсегда. Панель при этом
    // молчит, а баннер «офлайн» не появляется: /api/status отвечает как ни в чём не
    // бывало. Тик и так ходит раз в три секунды, поэтому проверка стоит сравнения.
    const es = streams.get(p.pane);
    if (p.session && !dead && (!es || es.readyState === EventSource.CLOSED)) watch(p);

    el?.classList.toggle('busy', busy);
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
const drawDots = () => { $('dots').textContent = 'скрытые: ' + (showDots() ? 'вкл' : 'выкл'); };
$('dots').onclick = () => {
  localStorage.setItem('dots', showDots() ? '0' : '1');
  drawDots();
  openDir(localStorage.getItem('dir') || $('root').value).catch(treeFail);
};
drawDots();
// Создание файла: пустой файл в открытом каталоге, дальше он сам открывается панелью
// редактора. Каталогов и удаления тут нет — это к claude в соседней панели, там об
// этом можно сказать словами.
$('newfile').onclick = async () => {
  const name = prompt('имя файла в ' + (atDir || '?'));
  if (!name || !name.trim()) return;
  const r = await fetch('api/file/new', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir: atDir, name: name.trim() }) });
  const body = await r.text();
  if (!r.ok) return alert(body || ('не создать: ' + r.status));
  await openDir(atDir).catch(treeFail);
  addPane({ pane: uid(), file: JSON.parse(body).path });
};

$('root').onchange = () => {
  localStorage.setItem('root', $('root').value);
  openDir($('root').value).catch(treeFail);
};

$('reload').onclick = () => location.reload();
$('proj').onchange = () => { $('find').value = ''; loadSessions(); };
$('find').oninput = scheduleFind;
$('empty').querySelector('.list').onclick = () => $('fold').click();
$('empty').querySelector('.fresh').onclick = () => $('new').click();
$('tile').onclick = () => { retile(); save(); };
$('new').onclick = () => addPane({ pane: uid(), project: $('proj').value, session: null, next: 0 });
loadPeers();
loadModels();
loadRoots().catch(treeFail);
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
