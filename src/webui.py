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
  section.rolled.zoomed { position:static }
  /* Пока окно развёрнуто, соседей не просто не видно — их нет в отрисовке. Safari на iOS
     уводит `position:fixed` внутри прокручиваемого #panes в отдельный слой, и соседнее
     окно всплывает поверх, хотя z-index у него меньше (первым — заголовок свёрнутого,
     единственное, что от него осталось). Спорить со слоями бесполезно, а скрытое не
     всплывает.
     Прячем всех, кроме самого развёрнутого, а не только `:not(.zoomed)`: сворачивание
     не сбрасывает zoom (`fold.onclick` трогает только `p.roll`), поэтому у свёрнутого
     соседа класс `zoomed` обычно остаётся — на нём первая версия правила и споткнулась. */
  #panes:has(> section.zoomed:not(.rolled)) > section { display:none }
  #panes:has(> section.zoomed:not(.rolled)) > section.zoomed:not(.rolled) { display:flex }
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
  <button class=new id=dots title="показывать файлы с точкой в начале">скрытые: вкл</button>
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
  const max = el.querySelector('header .max');
  max.onclick = () => { zoom(p); save(); raise(el); };
  const fold = el.querySelector('header .foldbar');
  const drawFold = () => {
    el.classList.toggle('rolled', !!p.roll);
    fold.textContent = p.roll ? '▾' : '▴';
    fold.title = p.roll ? 'развернуть окно' : 'свернуть окно в заголовок';
  };
  // Флаг лежит в самой панели, а она целиком уходит в localStorage — свёрнутая
  // остаётся свёрнутой и после F5, как остаётся её место в сетке.
  fold.onclick = () => { p.roll = !p.roll; save(); drawFold(); };
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

async function openDir(path) {
  const data = await get('api/files?path=' + encodeURIComponent(path));
  localStorage.setItem('dir', data.path);
  drawCrumb(data.path);
  drawTree(data.entries);
}

const treeFail = (e) => { $('tree').innerHTML = '<div class=none>не открыть (' + esc(e) + ')</div>'; };

// Путь от корня кнопками. Выше корня подниматься нечем и не нужно: сервер такой путь
// всё равно отклонит, а в дереве видно только примонтированное.
function drawCrumb(path) {
  const root = $('root').value;
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
