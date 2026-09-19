"""Рабочее пространство в браузере: несколько сессий на одной странице.

Живёт в процессе бота, в netns dind — у сессий из браузера ровно то же окружение, что
у сессий из Telegram: `localhost:2375`, те же порты тестовых контейнеров, те же тома.
Отдельным контейнером на сети `proxy` этого не получить, а маршрут из netns даёт хост:
порт публикуется на шлюзе сети proxy, Traefik ходит на него так же, как на AdGuard.

Параллельность бесплатна — `runner._runs` уже словарь, а скоупом служит id панели из
браузера. Панель живёт в localStorage, поэтому её скоуп переживает перезагрузку страницы
и кнопка «стоп» после F5 бьёт по своему запуску.

Вывод берётся из транскрипта, а не из процесса: claude пишет файл по ходу, сервер
тейлит его с байтового оффсета и отдаёт панели потоком SSE. Поэтому запуск, начатый в
Telegram, виден в браузере тем же механизмом, что и свой.

Один запуск на панель одновременно; промпт, присланный в занятую панель, встаёт в
очередь `runner.slot` и уходит сам, когда освободится место.
Гонять одну сессию из двух мест одновременно никто не мешает —
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
from page import PAGE

log = logging.getLogger("claude_bot.webui")

# id сессии приходит от клиента и подставляется в имя файла. Пропускаем только то,
# чем claude их и называет — uuid: ни слешей, ни точек, ни `..`.
SESSION_RE = re.compile(r"[0-9a-fA-F-]{8,64}\Z")
# id панели генерит браузер, а он становится ключом в `runner._runs` и попадает в логи.
PANE_RE = re.compile(r"[0-9a-zA-Z-]{4,64}\Z")

# Элементов в одном кадре. Транскрипт бывает на десятки тысяч строк, а страница должна
# отрисоваться сразу — остальное доедет следующими кадрами с того же оффсета.
CHUNK = 3000
# Потолок раскрытого аргумента шага. Медиана шага в песочнице — 244 символа, p90 —
# около 1700, но встречаются и тридцатитысячные: такой развернули бы лог на весь экран,
# а прочесть его всё равно негде. Обрезанную строку показываем без раскрытия.
FULL_ARG = 2000
# Как часто сервер смотрит на хвост транскрипта. Чтение стоит дописанных байт, поэтому
# частота ограничена не ценой, а тем, что быстрее человек всё равно не заметит.
TAIL_TICK = 0.3
# Холостых заходов между служебными комментариями в молчащем потоке — примерно 20 секунд.
PING_EVERY = 60
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

# Ответ местной команды claude (`/cost`, `/model`, `/context`) по скоупу. Такие команды
# claude отвечает сам, не обращаясь к модели, и в транскрипт ответ не пишет — там
# остаются только пометка клиента и имя команды. Панель читает транскрипт, поэтому без
# этого словаря она показывала серую пометку и молчание. В Telegram то же самое видно
# всегда: он рендерит поток событий, а не файл.
_local: dict[str, str] = {}

# Итог последнего прогона по скоупу: `{"at": время, "text": строка}`. Живёт до начала
# следующего запуска в той же панели, как и `_errors`. Время нужно панели, чтобы отличить
# новый итог от уже показанного: два прогона подряд могут дать посимвольно равный текст.
_stats: dict[str, dict] = {}


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


def items(path: Path, start: int) -> tuple[int, list[dict], dict | None]:
    """С байтового оффсета `start`: (оффсет конца прочитанного, элементы, контекст).

    Оффсет, а не номер строки: по номеру пришлось бы каждый раз пролистывать файл с
    начала, и на сорока мегабайтах это полторы сотни миллисекунд на каждый опрос. С
    `seek` цена запроса — только дописанный хвост.

    Занятость контекста считается этим же проходом, а не вторым по всему файлу. `None`
    означает «в этом куске нечего сказать» — панель оставляет прошлое значение.

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
    used = model = None
    with path.open("rb") as f:
        f.seek(start)
        for raw in f:
            # Строка без перевода — это событие, которое claude прямо сейчас дописывает.
            # Съесть половину и сдвинуть оффсет значит потерять его целиком, поэтому
            # останавливаемся до следующего захода. С опросом раз в 300 мс попасть в
            # середину записи куда вероятнее, чем раз в три секунды.
            if not raw.endswith(b"\n"):
                break
            start += len(raw)
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            role = ev.get("type")
            if role not in ("user", "assistant"):
                continue
            msg = ev.get("message") or {}
            # Занято — сумма по последнему `assistant`: свежий ввод, записанный кэш,
            # прочитанный кэш и ответ. Суммировать по всей сессии нельзя, контекст не
            # растёт линейно — после `/compact` он падает.
            if role == "assistant" and (u := msg.get("usage")):
                used = sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"))
                model = msg.get("model") or model
            content = msg.get("content")

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
                    step = {"role": "tool", "icon": render.ICONS.get(name, "🔧"),
                            "name": name, "text": render.clip(arg, 200)}
                    # Полный текст — только когда строку реально обрезало: у Read и Edit
                    # аргумент это путь, и раскрывать там нечего. Переводы строк тут
                    # живые, в отличие от `clip`: команда с heredoc читается столбиком.
                    full = render.strip_ansi(str(arg)).strip()
                    if len(full) > 200:
                        step["full"] = full[:FULL_ARG]
                    out.append(step)
            if len(out) >= CHUNK:
                break
    return start, out, _ctx(used, model)


