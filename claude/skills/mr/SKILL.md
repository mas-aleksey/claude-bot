---
name: mr
description: Собрать MR в GitLab — ветка с префиксом, пуш, создание через API с ревьюером, ожидание зелёного пайплайна, мерж. Хост и проект берутся из git remote, остальное — из памяти проекта. Используй при "/mr", "заведи MR", "оформи MR", "создай мердж-реквест", "влей ветку", "отправь на ревью".
---

# MR в GitLab

Процедура одна на любой репозиторий. Всё, что зависит от конкретного проекта —
хост, путь, целевая ветка, ревьюер — вычисляется из `git remote` или читается из
памяти проекта. В этом файле конкретных адресов, id и имён веток быть не должно.

## 0. Откуда что берётся

```bash
set -a && . ./.env && set +a          # $GITLAB_TOKEN, если он там
eval "$(git remote get-url origin | python3 -c '
import re, sys, urllib.parse
u = sys.stdin.read().strip()
if "://" in u:
    s = urllib.parse.urlsplit(u)
    h, p = s.hostname, s.path.lstrip("/")
else:
    h, p = re.match(r"(?:[^@]+@)?([^:]+):(.+)$", u).groups()
print("HOST=%s" % h)
print("PROJ=%s" % urllib.parse.quote(re.sub(r"\.git$", "", p), safe=""))
')"
API="https://$HOST/api/v4/projects/$PROJ"
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$API" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('path_with_namespace'),'| default:',d.get('default_branch'),'| ERR:',d.get('message'))"
```

Токен привязан к хосту. Токен от другого GitLab даёт 401 на этом — если API
отвечает 401, первым делом сверить хост в `$API` с тем, откуда токен.

Из памяти проекта читаются, а не угадываются:

| что | где искать |
|---|---|
| целевая ветка | память проекта; нет записи — `default_branch` из API выше |
| ревьюер | память проекта, по **username**, не по id |
| префикс ветки | память проекта |
| порог мержа | память проекта |
| красные локальные гейты | память проекта |

Записи нет — спросить у пользователя и после ответа завести её в память проекта.
Id пользователей и групп у каждого хоста свои, в память идёт username.

Целевую ветку экспортировать — её читают шаги 2, 3 и 6:

```bash
export BASE=<целевая ветка>
```

## 1. Имя ветки — префикс обязателен

`feature/<slug>` или `fix/<slug>`, обычно с ключом задачи:
`feature/PROJ-1819-contract-types-parity`.

На хостах с ботом-сторожем ветка с голым именем (`PROJ-1335-...`) приводит к
тому, что MR закрывается через несколько секунд после создания, без комментария
о причине, и reopen не держится. Политика префиксов — в памяти проекта.

## 2. Собрать ветку

`BASE` — целевая ветка из шага 0. Две ситуации, и они требуют разного.

**Дерево чистое и ветка уже от свежего `origin/$BASE`** — пушим напрямую:

```bash
git fetch origin && git rev-list --count HEAD..origin/$BASE   # 0 — база не отстала
git push origin HEAD:refs/heads/feature/<slug>
```

**Дерево грязное или ветка тащит чужие коммиты** — черри-пик в отдельном
worktree, иначе в MR уедут десятки чужих правок:

```bash
git fetch origin
git worktree add --detach /tmp/wt-<key> origin/$BASE
git -C /tmp/wt-<key> cherry-pick <коммит>
# прогон тестов ТАМ ЖЕ, не в основном дереве
git -C /tmp/wt-<key> push origin HEAD:refs/heads/feature/<slug>
git worktree remove /tmp/wt-<key>
```

После мержа предупредить: локальная одноимённая ветка разошлась с запушенной
(тот же diff, другая база), её пуш отобьётся non-fast-forward.

## 3. Создать MR — ревьюер сразу, не потом

Ревьюер резолвится по username в id на этом хосте:

```bash
RID=$(curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://$HOST/api/v4/users?username=<username>" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[0]['id'] if d else '')")
```

Пусто — username неверен или пользователя нет на этом хосте, останов и вопрос
пользователю. Дальше:

```bash
python3 - > /tmp/mr.json <<PY
import json, os
desc = """<описание в Markdown>"""
print(json.dumps({
  "source_branch": "feature/<slug>",
  "target_branch": os.environ["BASE"],
  "title": "<KEY> <суть одной строкой>",
  "description": desc,
  "reviewer_ids": [int(os.environ["RID"])],
  "remove_source_branch": True,
}, ensure_ascii=False))
PY

curl -s -X POST -H "PRIVATE-TOKEN: $GITLAB_TOKEN" -H "Content-Type: application/json" \
  --data @/tmp/mr.json "$API/merge_requests" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print('IID:',d.get('iid'));print('URL:',d.get('web_url'));print('STATE:',d.get('state'));print('REVIEWERS:',[r['username'] for r in d.get('reviewers',[])]);print('ERR:',d.get('message'))"
```

Ревьюера ставить при создании, а не отдельным `PUT` после.

Описание собирать через `json.dumps`: русский текст, переводы строк и кавычки
руками в JSON не экранируются без ошибок.

Что в описании полезно: таблица «было / стало» по наблюдаемому поведению,
причина дефекта со ссылкой на источник канона, таблица проверок с числами,
отдельный раздел «на что смотреть на ревью».

## 4. Проверить, что MR жив

Обязательно, и не сразу — боту нужно несколько секунд:

```bash
sleep 15
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$API/merge_requests/<iid>" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print('STATE:',d['state'],'| DRAFT:',d['draft'],'| REVIEWERS:',[r['username'] for r in d['reviewers']])"
```

`state: closed` через несколько секунд после создания — первым делом смотреть
на имя ветки, а не искать причину в диффе или в статусе задачи.

## 5. Дождаться пайплайна

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$API/merge_requests/<iid>" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);p=d.get('head_pipeline') or {};print('pipeline:',p.get('status'),'|',p.get('web_url'));print('merge_status:',d.get('detailed_merge_status'))"
```

`head_pipeline.status`: `running` → `success` либо `failed`. Типичная
длительность прогона — в памяти проекта; нет записи — опрашивать раз в 2–3
минуты и записать замер после первого MR.

Упал — открыть `web_url` пайплайна и смотреть упавшую job, а не гадать.

## 6. Мерж

```bash
curl -s -X PUT -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  --data "should_remove_source_branch=true" \
  "$API/merge_requests/<iid>/merge" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print('STATE:',d.get('state'),'| SHA:',d.get('merge_commit_sha'),'| ERR:',d.get('message'))"
```

После мержа подтянуть базу и убрать ветку:

```bash
git switch $BASE && git pull --ff-only origin $BASE
git branch -d feature/<slug>
```

## 7. Задача в трекере

Отдельная процедура, скил `jira`. Порядок переходов и id — в памяти проекта:
у каждого проекта своя схема workflow. Общее правило: комментарий с ссылкой на
MR отправлять **отдельным вызовом**, в теле перехода он молча теряется.

## Правила

- **Запись — только по явной просьбе пользователя.** Создание MR, мерж и
  переходы в трекере — необратимые снаружи действия, они видны команде.
- Целевая ветка — из памяти проекта. Не считать `default_branch` рабочей веткой
  по умолчанию: релизная ветка часто и есть дефолтная, а работа идёт в другую.
- Ревьюер ставится всегда, даже если апрув не нужен.
- Мержить только на зелёном пайплайне. Красный пайплайн не «поправим потом».
- Порог мержа (нужен ли апрув) — из памяти проекта, не угадывать.
- Своя работа живёт в своём репозитории. Сабмодули на чужие хосты — только
  `clone`/`fetch`, никаких веток, тегов и MR.
