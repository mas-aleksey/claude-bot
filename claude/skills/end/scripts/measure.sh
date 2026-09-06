#!/bin/sh
# Замер места. Без аргумента — печатает картину и строку STATE=<байты свободно>.
# С аргументом (прошлый STATE) — добавляет строку «освобождено».
# Дельта по свободному месту на /, а не по сумме du: параллельная запись в том
# же ФС её исказит, зато это единственное число, которое видит пользователь.
set -u

avail_kb() { df -Pk / | awk 'NR==2 {print $4}'; }

now=$(avail_kb)

echo "== диск"
df -h /
echo
echo "== кеши в /root"
du -sh /root/.npm /root/.cache/uv /root/.cache/pip 2>/dev/null
echo
echo "== репозиторий"
du -sh .git 2>/dev/null
du -sh ./*/.venv 2>/dev/null
echo
echo "== транскрипты сессий (топ-5)"
du -sh /root/.claude/projects/*/ 2>/dev/null | sort -h | tail -5

if [ $# -ge 1 ]; then
    freed=$(( (now - $1) / 1024 ))
    echo
    echo "освобождено: ${freed} МБ"
fi

echo
echo "STATE=$now"
