"""Точка входа: aiogram long-polling, один разрешённый пользователь, один чат."""

import asyncio
import contextlib
import html
import logging
import os
import shlex
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

import render
import runner
import sessions
import store
import webui

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude_bot")

TOKEN = os.environ["TG_BOT_TOKEN"]
ALLOWED = {int(x) for x in os.environ.get("TG_ALLOWED_USER_ID", "").split(",") if x.strip()}
PROJECTS_DIR = sessions.PROJECTS_DIR
AUDIT = Path(os.environ.get("AUDIT_LOG", "/data/audit.log"))
INBOX = webui.INBOX  # один каталог для файлов из Telegram и из браузера
# Пусто — рабочее пространство в браузере не поднимается. Порт нужен не всем инстансам:
# ассистенту он ни к чему, песочнице — только если её пробросили через Traefik.
WEB_PORT = int(os.environ.get("WEB_PORT") or 0)


THROTTLE = 2.0   # секунд между editMessageText
LONG_RUN = 120   # после стольких секунд шлём отдельный пинг «готово»

dp = Dispatcher()


@dp.update.outer_middleware()
async def allowlist(handler, event: TelegramObject, data: dict):
    """Fail-closed: пустой ALLOWED — не отвечаем никому."""
    user = data.get("event_from_user")
    if user is None or user.id not in ALLOWED:
        log.warning("denied: %s", user and user.id)
        return None
    return await handler(event, data)


def scope(msg: Message) -> str:
    """Ключ состояния: топик форума = свой проект и своя сессия, поэтому в топиках
    можно работать параллельно. Личка и обычная группа — общий скоуп "0".

    Условие ровно `is_topic_message`: обычный реплай тоже приносит message_thread_id,
    и по нему состояние расползлось бы на случайные скоупы. Ответы в топик уходят сами —
    aiogram подставляет message_thread_id в msg.answer по тому же признаку.
    """
    return str(msg.message_thread_id) if msg.is_topic_message else "0"


def cwd(scope: str) -> str:
    """Текущий проект скоупа. Дефолт — первый в /projects; пусто — сам PROJECTS_DIR."""
    if saved := store.get(f"{scope}:cwd"):
        return saved
    found = sessions.projects()
    return str(found[0] if found else PROJECTS_DIR)


# Один источник правды: и для /help, и для меню команд Telegram (setMyCommands).
# Пары, а не dict, — порядок важен, группировка по смыслу.
HELP = [
    ("status", "текущий проект, модель, сессия, авторизация"),
    ("projects", "список проектов кнопками"),
    ("cd", "<name> — переключить проект"),
    ("clone", "<git-url> [name] — склонировать репозиторий в проекты"),
    ("sessions", "последние сессии проекта — переключиться"),
    ("new", "сбросить сессию текущего проекта"),
    ("purge", "[дней] — удалить старые сессии; без yes только предпросмотр"),
    ("cancel", "убить активный запуск или прервать логин"),
    ("model", "[алиас|полный id] — показать или сменить модель"),
    ("mcp", "MCP-серверы: list, add, get, rm — как в claude mcp"),
    ("plugin", "плагины: list, install, rm, enable, disable, marketplace"),
    ("login", "войти в Claude по подписке (код с телефона)"),
    ("logout", "выйти из Claude"),
    ("help", "это сообщение"),
]


# Ввод и примеры к пробросам. Полную справку по подкомандам не дублируем —
# она живёт в CLI и разъедется с ботом: `/mcp --help`, `/plugin --help`.
HELP_TAIL = """
<b>ввод</b>
текст — промпт в текущий проект
в группе с топиками каждый топик — своя сессия и свой проект, работают параллельно
<code>!refine текст</code> — слеш-команда Claude <code>/refine текст</code>

<b>mcp и plugin — аргументы уходят в CLI как есть</b>
<code>/mcp add sentry --transport http https://…</code>
<code>/mcp get sentry</code> · <code>/mcp rm sentry</code>
<code>/plugin marketplace add owner/repo</code>
<code>/plugin install name@market</code> · <code>/plugin rm name</code>
<code>/plugin enable name</code> · <code>/plugin disable name</code>
<code>/plugin details name</code> · <code>/plugin update name</code>
<code>/mcp --help</code> · <code>/plugin --help</code> — полный список подкоманд
"""


@dp.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    body = "\n".join(f"/{c} — {html.escape(d)}" for c, d in HELP)
    await msg.answer(f"<b>команды</b>\n{body}\n{HELP_TAIL}", parse_mode="HTML")


