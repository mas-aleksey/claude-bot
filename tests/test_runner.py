import asyncio
import json

import pytest

import runner


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / ".claude.json"
    monkeypatch.setattr(runner, "CONFIG", str(path))
    return path


def read(path):
    return json.loads(path.read_text())


def test_skip_onboarding_creates_both_flags(config):
    # Свежий инстанс: файла ещё нет, а человек уже заходит по ssh.
    runner.skip_onboarding()
    cfg = read(config)
    assert cfg["hasCompletedOnboarding"] is True
    # ssh-сессия стартует в /root — без своего trust вылезет второй диалог.
    assert cfg["projects"]["/root"]["hasTrustDialogAccepted"] is True


def test_skip_onboarding_keeps_other_keys(config):
    # Конфиг ведёт сам claude-cli: правка не должна ничего затирать.
    config.write_text(json.dumps({"theme": "dark-ansi", "oauthAccount": {"id": "x"}}))
    runner.skip_onboarding()
    cfg = read(config)
    assert cfg["theme"] == "dark-ansi"
    assert cfg["oauthAccount"] == {"id": "x"}


def test_skip_onboarding_does_not_touch_existing_projects(config):
    config.write_text(json.dumps({"projects": {"/projects/x": {"hasTrustDialogAccepted": True}}}))
    runner.skip_onboarding()
    cfg = read(config)
    assert cfg["projects"]["/projects/x"]["hasTrustDialogAccepted"] is True
    assert cfg["projects"]["/root"]["hasTrustDialogAccepted"] is True


def test_skip_onboarding_idempotent(config):
    runner.skip_onboarding()
    first = config.read_text()
    runner.skip_onboarding()
    assert config.read_text() == first


def test_patch_config_survives_broken_file(config):
    # Битый JSON не должен ронять логин — пересоздаём с нуля.
    config.write_text("{ not json")
    runner.skip_onboarding()
    assert read(config)["hasCompletedOnboarding"] is True


def test_trust_adds_cwd(config):
    runner.trust("/projects/new")
    assert read(config)["projects"]["/projects/new"]["hasTrustDialogAccepted"] is True


# --- очередь на скоуп -------------------------------------------------------------


async def test_slot_serializes_scope_and_keeps_order():
    """Занятый скоуп копит: промпты идут по одному и в порядке постановки."""
    order: list[int] = []
    live: list[int] = []

    async def worker(n):
        async with runner.slot("s"):
            live.append(n)
            assert live == [n]  # одновременно в скоупе только один прогон
            await asyncio.sleep(0)
            order.append(n)
            live.remove(n)

    await asyncio.gather(*(worker(i) for i in range(3)))
    assert order == [0, 1, 2]
    assert runner.ahead("s") == 0  # состояние скоупа убрано за собой


async def test_cancel_drops_queue():
    """`/cancel` — это «стоп всему», иначе следом сама собой поедет следующая задача."""
    started, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with runner.slot("s"):
            started.set()
            await release.wait()

    async def waiter():
        with pytest.raises(runner.Dropped):
            async with runner.slot("s"):
                pytest.fail("отброшенный промпт не должен стартовать")

    first = asyncio.create_task(holder())
    await started.wait()
    second = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # дать второму встать в очередь
    assert runner.ahead("s") == 2

    # Процесса в _runs нет (claude тут не поднимали), но очередь обязана очиститься.
    assert await runner.cancel("s") == (False, 1)
    release.set()
    await asyncio.gather(first, second)
    assert runner.ahead("s") == 0


def drive(events: list[dict]) -> list[bool]:
    """Прогнать поток через Drain и вернуть решение на каждом событии."""
    drain = runner.Drain()
    out = []
    for ev in events:
        drain.feed(ev)
        out.append(drain.done(ev))
    return out


def sysev(subtype: str, **kw) -> dict:
    return {"type": "system", "subtype": subtype, **kw}


def test_drain_closes_when_no_background():
    """Обычный промпт: фоновых задач нет, выход по первому же result — как раньше."""
    assert drive([sysev("init"), {"type": "assistant"}, {"type": "result"}])[-1] is True


def test_drain_waits_for_background_task():
    """Порядок с живого прогона: на result задача ещё идёт, закрывать stdin нельзя,
    иначе claude унесёт её с собой (файл вывода остаётся со словом `[killed]`)."""
    got = drive([
        sysev("init"),
        sysev("background_tasks_changed", tasks=[{"task_id": "b1", "task_type": "local_bash"}]),
        sysev("task_started", task_id="b1", is_backgrounded=True),
        {"type": "result"},                       # ход кончился, задача жива
        sysev("background_tasks_changed", tasks=[]),
        sysev("task_notification", task_id="b1", status="completed"),
        sysev("init"),                            # ход по уведомлению
        {"type": "assistant"},
        {"type": "result"},
    ])
    assert got[3] is False   # первый result: ждём задачу
    assert got[-1] is True   # второй: ждать больше нечего


def test_drain_waits_for_subagent():
    """Субагент приезжает тем же списком (`local_agent`), правило на оба одно."""
    got = drive([
        sysev("init"),
        sysev("background_tasks_changed", tasks=[{"task_id": "a1", "task_type": "local_agent"}]),
        {"type": "result"},
    ])
    assert got[-1] is False


