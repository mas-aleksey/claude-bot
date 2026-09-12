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