def _ctx(used: int | None, model: str | None) -> dict | None:
    """Занятость контекста в токенах, либо None, если считать было не по чему.

    Размер окна в транскрипт не пишется: его отдаёт `result` в конце прогона, откуда
    `runner` кладёт его в `store` по имени модели. Пока модель ни разу не отвечала в
    этом контейнере, берём 200k и помечаем оценкой.
    """
    if not used:
        return None
    window = store.get(f"ctxwin:{model}")
    return {"used": used, "window": int(window) if window else DEFAULT_WINDOW,
            "guess": not window}


def ctx_of(path: Path) -> dict | None:
    """Занятость контекста готовой сессии — то же, что `items` считает попутно.

    Своим проходом, а не `items(path, 0)[2]`: тот останавливается на `CHUNK` элементов
    и у длинной сессии посчитал бы контекст по её началу. Нужен последний `assistant`:
    контекст не растёт линейно, после `/compact` он падает.

    Дешёвый отсев по подстроке — как в `sessions.title`: json.loads на каждой строке
    транскрипта дороже самого чтения. Файл бывает на десятки мегабайт, поэтому
    вызывающий обязан звать это из потока, а не с event loop.
    """
    used = model = None
    with path.open("rb") as f:
        for raw in f:
            if b'"usage"' not in raw:
                continue
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if ev.get("type") != "assistant":
                continue
            msg = ev.get("message") or {}
            if u := msg.get("usage"):
                used = sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"))
                model = msg.get("model") or model
    return _ctx(used, model)


def _short(n: int) -> str:
    """Токены человеческим числом: 950, 12.3k, 1.4M."""
    for div, suffix in ((1_000_000, "M"), (1_000, "k")):
        if n >= div:
            return f"{n / div:.1f}{suffix}"
    return str(n)