@dp.message(Command("start", "status"))
async def cmd_status(msg: Message) -> None:
    sc = scope(msg)
    here = cwd(sc)
    auth = await runner.auth_status()
    sid = store.session_of(sc, here)
    queued = runner.waiting().get(sc, 0)
    await msg.answer(
        f"проект: {Path(here).name} ({here})\n"
        f"модель: {store.get('model', 'default')}\n"
        f"сессия: {sid or 'новая'}\n"
        f"контекст: {_context_line(here, sid)}\n"
        f"claude: {'вошёл' if auth.get('loggedIn') else 'НЕ вошёл'}\n"
        f"занят: {runner.busy(sc)}{f', в очереди {queued}' if queued else ''}\n"
        f"\n/help — команды"
    )


def _context_line(project: str, session_id: str | None) -> str:
    """Занятость контекста строкой. Полоски, как в панели браузера, в Telegram нет —
    и числа тут смотрят реже, поэтому одна строка в /status, а не в каждом ответе."""
    if not session_id:
        return "—"
    path = webui.transcript(project, session_id)
    ctx = webui.ctx_of(path) if path.is_file() else None
    if not ctx:
        return "—"
    k = round(ctx["used"] / 1000)
    win = round(ctx["window"] / 1000)
    return f"{k}k/{win}k{'?' if ctx['guess'] else ''} ({round(100 * ctx['used'] / ctx['window'])}%)"


@dp.message(Command("projects"))
async def cmd_projects(msg: Message) -> None:
    here = cwd(scope(msg))
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅ ' if str(p) == here else ''}{p.name}", callback_data=f"cd:{p}"
        )]
        for p in sessions.projects()
    ]
    await msg.answer("проекты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("cd:"))
async def cb_cd(cb: CallbackQuery) -> None:
    path = cb.data.removeprefix("cd:")
    if Path(path) not in sessions.projects():
        await cb.answer("проекта больше нет", show_alert=True)
        return
    store.put(f"{scope(cb.message)}:cwd", path)
    await cb.answer(Path(path).name)
    await cb.message.edit_text(f"проект: {path}")


@dp.message(Command("sessions"))
async def cmd_sessions(msg: Message) -> None:
    sc = scope(msg)
    here = cwd(sc)
    current = store.session_of(sc, here)
    # Диск, а не asyncio: транскрипты бывают на десятки мегабайт. Держим в to_thread,
    # чтобы чтение не морозило long-polling.
    found = await asyncio.to_thread(sessions.recent, here)
    if not found:
        await msg.answer(f"нет сессий в {Path(here).name}")
        return
    # Лимит текста кнопки — 64 символа; префикс с возрастом съедает до 6.
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅ ' if sid == current else ''}{sessions.ago(age)} · "
                 f"{render.clip(name or sid, 48)}",
            callback_data=f"rs:{sid}",
        )]
        for sid, name, age in found
    ]
    await msg.answer(
        f"сессии {Path(here).name}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data.startswith("rs:"))
async def cb_resume(cb: CallbackQuery) -> None:
    sid = cb.data.removeprefix("rs:")
    sc = scope(cb.message)
    here = cwd(sc)
    store.save_session(sc, here, sid)
    await cb.answer("переключено")
    await cb.message.edit_text(f"сессия: {sid}")
    # Хвост последнего ответа: иначе после переключения на экране один id, и о чём был
    # разговор, видно только в читалке. Шлём последний кусок split — верх длинного
    # ответа при возврате в сессию не нужен, нужен тот, на чём она остановилась.
    path = webui.transcript(here, sid)
    if not path.is_file():
        return
    last = await asyncio.to_thread(sessions.last_message, path)
    if last:
        chunks = render.split(last)
        await send(cb.message, render.md(("…" if len(chunks) > 1 else "") + chunks[-1]))


@dp.message(Command("new"))
async def cmd_new(msg: Message) -> None:
    sc = scope(msg)
    store.drop_session(sc, cwd(sc))
    await msg.answer("сессия сброшена")


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message) -> None:
    global login
    if login:
        login.close()
        login = None
        await msg.answer("логин отменён")
        return
    stopped, dropped = await runner.cancel(scope(msg))
    said = ["остановлено"] if stopped else []
    if dropped:
        said.append(f"очередь очищена: {dropped}")
    await msg.answer(", ".join(said) or "нечего останавливать")


