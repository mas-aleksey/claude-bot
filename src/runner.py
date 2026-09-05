"""Запуск claude-cli: стрим-режим для промптов, обычный пайп для подкоманд."""

import asyncio
import contextlib
import json
import os
import pty
import re
import signal
import subprocess
from collections.abc import AsyncIterator

from render import strip_ansi

BASE = ["claude", "--permission-mode", "bypassPermissions"]
CREDS = "/root/.claude/.credentials.json"
CONFIG = "/root/.claude.json"
# Домен OAuth уже переезжал (claude.ai → claude.com), поэтому ловим по пути /oauth/,
# а не по списку хостов. Проверено на 2.1.220: claude.com/cai/oauth/authorize?...
URL_RE = re.compile(r"https://\S+/oauth/\S+")

_current: asyncio.subprocess.Process | None = None


def busy() -> bool:
    return _current is not None and _current.returncode is None


def trust(cwd: str) -> None:
    """Без `hasTrustDialogAccepted` claude в новой директории игнорит settings.json
    и просит принять диалог интерактивно — в headless это тупик. Диалог тут не нужен:
    в /projects попадает только то, что смонтировал владелец бота."""
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    proj = cfg.setdefault("projects", {}).setdefault(cwd, {})
    if proj.get("hasTrustDialogAccepted"):
        return
    proj["hasTrustDialogAccepted"] = True
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


async def run(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """Событие за событием из `--output-format stream-json`.

    Служебные события бота отдаются с типом `_bot` — так вызывающему не нужен
    второй канал под ошибки и код возврата.
    """
    global _current

    trust(cwd)
    argv = [*BASE, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if session_id:
        argv += ["--resume", session_id]
    if model:
        argv += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # своя группа — /cancel бьёт по всем детям
        # Одно событие = одна строка, а в ней бывает целый файл. Дефолтные 64 KiB
        # роняют readline на `Separator is found, but chunk is longer than limit`.
        limit=16 * 1024 * 1024,
    )
    _current = proc

    try:
        async for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"type": "_bot", "kind": "raw", "text": line.decode("utf-8", "replace")}

        rc = await proc.wait()
        err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
        if rc != 0:
            yield {"type": "_bot", "kind": "error", "rc": rc, "text": err}
    finally:
        if _current is proc:
            _current = None


async def cancel() -> bool:
    """SIGTERM группе, через 2 с — SIGKILL. proc.kill() оставил бы живых детей."""
    proc = _current
    if proc is None or proc.returncode is not None:
        return False
    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except TimeoutError:
        os.killpg(pgid, signal.SIGKILL)
    return True


async def cli(*args: str, cwd: str | None = None, timeout: float = 60) -> tuple[int, str]:
    """Неинтерактивная подкоманда claude → (код возврата, stdout+stderr)."""
    if cwd:
        trust(cwd)
    proc = await asyncio.create_subprocess_exec(
        "claude",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


class Login:
    """`claude auth login` в pty: без tty команда молча висит (проверено, POC шаг 1).

    Бот выдирает URL из потока и шлёт в чат, код из чата пишет процессу в stdin.
    """

    def __init__(self) -> None:
        self.master, self._slave = pty.openpty()
        self.proc: subprocess.Popen | None = None
        self.buf = ""

    async def start(self) -> str | None:
        """Запустить и дождаться URL. None — если URL не появился."""
        # ASYNC101: Popen тут не блокирует — pty, ждём потом через _read_until.
        self.proc = subprocess.Popen(  # noqa: ASYNC220
            ["claude", "auth", "login", "--claudeai"],
            stdin=self._slave,
            stdout=self._slave,
            stderr=self._slave,
            start_new_session=True,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(self._slave)
        os.set_blocking(self.master, False)
        return await self._read_until(URL_RE, 60)

    async def submit(self, code: str) -> tuple[bool, str]:
        os.write(self.master, code.encode() + b"\n")
        # Успех определяем по факту появления кред, а не по тексту в TUI.
        for _ in range(60):
            await asyncio.sleep(1)
            if os.path.exists(CREDS):
                self.close()
                return True, "авторизован"
            if self.proc and self.proc.poll() is not None:
                break
        self.close()
        return False, strip_ansi(self.buf)[-500:]

    async def _read_until(self, rx: re.Pattern, timeout: float) -> str | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(0.3)
            try:
                chunk = os.read(self.master, 65536)
            except BlockingIOError:
                continue
            except OSError:
                break
            self.buf += chunk.decode("utf-8", "replace")
            if m := rx.search(strip_ansi(self.buf)):
                return m.group(0)
        return None

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.close(self.master)


async def auth_status() -> dict:
    rc, out = await cli("auth", "status", "--json", timeout=20)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": out or f"rc={rc}"}


async def check_model(model: str) -> str | None:
    """Валиден ли алиас модели — спрашиваем сам claude, чтобы не вести свой список.
    С пустым stdin cli печатает жалобу на незнакомую модель и выходит ДО обращения
    к API, так что проверка бесплатная. Возвращает текст ошибки или None."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--model", model,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    err = (await proc.communicate())[1].decode("utf-8", "replace")
    return err.strip() if "is not a model" in err else None