def _secs(sec: float) -> str:
    s = int(sec)
    if s < 60:
        return f"{s}с"
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}ч {s % 3600 // 60}м"


def _stat_line(ev: dict, model: str | None) -> str:
    """Итог прогона одной строкой: модель, время, цена, токены.

    Всё это приезжает в `result` и до сих пор выбрасывалось — панель после ответа просто
    гасила таймер, и сколько он стоил, было видно только в Telegram. Ввод считаем со
    свежим и кэшированным вместе: платится и то и другое, а раздельно это четыре числа
    в строке, которую читают на бегу.

    Пустые поля пропускаем: у местных команд и у оборванного прогона цены нет, и `$0.000`
    сказал бы неправду.
    """
    u = ev.get("usage") or {}
    bits = [model] if model else []
    if ms := ev.get("duration_ms"):
        bits.append(_secs(ms / 1000))
    if cost := ev.get("total_cost_usd"):
        bits.append(f"${cost:.3f}")
    if tin := sum(int(u.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")):
        bits.append(f"↓{_short(tin)}")
    if tout := int(u.get("output_tokens") or 0):
        bits.append(f"↑{_short(tout)}")
    return " · ".join(bits)


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


# Скиллы для подсказки по `/`. Каталог тот же, что читает claude: бот и прогон живут в
# одном контейнере, поэтому список в браузере — это ровно то, что сработает в сессии.
# Плагины (`/root/.claude/plugins`) сюда не входят: у них своё устройство, а каталог
# скиллов — один файл на скилл.
SKILLS = Path("/root/.claude/skills")
# Имя в подсказку берём только такое, каким его можно набрать после слеша.
SKILL_NAME_RE = re.compile(r"[\w-]{1,64}\Z")
FRONT_RE = re.compile(r"^(name|description):\s*(.+)$", re.M)
# Встроенные команды claude: в каталоге скиллов их нет, а в панели они нужны наравне.
# Список руками, потому что наружу claude свой набор не отдаёт (`/help` в headless как
# раз и отвечает «isn't available»). Каждая строка проверена прогоном `claude -p`
# на 2.1.269: то, что открывает панель TUI (`/permissions`, `/hooks`, `/resume`,
# `/export`, `/ide`, `/vim`, `/statusline`, `/sandbox`, `/add-dir`, `/bashes`,
# `/memory`, `/status`, `/release-notes`, `/privacy-settings`, `/theme`), в headless
# отвечает отказом и в меню не попало.
#
# Чего тут нет сознательно: `/login`, `/logout`, `/upgrade`, `/terminal-setup`,
# `/install-github-app`, `/migrate-installer`, `/bug`. Они меняют общее состояние —
# OAuth-ключ один на все сессии контейнера, и разлогин из панели убил бы и бота.
BUILTIN = [
    {"name": "clear", "desc": "начать разговор с чистого листа, панель перейдёт в новую сессию"},
    {"name": "compact", "desc": "сжать контекст, сохранив суть разговора"},
    {"name": "context", "desc": "на что потрачен контекст этой сессии"},
    {"name": "cost", "desc": "расход подписки: сессия, неделя, когда сброс"},
    {"name": "usage", "desc": "то же, что /cost"},
    {"name": "model", "desc": "показать модель сессии; сменить нельзя — её задаёт панель"},
    {"name": "output-style", "desc": "стиль ответа: текущий и список доступных"},
    {"name": "config", "desc": "настройки claude: показать и поменять"},
    {"name": "mcp", "desc": "состояние MCP-серверов"},
    {"name": "doctor", "desc": "проверка установки claude"},
    {"name": "init", "desc": "собрать CLAUDE.md по текущему проекту"},
    {"name": "review", "desc": "ревью изменений в рабочем дереве"},
    {"name": "security-review", "desc": "ревью изменений на дыры, нужен git-репозиторий"},
]


def skills(project: str = "") -> list[dict]:
    """Команды для автодополнения: скиллы общие и проектные, затем встроенные.

    Скиллы наверху, потому что их тринадцать встроенных не должны отжимать вниз: в
    панель ходят за своим `/refine` и `/end`, а `/doctor` набирают раз в полгода.

    Проектные (`<project>/.claude/skills`) идут вторыми и перекрывают общие по имени —
    так же, как их разрешает сам claude. Без них панель localhome не знала бы про
    `/bw` и `/sync-repo`, а вызываются они именно там.

    Каталог без `SKILL.md` пропускается молча: в `skills/` попадают и черновики.
    """
    out: dict[str, dict] = {}
    for root in [SKILLS] + ([Path(project) / ".claude" / "skills"] if project else []):
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
            meta = {k: v.strip() for k, v in FRONT_RE.findall(head)}
            name = meta.get("name", "")
            # Имя из frontmatter бывает и с пробелами, и вовсе отсутствует, а набирают
            # скилл каталогом — на нём и стоим, когда frontmatter не годится.
            if not SKILL_NAME_RE.match(name):
                name = path.parent.name
            out[name] = {"name": name, "desc": meta.get("description", "")[:160]}
    return sorted(out.values(), key=lambda s: s["name"]) + BUILTIN


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

# Что показывает дерево файлов помимо проектов. По умолчанию — конфиг claude, источник
# скиллов и состояние бота: правят и смотрят их чаще всего, а лежат они вне /projects.
# Все три пути есть у любого инстанса, поэтому дефолт, а не строка в каждом .env.
# Список через запятую и из окружения, как PROJECTS_DIR и WEB_PEERS: инстанс с иным
# набором монтирований переопределяет его у себя. Кнопка «добавить корень» из панели
# означала бы «добавить /», после чего список корней теряет смысл.
FILE_ROOTS = [Path(x.strip()) for x in
              os.environ.get("FILE_ROOTS", "/root/.claude,/opt/skills,/data").split(",") if x.strip()]
# Потолок файла для редактора. Больше в textarea всё равно не поправить, а транскрипт
# сессии на 18 МБ утащил бы вкладку в своп. Имя файла и размер показываем и сверх него.
MAX_EDIT = 1 << 20


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


def roots() -> list[Path]:
    """Корни дерева файлов: проекты плюс FILE_ROOTS. Несуществующие пропускаем —
    инстансы монтируют разное, и лишний путь в .env не должен оставлять панель без
    списка."""
    return [*sessions.projects(), *(r for r in FILE_ROOTS if r.is_dir())]


def _inside(raw: str) -> Path:
    """Путь из браузера, обязанный лежать в одном из корней.

    Сверяем после `resolve()`, а не до: и `..`, и симлинк иначе уводят наружу. Именно
    симлинками собран `/root/.claude/skills` — каждый скилл ведёт в `/opt/skills/*`.
    Поэтому `/opt/skills` и стоит корнем по умолчанию: без него скилл видно в дереве,
    но не открыть, а список исключений пришлось бы вести руками.

    Отдельно от `_project`: тот решает, где запускается claude, и `/root/.claude`
    рабочим каталогом промпта быть не должен.
    """
    path = Path(raw or "").resolve()
    tops = [r.resolve() for r in roots()]
    if any(path == top or top in path.parents for top in tops):
        return path
    raise web.HTTPBadRequest(text="путь вне корней")


def _entries(path: Path) -> list[dict]:
    """Содержимое каталога: сначала каталоги, дальше по имени без учёта регистра.

    `stat` под try — битый симлинк в дереве обычное дело (скилл, чей источник отмонтировали),
    и ронять из-за него весь листинг незачем.
    """
    out = []
    for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        try:
            size = item.stat().st_size
        except OSError:
            size = 0
        out.append({"name": item.name, "path": str(item), "dir": item.is_dir(), "size": size})
    return out


def _version(st) -> str:
    """Метка версии файла для защиты от затирания. Строкой, а не числом: `st_mtime_ns`
    это 1.8e18, а JSON-число в браузере теряет точность после 9e15 — сравнение на
    сервере разъехалось бы на каждом сохранении."""
    return f"{st.st_mtime_ns}-{st.st_size}"


def _answered_locally(ev: dict) -> bool:
    """Ответил ли claude сам, не обращаясь к модели.

    Признак — нулевые цена и токены в `result`: местная команда до API не доходит.
    Проверено на `/cost` и `/model`, оба отдают текст при `total_cost_usd: 0` и
    `output_tokens: 0`. Настоящий прогон обоих нулей одновременно дать не может.

    Нужно, чтобы не дублировать обычный ответ: у него тот же `result`, но его панель
    уже вытянула из транскрипта.
    """
    return not ev.get("total_cost_usd") and not (ev.get("usage") or {}).get("output_tokens")


async def _drive(scope: str, prompt: str, project: str, session_id: str | None,
                 got: asyncio.Future, model: str | None = None, adopt: bool = False) -> None:
    """Довести запуск до конца, ничего не рендеря: вывод claude сам пишет в транскрипт,
    а панель его тейлит. Наружу отдаём только первый session_id — панели нужно знать,
    какой файл читать, особенно когда сессия новая и id придумал claude.

    `adopt` ставит промпт, который ждал очереди без сессии, в сессию предыдущего
    прогона. Без него два промпта подряд в пустую панель открыли бы две разные сессии
    вместо продолжения разговора.
    """
    sid = session_id
    err = ""
    _errors.pop(scope, None)  # новый запуск — прошлая ошибка больше не про него
    _local.pop(scope, None)
    _stats.pop(scope, None)
    seen_model = None
    try:
        async with runner.slot(scope):
            if adopt:
                sid = session_id = runner.last_session(scope) or session_id
            # Модель панели, а иначе глобальная из бота: две панели на разных моделях —
            # ровно то, ради чего делалась параллельность.
            async for ev in runner.run(prompt, project, session_id,
                                       model or store.get("model"), scope=scope):
                if not got.done() and (sid := ev.get("session_id") or sid):
                    got.set_result(sid)
                # Внятная причина приходит в `result`, а не в стоп-коде: лимит подписки,
                # отказ модели, недоступный проект — всё это claude пишет в stdout и
                # выходит с rc=1 при пустом stderr. Поэтому текст result важнее кода.
                # Модель берём из ответа, а не из того, что просили: панель с пустым
                # выбором едет на общей модели бота, а сессия могла быть заведена на другой.
                if ev.get("type") == "assistant":
                    seen_model = (ev.get("message") or {}).get("model") or seen_model
                if ev.get("type") == "result":
                    if ev.get("is_error"):
                        err = (ev.get("result") or "").strip()[:2000]
                    elif _answered_locally(ev) and (said := (ev.get("result") or "").strip()):
                        _local[scope] = said
                    elif line := _stat_line(ev, seen_model):
                        _stats[scope] = {"at": time.time(), "text": line}
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
    except runner.Dropped:
        log.info("промпт отброшен из очереди: scope=%s", scope)
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
        # no-store: страница целиком лежит в образе, и после раскатки вкладка обязана
        # взять новую. Валидаторов у ответа нет, поэтому без этого заголовка браузер
        # вправе отдать свою копию, и человек сидит на прошлой версии панели.
        return web.Response(text=PAGE, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def api_peers(_: web.Request) -> web.Response:
        return web.json_response(peers())

    async def api_models(_: web.Request) -> web.Response:
        return web.json_response(await runner.models())

    async def api_projects(_: web.Request) -> web.Response:
        return web.json_response(
            [{"name": p.name, "path": str(p)} for p in sessions.projects()]
        )

    async def api_skills(req: web.Request) -> web.Response:
        # Проект проверяем тем же `_project`: путь уходит в glob, и чужие каталоги тут
        # читать нечего. Панель со снесённым проектом получит 400 и останется без
        # подсказки — предлагать ей скиллы всё равно некуда.
        project = req.query.get("project") or ""
        return web.json_response(skills(_project(project) if project else ""))

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

    async def api_stream(req: web.Request) -> web.StreamResponse:
        """Хвост транскрипта, пока панель открыта: сервер сам говорит о новых строках.

        Оффсет едет в `id:` каждого кадра. Браузер при обрыве переподключается сам и
        возвращает его в `Last-Event-ID` — поэтому переподключение продолжает с места,
        хотя адрес потока остался прежним и `from` в нём давно устарел.

        Отсутствие файла — нормальное состояние, а не ошибка: id новой сессии известен
        раньше, чем claude успевает создать транскрипт. Поток просто ждёт.
        """
        path = transcript(req.query.get("project", ""), req.query.get("id", ""))
        off = _int(req.headers.get("Last-Event-ID") or req.query.get("from"))
        res = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-store",
            # Traefik ответ не собирает, но заголовок стоит копейку и страхует от прокси,
            # который решит иначе: тогда панель молчала бы до конца потока, то есть всегда.
            "X-Accel-Buffering": "no",
        })
        await res.prepare(req)
        idle = 0
        try:
            while True:
                found: list[dict] = []
                ctx = None
                reset = False
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None  # транскрипта ещё нет, claude его вот-вот создаст
                if size is not None:
                    # Файл короче нашего оффсета — его переписали или подменили. `seek`
                    # за его конец молчит вечно, и панель выглядит зависшей при живом
                    # потоке. Читаем сначала и просим панель очистить лог: показанное
                    # относится к прежнему содержимому файла и уже неверно.
                    if size < off:
                        off, reset = 0, True
                    # Диск в потоке: первый заход читает сессию целиком, а она бывает на
                    # десятки мегабайт — в общем event loop это заморозило бы все панели.
                    off, found, ctx = await asyncio.to_thread(items, path, off)
                if found or ctx:
                    body = json.dumps({"next": off, "items": found, "ctx": ctx,
                                       "reset": reset})
                    await res.write(f"id: {off}\ndata: {body}\n\n".encode())
                if found:
                    idle = 0
                    continue  # кусок мог упереться в CHUNK — дочитываем без паузы
                # Комментарий раз в ~20 секунд: молчащее соединение рвут и прокси, и
                # мобильная сеть, а браузер комментарий молча выбрасывает.
                if (idle := idle + 1) % PING_EVERY == 0:
                    await res.write(b": ping\n\n")
                await asyncio.sleep(TAIL_TICK)
        except ConnectionResetError:
            pass  # вкладку закрыли — обычный конец потока, а не сбой
        return res

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

    async def api_roots(_: web.Request) -> web.Response:
        return web.json_response([{"name": r.name, "path": str(r)} for r in roots()])

    async def api_files(req: web.Request) -> web.Response:
        """Листинг каталога. Скрытые файлы отдаём все — прячет их переключатель в
        панели. Чёрного списка имён тут нет сознательно: его пришлось бы вести руками,
        он молча прятал бы нужный файл, а закрывать им нечего — claude читает те же
        файлы сам, и в панель пускает allowlist."""
        path = _inside(req.query.get("path", ""))
        if not path.is_dir():
            raise web.HTTPBadRequest(text="не каталог")
        try:
            entries = await asyncio.to_thread(_entries, path)
        except OSError as err:
            raise web.HTTPBadRequest(text=f"не прочитать каталог: {err}") from err
        return web.json_response({"path": str(path), "entries": entries})

    async def api_file(req: web.Request) -> web.Response:
        """Содержимое файла для редактора.

        Отказ отдаётся полем `why`, а не кодом ошибки: панель показывает имя, размер и
        причину, а не пустое окно. Не-utf8 отклоняем до декодирования с `replace` —
        сохранение такого текста переписало бы файл испорченным.
        """
        path = _inside(req.query.get("path", ""))
        try:
            st = path.stat()
        except OSError as err:
            raise web.HTTPBadRequest(text=f"нет файла: {err}") from err
        if not path.is_file():
            raise web.HTTPBadRequest(text="не файл")
        head = {"path": str(path), "size": st.st_size, "version": _version(st)}
        if st.st_size > MAX_EDIT:
            return web.json_response({**head, "why": "больше 1 МБ"})
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as err:
            raise web.HTTPBadRequest(text=f"не прочитать: {err}") from err
        try:
            text = data.decode()
        except UnicodeDecodeError:
            return web.json_response({**head, "why": "не текст в utf-8"})
        if b"\x00" in data:
            return web.json_response({**head, "why": "двоичный файл"})
        return web.json_response({**head, "text": text})

    async def api_save(req: web.Request) -> web.Response:
        """Запись поверх существующего файла.

        Версия из чтения возвращается назад и сверяется: claude правит те же файлы, и
        без этой сверки правка человека молча затирала бы его правку. Расхождение —
        409, панель предлагает перечитать.

        Создания, удаления и переименования тут нет: это умеет claude в соседней
        панели, а редактору хватает существующего файла. Открытие идёт по тому же
        inode, поэтому владелец и права остаются чужими — новых root-файлов в проекте
        не появляется.
        """
        data = await req.json()
        path = _inside(data.get("path") or "")
        text = data.get("text")
        if not isinstance(text, str):
            raise web.HTTPBadRequest(text="нужен text")
        if not path.is_file():
            raise web.HTTPBadRequest(text="нет такого файла")
        if _version(path.stat()) != data.get("version"):
            raise web.HTTPConflict(text="файл изменился на диске")
        try:
            await asyncio.to_thread(path.write_text, text, encoding="utf-8")
        except OSError as err:
            # Сюда попадает и `:ro`-монтирование: `/root/.claude/CLAUDE.md` в песочнице
            # примонтирован только на чтение, и текст системы об этом честнее нашего.
            raise web.HTTPBadRequest(text=f"не записать: {err}") from err
        return web.json_response({"version": _version(path.stat())})

    async def api_status(_: web.Request) -> web.Response:
        """Живые запуски и упавшие прогоны. Запуски берутся из тех же `runner._runs`,
        что у Telegram, и несут id сессии — по нему панель узнаёт свой сеанс, даже если
        его гоняют из топика под другим скоупом.

        Общая модель — оттуда же, откуда её берёт `_drive` при пустом выборе в панели.
        Без неё в селекте стояло безымянное «модель», и что именно поедет в claude,
        из панели было не видно.

        Лимиты подписки едут тем же ответом, а не своим роутом: он уже опрашивается
        раз в три секунды, а `runner.limits` держит свой минутный кеш — выходит один
        запрос в API на минуту на все открытые вкладки.
        """
        return web.json_response({"runs": runner.active(), "errors": _errors,
                                  "local": _local, "stats": _stats,
                                  "queued": runner.waiting(),
                                  "limits": await runner.limits(),
                                  "model": await runner.resolve_model(
                                      store.get("model") or runner.default_model())})

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
        queued = runner.ahead(scope)

        got: asyncio.Future = asyncio.get_running_loop().create_future()
        # Задача живёт дольше запроса: ответ панели — только session_id, а прогон
        # продолжается в фоне и виден ей через транскрипт.
        task = asyncio.create_task(_drive(scope, prompt, project, session_id, got, model,
                                          adopt=bool(queued) and session_id is None))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        if queued:
            # Ждать нечего: прогон начнётся после предыдущего, а id новой сессии панель
            # подберёт из /api/status по своему скоупу — в том числе после F5.
            return web.json_response({"queued": queued, "session": session_id})
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
        stopped, dropped = await runner.cancel(f"web:{pane}")
        return web.json_response({"stopped": stopped, "dropped": dropped})

    # client_max_size — предел на тело запроса. По умолчанию у aiohttp мегабайт, и
    # загрузка файла падала бы с 413 раньше нашего кода.
    app = web.Application(client_max_size=MAX_UPLOAD)
    app.add_routes([
        web.get("/", index),
        web.get("/api/peers", api_peers),
        web.get("/api/models", api_models),
        web.get("/api/projects", api_projects),
        web.get("/api/skills", api_skills),
        web.get("/api/sessions", api_sessions),
        web.get("/api/search", api_search),
        web.get("/api/roots", api_roots),
        web.get("/api/files", api_files),
        web.get("/api/file", api_file),
        web.post("/api/file", api_save),
        web.get("/api/purge", api_purge),
        web.post("/api/purge", api_purge),
        web.get("/api/stream", api_stream),
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
