---
name: end
description: Уборка рабочего окружения — свежий dev, мёртвые ветки и worktree, кеши сборки, docker, кеши пакетных менеджеров. Используй при "/end", "почисти", "убери мусор", "освободи место", "переключись на dev и почисти", а также когда кончается место на диске.
---

# end — уборка окружения

Три уровня. Первый безопасен всегда, второй — по умолчанию, третий — только
по явной просьбе или когда реально кончается место.

**Правило:** сначала измерить, потом удалять. Отчёт — таблицей «что / сколько
освободили». Ничего не удалять молча из того, что нельзя пересоздать командой.

## 0. Измерить

```bash
df -h /
du -sh /root/.npm /root/.cache/uv /root/.cache/pip 2>/dev/null
du -sh .git */.venv 2>/dev/null
DOCKER_HOST=tcp://localhost:2375 docker system df
du -sh /root/.claude/projects/*/ | sort -h | tail -5
```

## 1. Git — свежая база (безопасно)

```bash
git worktree prune
git fetch origin --prune
git switch dev && git pull --ff-only origin dev
```

Дерево грязное — **не** переключаться, сказать пользователю и остановиться.

Мёртвые ветки, у которых upstream удалён:

```bash
for b in $(git branch -vv | grep ': gone]' | awk '{print $1}'); do git branch -d "$b"; done
```

Только `-d`. Что не влилось — вывести списком и **спросить**, а не `-D`.
Squash-merge оставляет ветку «не влитой», хотя её содержимое уже в dev.

Проверить и показать, не удаляя:

```bash
git stash list          # старые заначки часто висят на удалённых ветках
git count-objects -v    # packs > 10 или prune-packable > 0 → git gc
```

## 2. Мусор сборки (по умолчанию)

Кеши Python в репозитории — исключать `.venv`, иначе снесёшь установленный пакет:

```bash
find . -path '*/.venv' -prune -o \
  \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) \
  -print0 | xargs -0 rm -rf
find . -path '*/.venv' -prune -o -name '*.pyc' -delete
```

Docker (свой dind, ломать безопасно):

```bash
export DOCKER_HOST=tcp://localhost:2375
docker ps -a                       # посмотреть, что удаляем
docker system prune -af --volumes
```

Тома с данными стендов и миграций уходят вместе с `--volumes`. Если в
контейнере лежит незакоммиченный результат прогона — сначала спросить.

Что **не** трогать: `.venv`, `.env`, `.run-env`, `*.egg-info` (на него
ссылается editable-установка), `node_modules` без просьбы переустановить.

## 3. Кеши пакетных менеджеров (растут молча, гигабайтами)

`/root` — том, кеши переживают рестарт контейнера и не чистятся сами.

```bash
npm cache clean --force        # /root/.npm легко набирает 1 ГБ
pip cache purge
git gc --prune=now             # packs, если count-objects показал много
env -u UV_NO_CACHE uv cache prune --cache-dir /root/.cache/uv
```

Грабля uv: в окружении стоит `UV_NO_CACHE=1`, поэтому голый `uv cache prune`
чистит временный каталог в `/tmp` и врёт «no unused entries», а настоящий кеш
в `/root/.cache/uv` не трогает. Нужны оба флага.

Остаток кеша uv после prune — это `archive-v0` и `git-v0`, распакованные
колёса и git-зависимости под живыми venv. Сносить целиком только если место
кончается: цена — переустановка окружения с нуля.

Транскрипты сессий, `/root/.claude/projects/<проект>/` — сотни мегабайт.
Это история переписки и `tool-results`. Удалять **только** по явной просьбе и
только старше двух недель:

```bash
find /root/.claude/projects -name '*.jsonl' -mtime +14 -delete
find /root/.claude/projects -path '*/tool-results/*' -mtime +14 -delete
```

## Отчёт

Таблица: что почищено, сколько освободилось. Отдельным списком — что оставил
и почему. Найденные по дороге аномалии (мусор в индексе git, чужие worktree,
осиротевший stash) — отдельным пунктом, с предложением, а не молча.
