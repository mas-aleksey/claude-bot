#!/bin/sh
# Deletes local branches whose upstream is gone (': gone]').
# Only -d. Unmerged ones are printed as a list instead — the user decides on those.
# Reason: a squash-merge leaves a branch "unmerged" although its content is already
# in the base, and -D would irreversibly lose commits that never reached it.
set -u

deleted=''
kept=''

for b in $(git branch -vv | grep ': gone\]' | sed 's/^[*+] //' | awk '{print $1}'); do
    if [ "$b" = "$(git branch --show-current)" ]; then
        kept="$kept  $b (current branch)\n"
    elif err=$(git branch -d "$b" 2>&1); then
        deleted="$deleted  $b\n"
    else
        kept="$kept  $b ($(echo "$err" | tr '\n' ' '))\n"
    fi
done

[ -n "$deleted" ] && { echo "deleted:"; printf "$deleted"; }
[ -n "$kept" ] && { echo "KEPT — needs the user's decision:"; printf "$kept"; }
[ -z "$deleted$kept" ] && echo "no dead branches"
exit 0
