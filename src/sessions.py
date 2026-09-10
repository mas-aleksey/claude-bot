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
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/projects"))


def projects() -> list[Path]:
    """Всё, что примонтировано в /projects. Список сканируется, а не конфигурируется:
    добавил mount в compose (или git clone внутрь) — проект появился, рестарт не нужен.

    Живёт здесь, а не в app.py: читают и бот, и читалка, а тащить в читалку aiogram
    с обязательным TG_BOT_TOKEN только за эту функцию не стоит.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    return sorted(p for p in PROJECTS_DIR.iterdir() if p.is_dir())


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


def search(cwd: str, query: str, limit: int = 20) -> list[tuple[str, str, float, str]]:
    """[(session_id, заголовок, возраст, фрагмент)] по подстроке, свежие сверху.

    Ни индекса, ни внешнего grep: замер на живом инстансе — 45 МБ транскриптов
    сканируются целиком за 0.47 с. Поэтому читаем файл, проверяем подстроку, и только
    у совпавших разбираем строки ради фрагмента.

    Ищем по тексту разговора, а не по всему файлу: `tool_result` бывает на мегабайт, и
    попадание в него означало бы «нашлось там, где ты ничего не писал».

    Быстрый путь опирается на то, что claude пишет текст литералами UTF-8, а не
    escape-последовательностями (проверено на живых транскриптах: в файле лежит
    «тест», а не его код). Начнёт экранировать — подстрока перестанет находиться,
    и придётся разбирать каждую строку каждого файла.
    """
    if not (needle := query.strip().lower()):
        return []
    now = time.time()
    out: list[tuple[str, str, float, str]] = []
    files = sorted((TRANSCRIPTS / _slug(cwd)).glob("*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for path in files:
        raw = path.read_text("utf-8", "replace")
        if needle not in raw.lower():
            continue
        if snippet := _snippet(raw, needle):
            out.append((path.stem, title(path), now - path.stat().st_mtime, snippet))
        if len(out) >= limit:
            break
    return out


def _snippet(raw: str, needle: str) -> str:
    """Фрагмент вокруг первого попадания в тексте разговора. Пусто — значит подстрока
    нашлась только в служебных полях или в выводе инструментов, и показывать нечего."""
    for line in raw.splitlines():
        if needle not in line.lower():
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") not in ("user", "assistant"):
            continue
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        text = " ".join((content or "").split())
        at = text.lower().find(needle)
        if at < 0:
            continue
        start = max(0, at - 60)
        return ("…" if start else "") + text[start:at + len(needle) + 90].strip() + "…"
    return ""


def recent(cwd: str, limit: int = 10) -> list[tuple[str, str, float]]:
    """[(session_id, заголовок, возраст в секундах)] проекта, свежие сверху."""
    files = sorted(
        (TRANSCRIPTS / _slug(cwd)).glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True
    )
    now = time.time()
    return [(f.stem, title(f), now - f.stat().st_mtime) for f in files[:limit]]
