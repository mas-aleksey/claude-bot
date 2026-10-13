#!/bin/sh
# Starts sshd (if host keys are present) and hands over to the bot.
# Two processes in one container, on purpose: the human in the IDE and the bot must see
# one working tree, one docker daemon and one claude session folder. Splitting them into
# separate containers loses exactly that. Children are reaped by `init: true` in compose.
set -e

# An ssh session does not inherit the container's environment: sshd forks a shell with a
# clean one. It is passed through two ways, because neither covers everything:
#   profile.d — login shells only (the IDE's terminal);
#   SetEnv    — any session, including `ssh host cmd` (how the IDE runs builds and tests).
# PATH is in the list for a second reason on top of that: Debian's /etc/profile assigns it
# unconditionally and exports it, so a login shell discards the image's ENV PATH even when
# it does arrive. profile.d is sourced after that assignment and wins. Without both channels
# an instance whose image prepends an interpreter to PATH gives the human one version while
# the bot gets another, on one working tree. A no-op where the image adds nothing.
# Both are generated at runtime: the values are only known here, they cannot be baked into
# the image. Quotes around the value — paths sometimes contain spaces.
: > /etc/ssh/sshd_config.d/20-env.conf
: > /etc/profile.d/container-env.sh
# One SetEnv line with every pair, not one line per variable: sshd takes the FIRST
# occurrence of a keyword and ignores the rest, so a line per variable silently passed only
# the first one (`sshd -T | grep setenv` showed just DOCKER_HOST — TZ and IS_SANDBOX never
# reached an `ssh host cmd` session). profile.d has no such rule; it is a shell script.
# SetEnv handles neither quotes nor spaces in a value — these variables contain neither.
set_env=
for v in DOCKER_HOST TZ IS_SANDBOX PATH; do
    eval "val=\$$v"
    [ -n "$val" ] || continue
    echo "export $v=\"$val\"" >> /etc/profile.d/container-env.sh
    set_env="$set_env $v=$val"
done
[ -n "$set_env" ] && echo "SetEnv$set_env" >> /etc/ssh/sshd_config.d/20-env.conf

# gitconfig comes from .env rather than a separate file in user_data: a work project has
# its own email address, and that is as much an instance parameter as the bot's token. As a
# file it would mean remembering one more step when setting up a sandbox, and a forgotten
# step only surfaces on the first commit.
# An existing config is left alone: it may have picked up aliases added by hand.
if [ -n "$GIT_USER_EMAIL" ] && [ ! -f /root/.gitconfig ]; then
    git config --global user.name "${GIT_USER_NAME:-$GIT_USER_EMAIL}"
    git config --global user.email "$GIT_USER_EMAIL"
    # The tree belongs to the host uid while git in the container runs as root: without
    # this, "detected dubious ownership" on any command in /projects.
    git config --global safe.directory '*'
fi

# umask 002 — because of the rw skill mounts: the process runs as root, while the folders
# in the repository on the host belong to the human (uid 1000) and are marked setgid, so
# new files inherit the right group. Only group-write was missing: with the default 022 the
# bot created `-rw-r--r-- root:<group>`, and the human on the host could neither edit nor
# delete such a skill without sudo (verified — `rm` failed with Permission denied).
umask 002

# Skills: claude-code reads only /root/.claude/skills, while there are several sources —
# this repository's base set and the instance's own (project, personal). Mounting a folder
# over a folder would hide all but the last, so the sources are mounted at /opt/skills/*
# (rw — claude edits skills itself) and linked in here one skill at a time.
#
# The order is alphabetical by directory name, and each next source overrides a skill of
# the same name from the previous one. Hence the numbered mount points (10-base,
# 20-project): weakest source first, the instance's own last. Without the numbers the word
# itself would decide the order, and the repository's set could win — exactly backwards.
#
# The sources are deliberately not listed here: adding one more means adding a volume, with
# no edit to this file.
#
# No /opt/skills — the skills are mounted directly, nothing to do.
if [ -d /opt/skills ]; then
    mkdir -p /root/.claude/skills
    # Only symlinks are cleared: a real folder may have been put there by hand.
    find /root/.claude/skills -maxdepth 1 -type l -delete
    for src in /opt/skills/*/; do
        [ -d "$src" ] || continue
        for skill in "$src"*/; do
            [ -d "$skill" ] || continue
            ln -sfn "${skill%/}" "/root/.claude/skills/$(basename "$skill")"
        done
    done
