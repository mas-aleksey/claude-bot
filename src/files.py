"""Дерево файлов и редактор: своя группа роутов панели.

Отдельно от `webui`, потому что с прогонами claude у неё общего только адрес: свои
корни, своя проверка пути и своя защита от затирания. В `webui` это была треть файла,
не связанная ни с одной его строкой.

Корни — проекты плюс `FILE_ROOTS`. Всё, что приходит от браузера, проходит через
`inside`: путь сверяется с корнями после `resolve()`.
"""

import asyncio
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

from aiohttp import web

import sessions

log = logging.getLogger("claude_bot.files")

# Каталог для файлов из браузера — тот же, что у файлов из Telegram: бот кладёт их
# сюда же и подставляет путь в промпт. Значение читают и app.py, и этот модуль, поэтому
# живёт в одном месте.
INBOX = Path(os.environ.get("INBOX_DIR", "/data/inbox"))
# Предел на запрос. Больше двадцати пяти мегабайт в промпт всё равно не имеет смысла:
# claude читает файл сам, а место в песочнице не бесконечное.
MAX_UPLOAD = 25 << 20

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


def roots() -> list[Path]:
    """Корни дерева файлов: проекты плюс FILE_ROOTS. Несуществующие пропускаем —
    инстансы монтируют разное, и лишний путь в .env не должен оставлять панель без
    списка."""
    return [*sessions.projects(), *(r for r in FILE_ROOTS if r.is_dir())]


def inside(raw: str) -> Path:
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



async def api_roots(_: web.Request) -> web.Response:
    return web.json_response([{"name": r.name, "path": str(r)} for r in roots()])


async def api_files(req: web.Request) -> web.Response:
    """Листинг каталога. Скрытые файлы отдаём все — прячет их переключатель в
    панели. Чёрного списка имён тут нет сознательно: его пришлось бы вести руками,
    он молча прятал бы нужный файл, а закрывать им нечего — claude читает те же
    файлы сам, и в панель пускает allowlist."""
    path = inside(req.query.get("path", ""))
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
    path = inside(req.query.get("path", ""))
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

    Удаления и переименования тут нет: это умеет claude в соседней панели. Запись
    идёт по тому же inode, поэтому владелец и права остаются чужими — существующий
    файл не становится root-овым от правки из браузера.
    """
    data = await req.json()
    path = inside(data.get("path") or "")
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


async def api_new(req: web.Request) -> web.Response:
    """Создать пустой файл и отдать его панели — дальше он открывается редактором.

    Только файл и только в существующем каталоге: `mkdir` и удаление остаются за
    claude в соседней панели, там об этом можно сказать словами.

    Владельца берём у каталога. Бот работает root-ом, и без этого новый файл в
    `/projects` человек на хосте не смог бы поправить — та же грабля, из-за которой
    правка существующего файла идёт по его inode.
    """
    data = await req.json()
    where = inside(data.get("dir") or "")
    name = _filename(data.get("name"))
    if not where.is_dir():
        raise web.HTTPBadRequest(text="нет такого каталога")
    # Ещё раз через `inside`: имя очищено, но каталог мог оказаться симлинком наружу.
    path = inside(str(where / name))
    if path.exists():
        raise web.HTTPBadRequest(text="такой файл уже есть")
    try:
        await asyncio.to_thread(path.touch)
        st = where.stat()
        os.chown(path, st.st_uid, st.st_gid)
    except OSError as err:
        raise web.HTTPBadRequest(text=f"не создать: {err}") from err
    log.info("new file: %s", path)
    return web.json_response({"path": str(path), "version": _version(path.stat())})


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