@dp.message(Command("purge"))
async def cmd_purge(msg: Message) -> None:
    """Удаление старых сессий во всех проектах. Без `yes` — только предпросмотр:
    действие необратимо, и одна опечатка в числе не должна ничего снести."""
    args = (msg.text or "").split()[1:]
    days = webui._days(args[0] if args and args[0] != "yes" else None)
    older = days * 86400

    doomed = await asyncio.to_thread(sessions.stale, older)
    if not doomed:
        await msg.answer(f"нет сессий старше {days:g} дн")
        return

    total = sum(r["bytes"] for r in doomed) / (1 << 20)
    if "yes" not in args:
        head = "\n".join(f"{r['ago']:>3} · {render.clip(r['title'] or r['id'], 38)}"
                          for r in doomed[:10])
        tail = f"\n…и ещё {len(doomed) - 10}" if len(doomed) > 10 else ""
        await msg.answer(
            f"старше {days:g} дн: {len(doomed)} сессий, {total:.1f} МБ\n\n{head}{tail}\n\n"
            f"удалить безвозвратно: <code>/purge {days:g} yes</code>", parse_mode="HTML")
        return

    killed = await webui.run_purge(older)
    await msg.answer(
        f"удалено {killed['sessions']} сессий, {total:.1f} МБ\n"
        f"окружений {killed['env']}, осиротевших {killed['orphans']}, "
        f"строк истории {killed['history_lines']}, указателей {killed['pointers']}")


@dp.message(Command("model"))
async def cmd_model(msg: Message) -> None:
    arg = (msg.text or "").partition(" ")[2].strip()
    # Опечатка молча оседала в БД и роняла КАЖДЫЙ следующий промпт 404-й,
    # причём ошибка приходила от claude, а не от бота. Свой список алиасов не ведём —
    # он протухает с каждой новой моделью; спрашиваем сам cli.
    if arg and (err := await runner.check_model(arg)):
        await msg.answer(err)
        return
    store.put("model", arg or None)
    await msg.answer(f"модель: {arg or 'default'}")


@dp.message(Command("cd"))
async def cmd_cd(msg: Message) -> None:
    arg = (msg.text or "").partition(" ")[2].strip()
    path = PROJECTS_DIR / arg
    if not arg or path not in sessions.projects():  # только то, что примонтировано
        await msg.answer("нет такого проекта. /projects — список")
        return
    store.put(f"{scope(msg)}:cwd", str(path))
    await msg.answer(f"проект: {path}")


@dp.message(Command("clone"))
async def cmd_clone(msg: Message) -> None:
    """git clone в /projects. Имя — из URL, если не задано явно."""
    args = shlex.split((msg.text or "").partition(" ")[2])
    if not args:
        await msg.answer("/clone <git-url> [name]")
        return
    url, name = args[0], (args[1] if len(args) > 1 else Path(args[0]).name.removesuffix(".git"))
    # Только имя каталога: `../x` и `a/b` увели бы клон из /projects, а `/cd` его потом
    # не нашёл бы (sessions.projects() сканирует один уровень).
    if name != Path(name).name or name.startswith("."):
        await msg.answer("плохое имя проекта")
        return
    path = PROJECTS_DIR / name
    if path.exists():
        await msg.answer(f"{name} уже есть. /cd {name}")
        return

    await msg.answer(f"клонирую {name}…")
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--", url, str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Ключ из ~/.ssh примонтирован, но хост неизвестен — без этого git виснет на
        # интерактивном «Are you sure you want to continue connecting?».
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
             "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 300)
    except TimeoutError:
        proc.kill()
        await msg.answer("таймаут 5м")
        return
    if proc.returncode:
        await msg.answer(f"❌ {out.decode('utf-8', 'replace')[-1500:]}")
        return
    store.put(f"{scope(msg)}:cwd", str(path))
    await msg.answer(f"✅ проект: {path}")


login: runner.Login | None = None  # активный OAuth-флоу; текст в чате = код, не промпт


@dp.message(Command("login"))
async def cmd_login(msg: Message) -> None:
    global login
    if login:
        login.close()
    login = flow = runner.Login()
    await msg.answer("запускаю логин…")
    url = await flow.start()
    if not url:
        # Не `login`: пока ждали URL, /cancel или код могли обнулить глобалку.
        flow.close()
        if login is flow:
            login = None
        await msg.answer(f"URL не появился за 60с:\n{render.strip_ansi(flow.buf)[-500:]}")
        return
    await msg.answer(f"открой и авторизуйся, потом пришли код сюда:\n{url}")


@dp.message(Command("logout"))
async def cmd_logout(msg: Message) -> None:
    _, out = await runner.cli("auth", "logout")
    await msg.answer(out or "вышел")


