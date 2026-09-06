ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.5.27 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON=/usr/local/bin/python \
    UV_NO_CACHE=1

# node is the runtime for claude-code; docker-ce-cli + compose-plugin cover host
# operations through docker.sock (in project sandboxes — through their own dind).
# Trap for instances on the HOST daemon (the assistant): relative paths in a compose
# file (`./config.yaml`) are resolved by the daemon, i.e. by the host, while the working
# copy of the repo is mounted here under a different path — docker will silently create
# an empty directory instead of the config. So run compose from the directory with the
# host path (see CLAUDE.md), not from /projects/localhome.
# Project sandboxes do not have this: /projects is mounted identically in the bot and dind.
# git/openssh-client — working with project repositories.
# openssh-server — attaching to the container from an IDE (PyCharm/VSCode Remote): the bot
# and the human share one container, hence one working tree and one claude session folder.
# jq/postgresql-client — the tools claude reaches for most often.
# vim — edits over ssh from inside the container; the slim image has not even vi.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git openssh-client openssh-server \
        jq postgresql-client vim \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        nodejs docker-ce-cli docker-compose-plugin \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# A separate layer, and behind an ARG: the cli's self-update lives in /usr/lib/node_modules,
# there is no volume there — so it only ever updates on a rebuild. Changing the version
# invalidates this layer alone, the apt layer above is reused:
#   docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.240 <service>
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# /run/sshd — without it sshd dies with "Missing privilege separation directory".
# sshd_config.d/ is picked up by the image's default config through Include.
RUN mkdir -p /run/sshd
COPY sshd_config /etc/ssh/sshd_config.d/10-container.conf
COPY entrypoint.sh /entrypoint.sh

WORKDIR /src

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=.,target=/src \
    uv sync --no-editable --no-dev --frozen

# UV_PROJECT_ENVIRONMENT has to be /usr/local for the line above and nothing else: the
# bot's dependencies go into the system python, it has no venv of its own. Past that point
# the variable is dangerous — at runtime `uv sync` in a working repository (sandboxes,
# /projects) writes to the same place, and the project's package shadows the bot's module:
# a package `app/` on top of `app.py` breaks `python -m app` with
# `No module named app.__main__`. It does not surface at once — the loaded module stays in
# memory and the container dies on the next restart. So it is reset here rather than in
# every sandbox: a new one inherits the safe value. Empty = uv's default, `./.venv` next
# to the repository. Details — docs/bot.md, section "Песочница проекта".
#
# UV_FROZEN is likewise absent from the ENV above — the bot's build passes `--frozen` as a
# flag, whereas an inherited variable would forbid `uv lock` in a sandbox's working
# repositories ("Unable to find lockfile"), including when setting up a new project.
#
# UV_NO_CACHE is switched off here by the same logic: during the build it is right (the
# cache does not settle into an image layer, it has its own `--mount=type=cache` above),
# while at runtime it would make every `uv sync` in a sandbox download packages again. In
# sandboxes the default `/root/.cache/uv` falls inside the `${USER_DATA}/root:/root` mount
# and survives a container recreate; the assistant has individual files covered by mounts,
# so its cache lives in the rw layer and is lost on recreate — harmless, it just downloads
# again.
#
# The value is `0`, not empty: UV_NO_CACHE is boolean and treats `''` as invalid
# (`invalid value '' for '--no-cache'`) — a sandbox build died on the very first `uv`.
# ENV cannot remove a variable from the environment, hence exactly `0`. For the string
# UV_PROJECT_ENVIRONMENT it is the other way round: empty is precisely "take the default".
ENV UV_PROJECT_ENVIRONMENT= \
    UV_NO_CACHE=0

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app"]
