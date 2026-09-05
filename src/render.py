"""Поток событий stream-json → текст одного сообщения в чате.

Формат событий проверен не на всех типах (шаг 2 плана прогнан не полностью),
поэтому парсер терпим: незнакомое событие игнорируется, а не роняет рендер.
"""

import html
import re
import textwrap
import time

LIMIT = 4096
TAIL = 15  # сколько последних прогресс-строк показываем
# Ширина, после которой <pre> на телефоне даёт горизонтальный скролл (он не переносит,
# а именно скроллит). Не влезли — сначала жмём широкие колонки с переносом внутри
# ячейки, и только если не помогло (колонок больше, чем места) — разворачиваем в список.
TABLE_WIDTH = 45
MIN_COL = 8  # уже — перенос рвёт слова в лапшу, лучше список

ICONS = {
    "Read": "📖",
    "Write": "✏️",
    "Edit": "✏️",
    "Bash": "🖥",
    "Glob": "🔍",
    "Grep": "🔍",
    "WebFetch": "🌐",
    "WebSearch": "🌐",
    "Task": "🤖",
    "TodoWrite": "📝",
}

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")

# Теги, которые мы сами и ставим (app.py: <b>, <code>, <pre>) — только их и снимаем
# в plain(): всё остальное в тексте пришло от claude и тегом не является.
OPEN = re.compile(r"</?(?:b|i|u|s|code|pre)>|<a href=\"[^\"]*\">|</a>")


def strip_ansi(s: str) -> str:
    return ANSI.sub("", s)


def esc(s: str) -> str:
    """Экранировать текст, который кладём внутрь наших HTML-тегов.

    Вывод claude — не разметка: голый ``<`` из диффа или ``&`` из url иначе
    ломает parse_mode=HTML и Telegram отвергает всё сообщение."""
    return html.escape(str(s), quote=False)