async def _cli_reply(msg: Message, *args: str) -> None:
    _, out = await runner.cli(*args, cwd=cwd(scope(msg)))
    await msg.answer(render.strip_ansi(out)[:4000] or "пусто")


@dp.message(Command("mcp"))
async def cmd_mcp(msg: Message) -> None:
    """Прозрачный проброс в `claude mcp`: подкоманды те же, что в CLI."""
    arg = (msg.text or "").partition(" ")[2].strip()
    sub = shlex.split(arg) if arg else ["list"]
    if sub[0] == "rm":  # единственная вольность, остальное уходит в CLI как есть
        sub[0] = "remove"
    await _cli_reply(msg, "mcp", *sub)


@dp.message(Command("plugin", "plugins"))
async def cmd_plugin(msg: Message) -> None:
    """Прозрачный проброс в `claude plugin`: подкоманды те же, что в CLI."""
    arg = (msg.text or "").partition(" ")[2].strip()
    sub = shlex.split(arg) if arg else ["list"]
    # Единственная вольность: rm короче. У плагина это uninstall, у маркетплейса remove.
    if sub[0] == "rm":
        sub[0] = "uninstall"
    elif sub[:2] == ["marketplace", "rm"]:
        sub[1] = "remove"
    await _cli_reply(msg, "plugin", *sub)


async def send(msg: Message, text: str) -> Message:
    """Отправить как HTML, при отказе — тем же текстом без разметки.

    Разметку строит наш конвертер, но входные данные — вывод claude: недобитый тег
    из обрезанного диффа не должен стоить пользователю всего сообщения.
    """
    try:
        return await msg.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest as e:
        log.warning("html отвергнут (%s), шлём как текст", e)
        return await msg.answer(render.plain(text), disable_web_page_preview=True)


async def edit(live: Message, text: str) -> None:
    try:
        await live.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        return
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        log.warning("html отвергнут при правке (%s)", e)
    with contextlib.suppress(TelegramBadRequest):
        await live.edit_text(render.plain(text), disable_web_page_preview=True)


@dp.message(F.text)
async def on_prompt(msg: Message) -> None:
    global login
    if login:  # режим ожидания кода: текст — это код, а не промпт
        ok, detail = await login.submit((msg.text or "").strip())
        login = None
        await msg.answer(f"{'✅' if ok else '❌'} {detail}")
        return
    await handle(msg, (msg.text or "").strip())


@dp.message(F.photo | F.document)
async def on_file(msg: Message) -> None:
    """Файл из чата → на диск, путь уходит в промпт вместе с подписью.

    Claude читает файл сам, если в промпте есть путь, — конвертировать и слать
    содержимое не нужно. Подпись к файлу Telegram кладёт в `caption`, не в `text`.
    """
    if login:  # ждём код из чата — файл сейчас не к месту
        await msg.answer("идёт логин: пришли код или /cancel")
        return
    # photo — набор превью одной картинки, последний элемент самый крупный.
    file = msg.document or msg.photo[-1]
    name = getattr(file, "file_name", None) or f"{file.file_unique_id}.jpg"
    # Path(name).name — Telegram отдаёт file_name как есть, `../` увёл бы файл из inbox.
    dest = INBOX / f"{int(time.time())}-{Path(name).name}"
    INBOX.mkdir(parents=True, exist_ok=True)
    try:
        await msg.bot.download(file, dest)
    except TelegramBadRequest as e:
        # Bot API не отдаёт файлы больше 20 МБ — своего Local Bot API server тут нет.
        log.warning("download failed: %s", e)
        await msg.answer(f"не смог забрать файл (лимит Telegram — 20 МБ)\n{e}")
        return
    caption = (msg.caption or "").strip()
    await handle(msg, f"{caption}\n\n{dest}".strip() if caption else str(dest))


