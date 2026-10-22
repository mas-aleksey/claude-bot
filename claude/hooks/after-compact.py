#!/usr/bin/env python3
"""SessionStart(compact): after compaction, point the model at this project's handoff notes.

The path to the full transcript is not repeated here: the CLI already appends it to the
summary. No notes or any failure — print nothing; the hook informs, it never blocks.
"""

import glob
import json
import os
import sys
import time

HANDOFF = "/root/.claude/handoff"


def note(inp: dict) -> str:
    project = os.path.basename((inp.get("cwd") or os.getcwd()).rstrip("/"))
    files = sorted(glob.glob(f"{HANDOFF}/{glob.escape(project)}--*.md"),
                   key=os.path.getmtime, reverse=True)
    if not files:
        return ""
    lines = ["The context was just compacted. Handoff notes of this project (newest "
             "first); if one is about the current task, read it:"]
    lines += [f"- `{f}` ({time.strftime('%Y-%m-%d', time.localtime(os.path.getmtime(f)))})"
              for f in files[:5]]
    return "\n".join(lines)


def main() -> None:
    try:
        inp = json.load(sys.stdin)
        if text := note(inp):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": text}}))
    except Exception:
        pass


if __name__ == "__main__":
    main()