# Markdown из вывода claude → HTML-теги Telegram. Порядок важен: сначала esc()
# всего текста, потом восстановление разметки — иначе `<` из диффа ломает parse_mode.
_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.S)
_INLINE = [
    (re.compile(r"(?<!`)`([^`\n]+)`(?!`)"), r"<code>\1</code>"),
    (re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*"), r"<b>\1</b>"),
    (re.compile(r"(?<![*\w])\*(\S(?:[^*\n]*\S)?)\*(?!\*)"), r"<i>\1</i>"),
    (re.compile(r"(?<![~\w])~~(\S(?:[^~]*\S)?)~~"), r"<s>\1</s>"),
    (re.compile(r"(?m)^(#{1,6})\s+(.+)$"), r"<b>\2</b>"),
    (re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
]


# Таблицы Telegram не умеет вовсе: колонки сводит только моноширинный <pre>,
# а он не рендерит вложенные <b>/<code> — выделения в ячейках неизбежно теряются.
# Узкую таблицу отдаём колонками, широкую разворачиваем в список: скролл в <pre>
# читать хуже, чем длинный список.
_TABLE = re.compile(r"(?m)^[ \t]*\|.+\|[ \t]*\n[ \t]*\|[ \t:|-]+\|[ \t]*\n(?:[ \t]*\|.*\|[ \t]*(?:\n|\Z))+")
_MARKERS = re.compile(r"\*\*(.+?)\*\*|`(.+?)`|\*(.+?)\*")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _unmark(s: str) -> str:
    """Снять markdown-маркеры: внутри <pre> они всё равно не разметка, только шум."""
    return _MARKERS.sub(lambda m: m.group(1) or m.group(2) or m.group(3), s)


def table(block: str) -> str:
    """Markdown-таблица → <pre> с колонками или список — по ширине."""
    lines = [ln for ln in block.strip().split("\n") if ln.strip()]
    head, rows = _cells(lines[0]), [_cells(ln) for ln in lines[2:]]
    n = len(head)
    rows = [r[:n] + [""] * (n - len(r)) for r in rows]  # рваные строки не должны ронять рендер

    grid = [[_unmark(c) for c in r] for r in (head, *rows)]
    width = [max(len(r[i]) for r in grid) for i in range(n)]

    # Жмём самую широкую колонку, пока таблица не влезет: длинная ячейка переносится
    # внутри своей колонки, а не гонит всю таблицу в скролл.
    gaps = 2 * (n - 1)
    while sum(width) + gaps > TABLE_WIDTH and max(width) > MIN_COL:
        width[width.index(max(width))] -= 1

    if sum(width) + gaps <= TABLE_WIDTH:
        out = [grid[0], ["─" * w for w in width], *grid[1:]]
        body = "\n".join(_row(r, width) for r in out)
        return f"<pre>{esc(body)}</pre>"

    # Колонок больше, чем места: переносом уже не спасти — разворачиваем в список.
    # Тут разметка в ячейках живая, поэтому маркеры не срезаем.
    out = []
    for r in rows:
        first, *rest = (f"{head[i]}: {c}" for i, c in enumerate(r) if c)
        out.append("▪ " + _inline(esc(first)))
        out += ["  " + _inline(esc(x)) for x in rest]
        out.append("")
    return "\n".join(out).rstrip()


def _row(cells: list[str], width: list[int]) -> str:
    """Строка таблицы; ячейка шире своей колонки переносится, съезжая на строку вниз."""
    wrapped = [textwrap.wrap(c, width[i]) or [""] for i, c in enumerate(cells)]
    return "\n".join(
        "  ".join((w[ln] if ln < len(w) else "").ljust(width[i]) for i, w in enumerate(wrapped)).rstrip()
        for ln in range(max(len(w) for w in wrapped))
    )


def md(s: str) -> str:
    """Markdown claude → HTML Telegram. Всё неизвестное остаётся текстом."""
    out: list[str] = []
    pos = 0
    for m in _FENCE.finditer(s):
        out.append(_text(s[pos : m.start()]))
        out.append(f"<pre>{esc(m.group(1))}</pre>")
        pos = m.end()
    out.append(_text(s[pos:]))
    return "".join(out)


def _text(s: str) -> str:
    """Кусок вне код-блоков: таблицы — блоками, остальное — inline-разметкой."""
    out: list[str] = []
    pos = 0
    for m in _TABLE.finditer(s):
        out.append(_inline(esc(s[pos : m.start()])))
        out.append(table(m.group(0)))
        pos = m.end()
    out.append(_inline(esc(s[pos:])))
    return "".join(out)


def _inline(s: str) -> str:
    # code первым: разметку внутри `` `**x**` `` трогать нельзя, поэтому куски
    # <code> вырезаем и возвращаем на место после остальных замен.
    holes: list[str] = []

    def stash(m: re.Match) -> str:
        holes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(holes) - 1}\x00"

    s = _INLINE[0][0].sub(stash, s)
    for pat, repl in _INLINE[1:]:
        s = pat.sub(repl, s)
    return re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], s)


def _first_arg(name: str, args: dict) -> str:
    for key in ("file_path", "command", "pattern", "path", "url", "query", "prompt", "description"):
        if val := args.get(key):
            return str(val)
    return name