fi

# The answer style lives in an output style, not in CLAUDE.md: it belongs in the system
# prompt, while CLAUDE.md is context. runner.py passes --settings /opt/claude/settings.json,
# which selects it by name — and the name is resolved against /root/.claude/output-styles,
# so the directory baked into the image is linked in. A symlink per file, not a mount of the
# directory: /root/.claude is a mount from the host and holds credentials and sessions.
#
# That leaves CLAUDE.md with a single source — the instance's own environment description,
# mounted straight at /root/.claude/CLAUDE.md. Nothing to assemble, and Claude editing the
# file from inside keeps the edit: no startup step overwrites it any more.
# Sweep before linking: /root/.claude is a mount from the host and survives a container
# recreate, so a link to a renamed or deleted style would hang around forever. Only links
# into the image's own directory are removed — styles the user added stay untouched.
mkdir -p /root/.claude/output-styles
for link in /root/.claude/output-styles/*.md; do
    [ -L "$link" ] || continue
    case "$(readlink "$link")" in /opt/claude/output-styles/*) rm -f "$link" ;; esac
done
for style in /opt/claude/output-styles/*.md; do
    [ -f "$style" ] || continue
    ln -sf "$style" "/root/.claude/output-styles/$(basename "$style")"
done

# Host keys arrive by mount (so they survive a container recreate). No mount — sshd is not
# started: quietly working without logins beats generating keys that change on the next
# start and alarm the IDE.
if [ -r /etc/ssh/keys/ssh_host_ed25519_key ]; then
    /usr/sbin/sshd -e
else
    echo "entrypoint: /etc/ssh/keys is not mounted — sshd not started" >&2
fi

# The browser terminal. Traefik routes /term of the same host here, so the page embeds it
# without a second login; -b tells ttyd to build its own urls from that prefix. No binary —
# the panel simply has no terminal.
#
# dtach, not tmux (since 2026-10-13). Both keep the shell alive across a page reload, which
# is the only reason either is here: ttyd spawns its command per connection, so without one
# F5 would kill whatever was running. The difference is what else they do.
#
# tmux draws its own screen, in the terminal's alternate buffer. That buffer has no
# scrollback of its own, so xterm.js has nothing to scroll and turns the wheel into arrow
# keys — in bash that is readline walking previous commands, which reads as "scrolling
# broke". Getting the wheel back means `set -g mouse on`, and then tmux takes the drag too:
# the browser never sees it, selection stops working, and OSC 52 — the way tmux could hand
# a copied selection back — does not reach Safari. Three days of flipping that switch
# (2026-10-01, 10-13) ended in a `C-b m` toggle nobody wants to press.
#
# dtach draws nothing. It holds the pty and passes bytes through, so the shell writes
# straight into xterm.js: its own scrollback, its own wheel, and a drag that stays the
# browser's. Nothing to toggle. What it cannot do is windows and panes — `C-b c` and split
# screens are gone, a second terminal is a second terminal.
#
#   -A   attach, or create the session if the socket has no master. One socket = one shell.
#   -r winch  redraw on attach by sending SIGWINCH; without it a reattached screen stays
#        blank until the first keystroke, because dtach keeps no screen of its own to repaint.
#   -E   no detach character. `C-\` otherwise detaches and leaves an empty terminal that
#        looks like a crash; closing the tab is the only detach anyone needs here.
#
# scrollback=20000 matches what tmux kept, and now it is xterm.js holding it.
#
# macOptionClickForcesSelection stays, even though the shell no longer asks for mouse
# reporting: full-screen programs still do. Inside vim, htop or claude's own TUI the drag
# goes to the program again, and on macOS xterm.js hands it back only under Option, and only
# with this flag — its rule is platform-split:
#
#   shouldForceSelection(e) { return isMac ? e.altKey && <this option> : e.shiftKey }
#
# So Shift — the advice everyone repeats — does nothing on macOS.
if command -v ttyd >/dev/null 2>&1; then
    ttyd -W -p 7681 -b /term -w /projects -d 3 \
        -t macOptionClickForcesSelection=true \
        -t scrollback=20000 \
        dtach -A /tmp/ttyd.dtach -r winch -E bash -l &
else
    echo "entrypoint: ttyd not found — web terminal not started" >&2
fi

exec "$@"
