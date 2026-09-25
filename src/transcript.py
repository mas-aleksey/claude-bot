"""Транскрипт сессии claude: путь к файлу и разбор его событий.

Читают отсюда оба интерфейса — и панель в браузере, и `/status` в Telegram, — поэтому
модуль свой, а не внутренность `webui`. Раньше он ею и был: Telegram ходил в HTTP-модуль
за разбором файла, а переименование внутри `webui` тихо сломало `/status`.

Про HTTP тут ничего не знают. Негодный id сессии — `ValueError`, а в 400 его
превращает вызывающий, если он веб.
"""

import json
import re
import time
from pathlib import Path

import render
import sessions
import store

# id сессии приходит от клиента и подставляется в имя файла. Пропускаем только то,
# чем claude их и называет — uuid: ни слешей, ни точек, ни `..`.
SESSION_RE = re.compile(r"[0-9a-fA-F-]{8,64}\Z")

# Элементов в одном кадре. Транскрипт бывает на десятки тысяч строк, а страница должна
# отрисоваться сразу — остальное доедет следующими кадрами с того же оффсета.
CHUNK = 3000
# Потолок раскрытого аргумента шага. Медиана шага в песочнице — 244 символа, p90 —
# около 1700, но встречаются и тридцатитысячные: такой развернули бы лог на весь экран,
# а прочесть его всё равно негде. Обрезанную строку показываем без раскрытия.
FULL_ARG = 2000
# Окно контекста, пока claude не назвал своё: столько у haiku и sonnet, у opus больше.
# Значение временное — после первого же прогона модели в `store` ложится настоящее.
DEFAULT_WINDOW = 200_000


def path_of(project: str, session_id: str) -> Path:
    """Путь к транскрипту по проекту и id, существование не проверяется.

    Оба параметра клиентские. `project` безопасен по построению: `sessions.slug` заменяет
    каждый не-алфанумерик на `-`, так что каталог из него не выйдет. `id` держит
    регулярка — она тут и есть защита, а не наличие файла.

    Отсутствие файла — нормальное состояние, а не ошибка: у новой сессии id уже
    известен из первого события, а транскрипт claude создаёт не мгновенно. Панель в
    этот момент уже опрашивает, и 404 в ответ был бы ложной тревогой.
    """
    if not SESSION_RE.match(session_id):
        raise ValueError("плохой id сессии")
    return sessions.TRANSCRIPTS / sessions.slug(project) / f"{session_id}.jsonl"


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
                used = render.tokens_total(u)
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
                    arg = render.first_arg(name, block.get("input") or {})
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
                used = render.tokens_total(u)
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


def stat_line(ev: dict, model: str | None) -> str:
    """Итог прогона одной строкой: модель, время работы, токены и час окончания.

    Цена отсюда снята 2026-09-25: подписка не считает по запросам, и `$0.084` рядом с
    ответом означал не расход, а оценку по прайсу API. Ввод считаем со свежим и
    кэшированным вместе: платится и то и другое, а раздельно это четыре числа в строке,
    которую читают на бегу.

    Час окончания берём по часам сервера: строка собирается ровно в момент, когда пришёл
    `result`, и хранится вместе с меткой времени. Он нужен, чтобы вернувшись к панели
    через час, понимать, когда ответ был готов, — таймер к тому моменту уже погашен.

    Пустые поля пропускаем: у местных команд нет ни токенов, ни длительности, и строки
    не появляется вовсе.
    """
    u = ev.get("usage") or {}
    bits = [model] if model else []
    if ms := ev.get("duration_ms"):
        bits.append(_secs(ms / 1000))
    if tin := render.tokens_in(u):
        bits.append(f"↓{_short(tin)}")
    if tout := int(u.get("output_tokens") or 0):
        bits.append(f"↑{_short(tout)}")
    # Час дописываем только к непустой строке: у местной команды строки нет, и одинокое
    # «14:38» под ответом читалось бы как время, потраченное на него.
    if bits:
        bits.append(time.strftime("%H:%M"))
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