class Run:
    """Накопитель состояния одного запуска. `text()` вызывается на каждый апдейт."""

    def __init__(self, prompt: str, project: str) -> None:
        self.prompt = prompt
        self.project = project
        self.started = time.monotonic()
        self.steps: list[str] = []
        self.result = ""
        self.error = ""
        self.session_id: str | None = None
        self.usage = ""
        self.done = False

    def feed(self, ev: dict) -> None:
        if sid := ev.get("session_id"):
            self.session_id = sid

        kind = ev.get("type")

        if kind == "_bot":
            if ev.get("kind") == "error":
                self.error = f"rc={ev.get('rc')}\n{ev.get('text', '')}".strip()
            return

        if kind == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    arg = _first_arg(name, block.get("input") or {})
                    self.steps.append(f"{ICONS.get(name, '🔧')} {name}: {clip(arg, 60)}")
            return

        if kind == "result":
            self.done = True
            self.result = ev.get("result") or ""
            if ev.get("is_error"):
                self.error = self.result or "claude вернул ошибку"
                self.result = ""
            u = ev.get("usage") or {}
            bits = []
            if cost := ev.get("total_cost_usd"):
                bits.append(f"${cost:.3f}")
            if tin := u.get("input_tokens"):
                bits.append(f"↓{tin}")
            if tout := u.get("output_tokens"):
                bits.append(f"↑{tout}")
            self.usage = " ".join(bits)

    def parts(self) -> tuple[str, list[str]]:
        """Финал: (прогресс-сообщение, доп. сообщения с продолжением результата).
        Пока результат влезает в одно сообщение — extra пустой, поведение как было."""
        body = (self.error and f"❌ {self.error}") or self.result
        # Всё меряем в HTML-длине: в Telegram уходит md(out), а не сам out.
        # room — место, оставшееся под результат после обвязки (заголовок, шаги, footer).
        room = LIMIT - len(self.text(tail="")) - 1
        if fits(body, room):
            return self.text(), []
        # Если обвязка съела почти всё место — весь результат уходит отдельными.
        if room < 400:
            return self.text(tail="…"), [md(c) for c in split(body)]
        chunks = split(body, room)
        return self.text(tail=chunks[0]), [md(c) for c in chunks[1:]]

    def text(self, tail: str | None = None) -> str:
        """`tail` — чем закончить сообщение вместо результата (для прогресс-сообщения,
        когда длинный результат уходит отдельными сообщениями)."""
        head = f"{'✅' if self.done else '⏳'} {clip(self.prompt, 120)}"

        steps = self.steps[-TAIL:]
        if hidden := len(self.steps) - len(steps):
            steps = [f"… ещё {hidden} вызовов", *steps]
        body = "\n".join(steps)

        if tail is None:
            tail = (self.error and f"❌ {self.error}") or self.result

        footer = ""
        if self.done:
            parts = [f"{time.monotonic() - self.started:.0f}s", f"{len(self.steps)} вызовов"]
            if self.usage:
                parts.append(self.usage)
            footer = "— " + " · ".join(parts)

        # Приоритет при обрезке: финальный текст > прогресс > заголовок.
        blocks = [b for b in (head, body, tail, footer) if b]
        out = "\n\n".join(blocks)
        while not fits(out, LIMIT) and body:
            steps = steps[1:]
            body = "\n".join(steps) if steps else ""
            blocks = [b for b in (head, body, tail, footer) if b]
            out = "\n\n".join(blocks)
        while not fits(out, LIMIT):  # обвязка одна не влезла — режем её саму
            out = out[: len(out) * 2 // 3] + "…"
        return md(out)


def clip(s: str, n: int) -> str:
    s = strip_ansi(str(s)).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def fits(s: str, limit: int) -> bool:
    """Влезет ли plain-кусок в лимит после md(): теги и escape раздувают его втрое."""
    return len(md(s)) <= limit


def split(s: str, limit: int = LIMIT) -> list[str]:
    """Длинный текст → куски под лимит Telegram, по границам строк.
    Меряем md(кусок), а не сам кусок: режем plain (иначе рвём тег пополам),
    но в Telegram уходит HTML, который втрое длиннее.
    Строку, которая не влезает и в одиночку, режем пополам, пока не влезет.
    Таблицу режем по строкам с повтором шапки: без неё хвост — сырые пайпы."""
    out: list[str] = []
    for block in _blocks(s):
        for line in _fit(block, limit):
            if out and fits(out[-1] + "\n" + line, limit):
                out[-1] += "\n" + line
            else:
                out.append(line)
    return [c for c in out if c.strip()] or [s]


def _blocks(s: str) -> list[str]:
    """Текст → куски, где таблица идёт одним куском, остальное — построчно."""
    out: list[str] = []
    pos = 0
    for m in _TABLE.finditer(s):
        out += s[pos : m.start()].split("\n")
        out.append(m.group(0))
        pos = m.end()
    return out + s[pos:].split("\n")


def _fit(block: str, limit: int) -> list[str]:
    """Кусок → части, каждая из которых влезает в лимит."""
    if fits(block, limit):
        return [block]
    if _TABLE.fullmatch(block):
        head = block.split("\n")[:2]  # шапка + разделитель, повторяем в каждой части
        out, cur = [], list(head)
        for row in block.strip().split("\n")[2:]:
            if len(cur) > 2 and not fits("\n".join([*cur, row]), limit):
                out.append("\n".join(cur))
                cur = [*head, row]
            else:
                cur.append(row)
        return [*out, "\n".join(cur)]
    out = []
    while not fits(block, limit):
        cut = len(block) // 2
        while cut > 1 and not fits(block[:cut], limit):
            cut //= 2
        out.append(block[:cut])
        block = block[cut:]
    return [*out, block]


def plain(s: str) -> str:
    """HTML → читаемый текст. Фолбэк, когда Telegram отверг разметку."""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = OPEN.sub("", s)
    return html.unescape(s)
