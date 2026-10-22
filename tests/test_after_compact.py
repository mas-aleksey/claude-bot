"""Хук после сжатия: handoff своего проекта в записке; нет файлов или мусор — тишина."""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent.parent / "claude" / "hooks" / "after-compact.py"


def run(stdin: str, monkey_dir: Path) -> str:
    src = HOOK.read_text().replace('"/root/.claude/handoff"', repr(str(monkey_dir)))
    return subprocess.run([sys.executable, "-c", src], input=stdin, capture_output=True,
                          text=True, check=True).stdout


def test_note_lists_own_handoffs(tmp_path):
    (tmp_path / "web--panel.md").touch()
    (tmp_path / "other--x.md").touch()
    out = run(json.dumps({"transcript_path": "/t/s.jsonl", "cwd": "/projects/web"}), tmp_path)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "web--panel.md" in ctx and "other--x.md" not in ctx


def test_silent_without_handoffs_or_on_bad_input(tmp_path):
    assert run(json.dumps({"cwd": "/projects/web"}), tmp_path) == ""
    assert run("not json", tmp_path) == ""