async def handle(msg: Message, prompt: str) -> None:
    """Промпт в текущий проект: живое сообщение с прогрессом, потом итог.

    Занятый скоуп не отказывает, а копит: промпт ждёт очереди и уходит в claude сам,
    как только предыдущий прогон закончился.
    """
    # `!refine текст` → слеш-команда Claude `/refine текст`. Только первый символ,
    # однократно: `почини баг!` и `git commit -m "fix!"` уходят в промпт как есть.
    # Telegram отдаёт `/` своему автокомплиту, поэтому `/` — боту, `!` — Claude.
    if prompt.startswith("!"):
        prompt = "/" + prompt[1:]
    if not prompt:
        return

    sc = scope(msg)
    live = None
    if ahead := runner.ahead(sc):
        live = await send(msg, f"⏳ в очереди, впереди {ahead}")
        # Ключ «⏳» ставим уже сейчас: иначе рестарт оставит это сообщение висеть
        # навсегда, mark_orphan знает только записанное.
        # ponytail: ключ один на скоуп, из нескольких ждущих пометку получит последний.
        store.put(f"{sc}:live", f"{live.chat.id}:{live.message_id}")
    try:
        async with runner.slot(sc):
            await _run(msg, prompt, sc, live)
    except runner.Dropped:
        store.put(f"{sc}:live", None)
        gone = "⏹ отброшено: очередь очистил /cancel"
        # live есть почти всегда: без очереди промпт не ждал бы. Почти — потому что
        # слот мог занять соседнее сообщение из этого же топика, пока мы его считали.
        if live:
            await edit(live, gone)
        else:
            await msg.answer(gone)


async def _run(msg: Message, prompt: str, sc: str, live: Message | None) -> None:
    # Проект и сессию читаем после очереди, а не при постановке: пока промпт ждал,
    # мог смениться проект через /cd, а сессию мог завести предыдущий прогон.
    project = cwd(sc)

    with AUDIT.open("a") as f:
        f.write(f"{int(time.time())}\t{msg.from_user.id}\t{project}\t"
                f"{prompt.replace(chr(10), ' ')}\n")

    run = render.Run(prompt, Path(project).name)
    if live:
        await edit(live, run.text())
    else:
        live = await send(msg, run.text())
    last_text, last_edit = run.text(), 0.0
    # Для дорисовки после рестарта. Ключ со скоупом — у каждого топика своё «⏳».
    store.put(f"{sc}:live", f"{live.chat.id}:{live.message_id}")

    try:
        async for ev in runner.run(
            prompt, project, store.session_of(sc, project), store.get("model"), scope=sc
        ):
            run.feed(ev)
            now = time.monotonic()
            if now - last_edit < THROTTLE:
                continue
            text = run.text()
            if text == last_text:
                continue
            last_text, last_edit = text, now
            await edit(live, text)
    except Exception as e:  # иначе сообщение навсегда зависает на «⏳»
        log.exception("run failed")
        run.done, run.error = True, f"{type(e).__name__}: {e}"

    if run.session_id:
        store.save_session(sc, project, run.session_id)
    store.put(f"{sc}:live", None)

    final, extra = run.parts()
    if final != last_text:
        await edit(live, final)
    # Результат не влез в одно сообщение — досылаем продолжение отдельными.
    for chunk in extra:
        await send(msg, chunk)

    # Длинный запуск: «⏳» уехало вверх ленты, отдельным сообщением зовём обратно.
    if time.monotonic() - run.started > LONG_RUN:
        await msg.answer(f"⬆️ готово: {render.clip(prompt, 60)}", reply_to_message_id=live.message_id)

    # Ошибку дублируем — в отредактированном сообщении её легко проспать. Если она уже
    # уехала отдельными сообщениями (extra), второй раз не шлём.
    if run.error and not extra:
        await send(msg, f"❌ <b>ошибка</b>\n<pre>{render.esc(run.error[:3500])}</pre>")


async def mark_orphan(bot: Bot) -> None:
    """Процессы умерли вместе с контейнером, а «⏳» в чатах осталось висеть.
    Топиков много — обходим все скоупы, по одному «⏳» на каждый."""
    for key, live in store.live_keys():
        store.put(key, None)
        chat_id, _, message_id = live.partition(":")
        with contextlib.suppress(Exception):
            await bot.edit_message_text(
                "⚠️ прервано рестартом", chat_id=int(chat_id), message_id=int(message_id)
            )


async def main() -> None:
    store.conn()
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    if not ALLOWED:
        log.warning("TG_ALLOWED_USER_ID пуст — бот не ответит никому")
    bot = Bot(TOKEN)
    # Автокомплит по «/» в клиенте. Описание Telegram режет на 256 символов — не наш случай.
    # Два скоупа, а не один: дефолтный клиент в группах не подхватывает, и в топиках
    # автокомплит оставался пустым (getMyCommands: default 13, all_group_chats 0).
    # Кнопки «Меню» в группе не будет никакой — setChatMenuButton работает только в личке.
    cmds = [BotCommand(command=c, description=d) for c, d in HELP]
    await bot.set_my_commands(cmds)
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())
    await mark_orphan(bot)
    if WEB_PORT:
        await webui.start(WEB_PORT)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
