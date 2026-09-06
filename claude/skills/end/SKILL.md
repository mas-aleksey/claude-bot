---
name: end
description: Clean up the working environment — a fresh base branch, dead branches and worktrees, build caches, docker, package-manager caches. Use on "/end", "clean up", "clear the junk", "free up space", "switch to dev and clean up", their Russian equivalents ("почисти", "убери мусор", "освободи место"), and whenever the disk is filling up.
allowed-tools: Bash, Read, AskUserQuestion
---

# end — environment cleanup

Three levels. The first is always safe, the second is the default, the third only
on an explicit request or when space is genuinely running out.

**Rule:** measure first, delete second. Report as a table of "what / how much was
freed". Never delete anything silently that a command cannot recreate.

`<base>` is the project's base branch: usually `dev`; no such branch — the default
one (`git symbolic-ref refs/remotes/origin/HEAD`) or whatever `CLAUDE.md` names.
Do not substitute `dev` without looking: in a repository on `main`, switching takes
the work somewhere else.

## 0. Measure

```bash
/root/.claude/skills/end/scripts/measure.sh
```

Remember `STATE=<number>` from the output — free kilobytes before the cleanup. At
the end, `measure.sh <that number>` works out how much was freed. Without the first
measurement there is nothing to report: the delta has nowhere to come from.

## 1. Git — a fresh base (safe)

```bash
git worktree prune
git fetch origin --prune
git switch <base> && git pull --ff-only origin <base>
```

Dirty tree — do **not** switch, tell the user and stop.

Dead branches whose upstream is gone:

```bash
/root/.claude/skills/end/scripts/prune-branches.sh
```

The script only ever runs `-d`, and prints the ones it could not delete as a
separate list. **Ask** about those, never reach for `-D` yourself: a squash-merge
leaves a branch looking "unmerged" although its content is already in the base —
and a branch with lost commits looks exactly the same.

Check and show without deleting:

```bash
git stash list          # old stashes often hang off deleted branches
git count-objects -v    # packs > 10 or prune-packable > 0 → git gc
```

## 2. Build junk (default)

Python caches in the repository — exclude `.venv`, or you wipe an installed package:

```bash
find . -path '*/.venv' -prune -o \
  \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) \
  -print0 | xargs -0 rm -rf
find . -path '*/.venv' -prune -o -name '*.pyc' -print0 | xargs -0 rm -f
```

Docker — check whose daemon it is first. The skill is shared, the instances are not:

```bash
echo "${DOCKER_HOST:-host socket}"
```

Empty or a socket means this is **not** a sandbox: delete nothing, tell the user and
move on to the next item.

`tcp://localhost:2375` is your own dind, safe to clean:

```bash
docker container prune -f          # stopped containers
docker image prune -f              # dangling images
docker builder prune -f            # build cache
```

Do **not** stop running containers. Space is freed by `prune`, which has already run
here, not by stopping them — and a stack you shut down is one the user has to bring
back up. Freeing what running containers hold is level 3, and only on an explicit
request:

```bash
docker ps -q | xargs -r docker stop && docker container prune -f
```

Leave tagged images and volumes alone: a stack's volume holds a database with
migrations and the run's data, and recreating it costs more than the space saved.
Delete them one by one, on an explicit request, having checked that nobody holds
them — a volume's name does not prove who owns it:

```bash
docker ps -a --filter volume=<volume>    # empty — nobody's, safe to remove
```

A guard hook refusing a deletion is protection, not breakage: hand the user the
ready commands and stop.

What **not** to touch: `.venv`, `.env`, `.run-env`, `*.egg-info` (an editable
install points at it), `node_modules` unless asked to reinstall.

## 3. Caches and what piles up in /root (grows silently, in gigabytes)

`/root` is a volume: caches survive a container restart and never clean themselves.

```bash
npm cache clean --force        # /root/.npm easily reaches 1 GB
pip cache purge
uv cache prune
git gc --prune=now             # packs, if count-objects showed a lot
env -u UV_NO_CACHE uv cache prune --cache-dir /root/.cache/uv
```

uv gotcha: the environment sets `UV_NO_CACHE=1`, so a bare `uv cache prune` cleans a
temporary directory under `/tmp` and lies with "no unused entries", leaving the real
cache in `/root/.cache/uv` untouched. Both flags are required.

What remains of the uv cache after a prune is `archive-v0` and `git-v0` — unpacked
wheels and git dependencies backing live venvs. Wipe those only if space is running
out: the price is reinstalling the environment from scratch.

Session transcripts, `/root/.claude/projects/<project>/` — hundreds of megabytes.
They are the conversation history and `tool-results`. Delete **only** on an explicit
request, and only older than two weeks:

```bash
find /root/.claude/projects -name '*.jsonl' -mtime +14 -delete
find /root/.claude/projects -path '*/tool-results/*' -mtime +14 -delete
```

The same directory holds `<project>/memory/` — long-term memory. The masks above do
not touch it, and must never be widened to `*.md` or pointed at the directory:
there is no way to restore it.

## Report

```bash
/root/.claude/skills/end/scripts/measure.sh <STATE from step 0>
```

A table: what was cleaned, how much was freed (the number comes from the "freed"
line). As a separate list — what was left and why. Anomalies found along the way
(junk in the git index, someone else's worktree, an orphaned stash, unmerged dead
branches) go in their own item, as a suggestion rather than silently.
