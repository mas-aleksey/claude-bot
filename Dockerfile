ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.5.27 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON=/usr/local/bin/python \
    UV_NO_CACHE=1

# node — рантайм для claude-code; docker-ce-cli + compose-plugin — host-операции через
# docker.sock (в песочницах проектов — через свой dind).
# Грабля для инстансов с ХОСТОВЫМ демоном (ассистент): относительные пути в compose
# (`./config.yaml`) резолвит демон, т.е. хост, а рабочая копия репо смонтирована здесь по
# другому пути — докер молча создаст пустой каталог вместо конфига. Значит запускать надо
# из каталога с хостовым путём (см. CLAUDE.md), а не из /projects/localhome.
# В песочницах проектов этого нет: /projects смонтирован в бота и в dind одинаково.
# git/openssh-client — работа с репозиториями проектов.
# openssh-server — вход в контейнер из IDE (PyCharm/VSCode Remote): у бота и человека
# один контейнер, значит одно рабочее дерево и одна папка сессий claude.
# jq/postgresql-client — инструменты, за которыми claude лезет чаще всего.
# vim — правки по ssh из контейнера; в slim-образе нет даже vi.
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

# Отдельным слоем и с ARG: автообновление cli живёт в /usr/lib/node_modules, тома там
# нет — значит обновляется только ребилдом. Смена версии инвалидирует лишь этот слой,
# apt-слой выше переиспользуется:
#   docker compose build --build-arg CLAUDE_CODE_VERSION=2.1.240 <service>
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

# /run/sshd — иначе sshd падает с "Missing privilege separation directory".
# sshd_config.d/ подхватывается дефолтным конфигом образа через Include.
RUN mkdir -p /run/sshd
COPY sshd_config /etc/ssh/sshd_config.d/10-container.conf
COPY entrypoint.sh /entrypoint.sh

WORKDIR /src

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=.,target=/src \
    uv sync --no-editable --no-dev --frozen

# UV_PROJECT_ENVIRONMENT нужен = /usr/local только для строки выше: зависимости бота
# ставятся в системный python, своего venv у него нет. Дальше переменная опасна — в
# рантайме `uv sync` в рабочем репозитории (песочницы, /projects) пишет туда же, и пакет
# проекта перекрывает модуль бота: пакет `app/` поверх `app.py` ломает `python -m app`
# с `No module named app.__main__`. Вылезает не сразу — загруженный модуль держится в
# памяти, контейнер падает на следующем рестарте. Поэтому сбрасываем здесь, а не в
# каждой песочнице: новая наследует безопасное значение. Пустое = дефолт uv, `./.venv`
# рядом с репозиторием. Подробности — docs/bot.md, раздел «Песочница проекта».
#
# UV_FROZEN в ENV выше тоже нет — сборка бота задаёт `--frozen` флагом, а унаследованная
# переменная запрещала бы `uv lock` в рабочих репозиториях песочницы («Unable to find
# lockfile»), в том числе при заведении нового проекта.
#
# UV_NO_CACHE сбрасываем здесь по той же логике: в сборке он к месту (кэш не оседает в
# слое образа, для него выше своя `--mount=type=cache`), а в рантайме заставлял бы каждый
# `uv sync` в песочнице качать пакеты заново. В песочницах дефолтный `/root/.cache/uv`
# попадает внутрь маунта `${USER_DATA}/root:/root` и переживает пересоздание контейнера;
# у ассистента маунтом накрыты отдельные файлы, так что его кэш живёт в rw-слое и
# теряется при пересоздании — на работу это не влияет, просто скачается заново.
ENV UV_PROJECT_ENVIRONMENT= \
    UV_NO_CACHE=

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app"]