def test_drain_holds_through_subagent_chatter():
    """События фонового субагента текут в общий поток без своего `init`. Отметку об
    ожидании они снимать не должны — иначе stdin закроется прямо перед ходом по ней."""
    got = drive([
        sysev("init"),
        sysev("background_tasks_changed", tasks=[{"task_id": "a1"}]),
        {"type": "result"},
        sysev("background_tasks_changed", tasks=[]),
        sysev("task_notification", task_id="a1", status="completed"),
        {"type": "assistant"},                    # болтовня субагента, ход не начался
        {"type": "result"},                       # гонка: итог раньше хода по уведомлению
    ])
    assert got[-1] is False


def test_drain_survives_missing_events():
    """CLI переименует события — список останется пустым, и поведение выродится в
    сегодняшнее: выход по первому result, без зависшего процесса."""
    assert drive([{"type": "assistant"}, {"type": "result"}])[-1] is True


def test_drain_bounds_the_wait_for_a_promised_turn():
    """Уведомление обещает ход, но `--resume` отдаёт его о задачах прошлой сессии прямо
    в ход человека — второго `init` не будет. Ждать такое можно только с потолком."""
    drain = runner.Drain()
    assert drain.wait() is None                       # обычный прогон — ждём сколько угодно
    drain.feed(sysev("task_notification", task_id="b1"))
    assert drain.wait() == runner.Drain.GRACE
    drain.give_up()
    assert drain.wait() is None


def test_drain_waits_forever_for_a_live_task():
    """Потолок — только на обещанный ход. Живая задача разбудит поток сама, и сборка
    на сорок минут не должна упираться в секунды."""
    drain = runner.Drain()
    drain.feed(sysev("background_tasks_changed", tasks=[{"task_id": "b1"}]))
    drain.feed(sysev("task_notification", task_id="b1"))
    assert drain.wait() is None


class _Stdin:
    def __init__(self):
        self.written, self.closed = b"", False

    def write(self, data):
        self.written += data

    async def drain(self):
        pass

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class _Proc:
    """claude, который отдал события и замолчал навсегда — как на зависшем прогоне."""

    returncode = None
    pid = 1

    def __init__(self, lines):
        self.lines = list(lines)
        self.stdin, self.stdout, self.stderr = _Stdin(), self, self

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        await asyncio.Event().wait()  # тишина до конца времён

    async def read(self):
        return b""

    async def wait(self):
        return 0


async def test_run_closes_stdin_when_the_promised_turn_never_starts(monkeypatch, tmp_path):
    """Авария surf 2026-09-15: уведомление о брошенных задачах пришло, ход по нему —
    нет, и прогон висел с открытым stdin при законченной работе."""
    monkeypatch.setattr(runner.Drain, "GRACE", 0.01)
    proc = _Proc([json.dumps(e).encode() + b"\n" for e in (
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "task_notification", "task_id": "b1"},
        {"type": "result"},
    )])

    async def fake_exec(*a, **kw):
        return proc

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runner, "trust", lambda cwd: None)
    monkeypatch.setattr(runner, "_runs", {})

    gen = runner.run("промпт", str(tmp_path), scope="s")
    for _ in range(3):
        await asyncio.wait_for(anext(gen), 1)
    assert not proc.stdin.closed  # придержали: ход по уведомлению ещё мог начаться

    with pytest.raises(TimeoutError):  # событий больше нет, и не будет
        await asyncio.wait_for(anext(gen), 1)
    assert proc.stdin.closed
    await gen.aclose()


@pytest.mark.asyncio
async def test_check_model_asks_the_catalog(monkeypatch):
    """Проверка модели идёт по каталогу, а не по тексту жалобы CLI: формулировка уже
    менялась, и `/model фигня` сохранялся молча, ломая следующий прогон."""
    catalog = [{"id": "claude-opus-5", "name": "Opus 5"}]
    monkeypatch.setattr(runner, "models", lambda: asyncio.sleep(0, catalog))

    assert await runner.check_model("claude-opus-5") is None
    assert await runner.check_model("opus") is None          # алиас каталог не перечисляет
    assert "не знаю модель" in await runner.check_model("sonnet-5")


@pytest.mark.asyncio
async def test_check_model_allows_everything_without_a_catalog(monkeypatch):
    """Каталога нет — значит нет и интернета. Запрещать смену модели из-за этого нечем."""
    monkeypatch.setattr(runner, "models", lambda: asyncio.sleep(0, []))
    assert await runner.check_model("что угодно") is None


def test_default_model_comes_from_settings(tmp_path, monkeypatch):
    """Дефолт показывают и бот, и панель. Раньше в этом месте стояло слово «default»,
    из которого не следует ничего."""
    path = tmp_path / "settings.json"
    path.write_text('{"model": "claude-opus-5"}', encoding="utf-8")
    monkeypatch.setattr(runner, "SETTINGS", str(path))
    assert runner.default_model() == "claude-opus-5"

    path.write_text("не json", encoding="utf-8")
    assert runner.default_model() == "default"   # битый файл не должен ронять /status


async def test_resolve_model_matches_a_catalog_row(monkeypatch):
    """Модель бота панель показывает обычной строкой списка, поэтому и приходить она
    должна в виде строки каталога. Алиас — это свежая модель семейства, каталог
    отсортирован по свежести."""
    catalog = [{"id": "claude-opus-5", "name": "Opus 5"},
               {"id": "claude-opus-4-8", "name": "Opus 4.8"}]
    monkeypatch.setattr(runner, "models", lambda: asyncio.sleep(0, catalog))

    assert (await runner.resolve_model("claude-opus-4-8"))["name"] == "Opus 4.8"
    assert (await runner.resolve_model("opus"))["id"] == "claude-opus-5"   # алиас — свежая
    # Семейства нет: отдаём как есть, панель покажет сырую строку отдельным пунктом.
    assert await runner.resolve_model("default") == {"id": "default", "name": "default"}
