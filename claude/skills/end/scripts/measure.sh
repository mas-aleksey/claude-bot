#!/bin/sh
# Disk measurement. With no argument — prints the picture and a STATE=<free bytes> line.
# With an argument (the previous STATE) — adds a "freed" line.
# The delta is free space on /, not a sum of du: a concurrent write to the same
# filesystem skews it, but it is the only number the user actually sees.
set -u

avail_kb() { df -Pk / | awk 'NR==2 {print $4}'; }

now=$(avail_kb)

echo "== disk"
df -h /
echo
echo "== caches in /root"
du -sh /root/.npm /root/.cache/uv /root/.cache/pip 2>/dev/null
echo
echo "== repository"
du -sh .git 2>/dev/null
du -sh ./*/.venv 2>/dev/null
echo
echo "== session transcripts (top 5)"
du -sh /root/.claude/projects/*/ 2>/dev/null | sort -h | tail -5

if [ $# -ge 1 ]; then
    freed=$(( (now - $1) / 1024 ))
    echo
    echo "freed: ${freed} MB"
fi

echo
echo "STATE=$now"
