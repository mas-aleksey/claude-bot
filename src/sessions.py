"""Список сессий Claude для проекта: транскрипты из ~/.claude/projects/<slug>/.

CLI листинга не даёт (`claude project` — про state, не про сессии), читаем файлы.
Один .jsonl на сессию, имя файла = session_id.
"""

import json
import os
import re
import time
from pathlib import Path

TRANSCRIPTS = Path(os.environ.get("CLAUDE_TRANSCRIPTS", "/root/.claude/projects"))


def _slug(cwd: str) -> str:
    """Slug папки транскриптов. Не только `/`: любой не-алфанумерик → `-`
    (`my_project` → `my-project`). Путь резолвим — slug строится от realpath
    (`/tmp` → `/private/tmp`). Промах по slug = молча пустой список, поэтому тест
    в tests/test_sessions.py прибивает оба правила."""
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(cwd))


def title(path: Path) -> str:
    """Заголовок сессии. `ai-title` есть только у интерактивных запусков — у наших
    headless (`-p`) его не пишут, поэтому основной источник `last-prompt`."""
    found = ""
    for line in path.read_text("utf-8", "replace").splitlines():
        if '"ai-title"' not in line and '"last-prompt"' not in line:
            continue  # дешёвый отсев: json.loads на каждой строке транскрипта дорог
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        found = ev.get("aiTitle") or ev.get("lastPrompt") or found
    return found


def ago(seconds: float) -> str:
    """Возраст в 2-3 символа: `5м`, `3ч`, `2д`. Абсолютная дата не нужна — сессии
    выбирают по «та, вчерашняя», а на кнопку и так лезет только 64 символа."""
    for div, suffix in ((60, "с"), (60, "м"), (24, "ч"), (365, "д")):
        if abs(seconds) < div:
            return f"{int(seconds)}{suffix}"
        seconds /= div
    return f"{int(seconds)}г"


def recent(cwd: str, limit: int = 10) -> list[tuple[str, str, float]]:
    """[(session_id, заголовок, возраст в секундах)] проекта, свежие сверху."""
    files = sorted(
        (TRANSCRIPTS / _slug(cwd)).glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    now = time.time()
    return [(f.stem, title(f), now - f.stat().st_mtime) for f in files[:limit]]
