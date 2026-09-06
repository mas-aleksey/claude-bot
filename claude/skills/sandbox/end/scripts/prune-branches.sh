#!/bin/sh
# Удаляет локальные ветки, у которых upstream удалён (': gone]').
# Только -d. Невлитые не удаляет, а печатает списком — по ним решает пользователь.
# Причина: squash-merge оставляет ветку «не влитой», хотя содержимое уже в базе,
# и -D тут безвозвратно потеряет коммиты, которые в базу не попали.
set -u

deleted=''
kept=''

for b in $(git branch -vv | grep ': gone\]' | sed 's/^[*+] //' | awk '{print $1}'); do
    if [ "$b" = "$(git branch --show-current)" ]; then
        kept="$kept  $b (текущая ветка)\n"
    elif err=$(git branch -d "$b" 2>&1); then
        deleted="$deleted  $b\n"
    else
        kept="$kept  $b ($(echo "$err" | tr '\n' ' '))\n"
    fi
done

[ -n "$deleted" ] && { echo "удалены:"; printf "$deleted"; }
[ -n "$kept" ] && { echo "ОСТАВЛЕНЫ — нужно решение пользователя:"; printf "$kept"; }
[ -z "$deleted$kept" ] && echo "мёртвых веток нет"
exit 0
