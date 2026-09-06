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

This repository ships neither: it builds the image and carries the pieces both
variants mount — the shared skills, the answer style, and `CLAUDE.sandbox.md`.
An instance is a compose file of its own, with its own `.env`, its own volumes and
its own environment prompt, kept wherever you keep that machine's configuration.

The image also carries an sshd, so an IDE can attach over Remote Development and
share the bot's working tree and session directory — a conversation started in the
editor continues in Telegram and back.

## Prompts and skills

Two different things are often conflated here, and this image keeps them apart.

**Answer style is a system prompt concern.** It ships in the image as an output style
and is selected by `--settings`, so it reaches the model as an instruction rather than
as reference material:

```
claude/output-styles/conclusion-first.md   the style itself
claude/settings.json                      {"outputStyle": "conclusion-first"}
        ↓ COPY, then --settings /opt/claude/settings.json
system prompt
```

**`CLAUDE.md` is context.** It describes the environment the instance runs in — the
assistant has a docker socket and the host, a sandbox its own dind — so each instance
mounts its own file straight at `/root/.claude/CLAUDE.md`. Nothing is assembled at
startup, which also means Claude editing that file from inside keeps the edit.

Skills come from two sources and are merged by symlink:

```
/opt/skills/10-base        claude/skills/ from this repo
/opt/skills/20-project     the instance's own skills, if any
        ↓ symlinked one by one
/root/.claude/skills       higher prefixes win over lower ones
```

Both skill sources are mounted read-write on purpose: Claude edits and creates
skills itself, and those edits land in the repository on the host, ready to be
committed like any other change.

The bundled skills — `refine`, `end` and `fresh` — target the sandbox workflow of
working through a project repository, and are written to be project-agnostic, so
they are a reasonable starting point rather than a personal configuration.

## Running it

Requires Docker and a bot token from [@BotFather](https://t.me/BotFather).

Build the image first — every instance runs on top of it:

```bash
docker compose --profile build build
```

Then take [`example/`](example/) — a working sandbox with the project-specific bits
removed. Copy it, rename, fill in the `.env`:

```bash
cp -r example ../my-project && cd ../my-project
sed -i 's/example/my-project/g' docker-compose.yml
cp .env.example .env
docker compose up -d --build
```

Its `docker-compose.yml` carries the reasoning behind every mount in comments, which
is the part worth reading before changing anything.

`USER_DATA` points at a host directory holding the credentials and workspace:
Claude's config and sessions, an ssh key for git remotes, a gitconfig, the projects
themselves. It lives outside this repository deliberately — the service can be
rebuilt from scratch without touching any of it.

An assistant additionally mounts `/var/run/docker.sock` and the repository it shares
with you on the host. When both sides write to the same `.git`, the container has to
join the host user's group and set `core.sharedRepository=group`; without that, the
bot's commits leave objects the human cannot overwrite. A sandbox instead runs a
`docker:dind` sidecar and shares its network namespace, so `DOCKER_HOST` points at
the sandbox daemon and the host's containers stay invisible.

### Sizing an instance

The image ships python 3.14 + uv, node 22 + npm, git, docker CLI, psql, jq and vim.
Anything a project needs beyond that goes into a `Dockerfile` of its own, layered
`FROM claude-bot:latest`. Answer these while writing the compose file above, not after
the first command fails inside the container:

| Question | Where the answer lands |
| --- | --- |
| Runtime and version — python 3.13? node 20? go? | `Dockerfile`: `uv python install <ver>` plus `ENV UV_PYTHON=<ver>` — the version, never a path: a standalone build's path carries the patch level and goes stale on the next update |
| Package manager — uv, poetry, pnpm, yarn | `Dockerfile`: `corepack enable pnpm` for node (corepack ships with node and honours `packageManager` in the repository), `pip install poetry` for poetry |
| Build tools and CLIs — make, gh, glab, graphviz, terraform | `Dockerfile`, an `apt-get install` layer or a downloaded binary |
| Does it need a Docker daemon — image builds, testcontainers, compose in tests | Yes: a `docker:dind` sidecar, `network_mode: service:<name>-dind`, `DOCKER_HOST=tcp://localhost:2375`, and the sshd port published on the sidecar. No: none of that — a single service, ports on the bot itself |
| Databases and services the tests need | If they come up as containers, the row above is a yes |
| Project-specific skills — deploy, code review, log queries | `skills/`, mounted as `/opt/skills/20-project` |

Two rules cover the whole table.

**Install at build time, not at runtime.** The container is recreated on every
image change; an `apt-get` run from inside it disappears with the old one.

**Move tool caches out of `/root` with `ENV`.** A sandbox mounts the host's data
directory over `/root`, which hides that path in the image layer completely. A cache
warmed during the build is then invisible at runtime, and the first command in each
new container goes back to the network — which means it fails when the network is
gone. `COREPACK_HOME=/opt/corepack` and `UV_PYTHON_INSTALL_DIR=/opt/uv-python` are
the two that bite in practice.

Without a daemon the instance is markedly simpler: no privileged sidecar, no shared
network namespace, no restart ordering between the two containers. When in doubt keep
it — an idle dind costs a container and nothing else.

## Security model

An assistant instance runs as root and mounts the docker socket, so **inside that
container it can do anything the host's Docker can** — stop services, prune images,
read any bind-mounted path. That is the point of an assistant, and it is why
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
