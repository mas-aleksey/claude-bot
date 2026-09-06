# claude-bot

Claude Code in a Telegram chat. Run it on a home server, message it from a phone,
and it works in your repositories — same sessions, same skills, same context you
would get in a terminal.

Long-polling, so no public URL and no webhook. Access is an allowlist of Telegram
user ids — everyone else is ignored.

## What it does

| Command | |
| --- | --- |
| `/status` | current project, model, session, auth state |
| `/projects`, `/cd <name>` | list projects as buttons, switch between them |
| `/clone <git-url> [name]` | clone a repository into the projects directory |
| `/sessions`, `/new` | switch to a recent session, or start a fresh one |
| `/model [alias\|id]` | show or change the model |
| `/cancel` | kill the running invocation |
| `/mcp`, `/plugin` | MCP servers and plugins, passed through to the CLI |
| `/login`, `/logout` | authenticate against a Claude subscription |

Claude's markdown answers are rendered as Telegram HTML — tables included, which
is most of what makes a phone-sized answer readable.

## Two ways to run it

**Assistant** — full access to the host it runs on: the docker socket, the shared
repository, whatever you mount. It manages the machine it lives on.

**Sandbox** — one container per project, with its own Docker-in-Docker daemon. The
host's containers are invisible from inside, so builds and tests in a work
repository cannot reach the services running next door.

This repository ships the assistant. Sandboxes are per-project by nature (own
compose, own `.env`, own skills) and live wherever you keep that project's
configuration; `CLAUDE.sandbox.md` here is the environment prompt they mount.

The image also carries an sshd, so an IDE can attach over Remote Development and
share the bot's working tree and session directory — a conversation started in the
editor continues in Telegram and back.

## Prompts and skills

Claude Code reads one `CLAUDE.md` and one skills directory. This image assembles
both from two sources so that instance-specific and shared parts stay separate:

```
/opt/claude-md/env.md      environment: assistant vs sandbox
/opt/claude-md/common.md   answer style, shared by every instance
        ↓ concatenated by entrypoint.sh
/root/.claude/CLAUDE.md

/opt/skills/shared         claude/skills/ from this repo
/opt/skills/project        the project's own skills, if any
        ↓ symlinked one by one
/root/.claude/skills       project entries win over shared ones
```

Both skill sources are mounted read-write on purpose: Claude edits and creates
skills itself, and those edits land in the repository on the host, ready to be
committed like any other change.

The bundled skills — `refine`, `mr`, `fresh`, `end` — are written to be
project-agnostic, so they are a reasonable starting point rather than a personal
configuration.

## Running it

Requires Docker and a bot token from [@BotFather](https://t.me/BotFather).

```bash
cp .env.example .env      # fill in the token, your Telegram user id and the paths
docker compose up -d --build
```

`USER_DATA` points at a host directory holding the credentials and workspace:
Claude's config and sessions, an ssh key for git remotes, a gitconfig, the projects
themselves. It lives outside this repository deliberately — the service can be
rebuilt from scratch without touching any of it.

`HOST_REPO` is the repository the bot shares with you on the host. Both sides write
to the same `.git`, which is why the container joins the host user's group and sets
`core.sharedRepository=group`; without that, the bot's commits leave objects the
human cannot overwrite.

## Security model

The bot runs as root and mounts the docker socket, so **inside the assistant
container it can do anything the host's Docker can** — stop services, prune images,
read any bind-mounted path. That is the point of the assistant, and it is why
`TG_ALLOWED_USER_ID` is checked on every update.

Sandboxes are the opposite: no host socket, a separate daemon, and only the volumes
that project needs.

Neither variant is meant to face the public internet. There is no inbound HTTP at
all — the bot polls Telegram.

## Development

```bash
uv sync
uv run pytest
```

```
src/
├── app.py        aiogram handlers, allowlist check
├── runner.py     invokes the Claude Code CLI, streams output back
├── sessions.py   session ids per project, slugged from the resolved path
├── render.py     Claude's markdown → Telegram HTML
└── store.py      SQLite: current project, model, session
```

## License

MIT — see [LICENSE](LICENSE).
