ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.5.27 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON=/usr/local/bin/python \
    UV_NO_CACHE=1

# node is the runtime for claude-code; docker-ce-cli + compose-plugin talk to the
# sandbox's own dind. /projects is mounted identically in the bot and in dind, so a
# relative path in a compose file resolves to the same place on both sides — the daemon
# resolves it, not the client, and a mismatch would have docker silently create an empty
# directory instead of mounting the file.
# git/openssh-client — working with project repositories.
# openssh-server — attaching to the container from an IDE (PyCharm/VSCode Remote): the bot
# and the human share one container, hence one working tree and one claude session folder.
# jq/postgresql-client — the tools claude reaches for most often.
# poppler-utils — pdftotext: a pdf arrives as an attachment often enough, and without it
# the only way to read one is a python library installed on the spot, every time.
# vim — edits over ssh from inside the container; the slim image has not even vi.
# iproute2/iputils-ping/dnsutils — inspecting a network path from inside the container:
# whether an interface exists, whether an address answers, whether a name resolves. The
# slim image has none of the three, so the first question after a tunnel comes up ("is
# tun0 actually here?") has no way to be answered — `ip -br a` prints nothing and the
# fallback is reading /proc/net/dev by hand. Kept here rather than in a sandbox layer
# because the answer is the same everywhere and the three cost about two megabytes.
# The VPN client itself is deliberately NOT here — a tunnel is a sidecar container in the
# instance's compose file, see example/docker-compose.yml.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git openssh-client openssh-server \
        jq postgresql-client poppler-utils vim iproute2 iputils-ping dnsutils \
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

# kubectl, behind an ARG for the same reason as the line above: the client is only
# supported one minor away from the server, so an instance whose cluster sits on another
# minor overrides it at build time instead of patching this file:
#   docker compose build --build-arg KUBECTL_VERSION=v1.33.4 <service>
# A pinned default rather than stable.txt: the latter turns every rebuild into a version
# bump nobody asked for, and a client that silently drifted two minors ahead of the
# cluster fails on individual API calls, not at startup — the worst way to find out.
# No kubeconfig here, and none baked into any image: the file carries cluster credentials
# and stays in the instance's ${USER_DATA} (see example/docker-compose.yml).
ARG KUBECTL_VERSION=v1.36.2
RUN curl -fsSLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client=true --output=yaml | grep -q "gitVersion: ${KUBECTL_VERSION}"

# /run/sshd — without it sshd dies with "Missing privilege separation directory".
# sshd_config.d/ is picked up by the image's default config through Include.
RUN mkdir -p /run/sshd
COPY sshd_config /etc/ssh/sshd_config.d/10-container.conf
COPY entrypoint.sh /entrypoint.sh

# The answer style is part of the product, not per-instance configuration: it is the same
# everywhere and changes with the image. Hence COPY, rather than a mount in every compose
# file. runner.py passes `--settings /opt/claude/settings.json`, which replaces a section
# of the system prompt, while the instance's CLAUDE.md stays environment context.
# /opt/claude rather than /root/.claude — the latter is covered by the ${USER_DATA}/root
# mount, which would hide anything the image puts there.
COPY claude/settings.json /opt/claude/settings.json
COPY claude/output-styles /opt/claude/output-styles

WORKDIR /src

# COPY, а не --mount=type=bind: BuildKit не включает bind-маунт контекста в ключ кеша
# слоя, поэтому правка в src/ его НЕ инвалидировала — шаг уходил в CACHED, а образ
# собирался зелёным со старым кодом внутри. Ловится только `grep` внутри контейнера,
# поэтому исходники приезжают копированием. Два слоя вместо одного оставляют кеш
# зависимостям: pyproject и uv.lock меняются редко, src/ — на каждой правке.
#
# --reinstall-package: собранный wheel проекта uv кеширует по (имя, версия), а версия в
# pyproject не двигается. Окружение слоя всегда чистое, и без флага туда ставится
# прежний артефакт из кеша — проверено на отдельном проекте, содержимое src/ в ключ
# не входит. Пересобирается один пакет, зависимости кеш отдаёт как раньше.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev --frozen

# Каталог чистится тем же слоем: пакет уехал в site-packages, а оставленный в образе
# /src/pyproject.toml виден всем, кто наследует WORKDIR. На этом падала сборка образа
# песочницы: `uv run python -V` в /src подхватывал requires-python = ">=3.14" и отвергал
# младший питон. При bind-маунте такого не было, /src в рантайме оставался пустым.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-editable --no-dev --frozen --reinstall-package claude-bot \
 && cd / && rm -rf /src && mkdir /src

# UV_PROJECT_ENVIRONMENT has to be /usr/local for the two lines above and nothing else: the
# bot's dependencies go into the system python, it has no venv of its own. Past that point
# the variable is dangerous — at runtime `uv sync` in a working repository (sandboxes,
# /projects) writes to the same place, and the project's package shadows the bot's module:
# a package `app/` on top of `app.py` breaks `python -m app` with
# `No module named app.__main__`. It does not surface at once — the loaded module stays in
# memory and the container dies on the next restart. So it is reset here rather than in
# every sandbox: a new one inherits the safe value. Empty = uv's default, `./.venv` next
# to the repository.
#
# UV_FROZEN is likewise absent from the ENV above — the bot's build passes `--frozen` as a
# flag, whereas an inherited variable would forbid `uv lock` in a sandbox's working
# repositories ("Unable to find lockfile"), including when setting up a new project.
#
# UV_NO_CACHE is switched off here by the same logic: during the build it is right (the
# cache does not settle into an image layer, it has its own `--mount=type=cache` above),
# while at runtime it would make every `uv sync` in a sandbox download packages again. In
# sandboxes the default `/root/.cache/uv` falls inside the `${USER_DATA}/root:/root` mount
# and survives a container recreate.
#
# The value is `0`, not empty: UV_NO_CACHE is boolean and treats `''` as invalid
# (`invalid value '' for '--no-cache'`) — a sandbox build died on the very first `uv`.
# ENV cannot remove a variable from the environment, hence exactly `0`. For the string
# UV_PROJECT_ENVIRONMENT it is the other way round: empty is precisely "take the default".
ENV UV_PROJECT_ENVIRONMENT= \
    UV_NO_CACHE=0

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app"]
