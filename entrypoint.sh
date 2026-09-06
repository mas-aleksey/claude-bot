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
# Both are generated at runtime: the values are only known here, they cannot be baked into
# the image. Quotes around the value — paths sometimes contain spaces.
: > /etc/ssh/sshd_config.d/20-env.conf
: > /etc/profile.d/container-env.sh
for v in DOCKER_HOST TZ IS_SANDBOX; do
    eval "val=\$$v"
    [ -n "$val" ] || continue
    echo "export $v=\"$val\"" >> /etc/profile.d/container-env.sh
    # SetEnv handles neither quotes nor spaces in a value — these variables contain neither.
    echo "SetEnv $v=$val" >> /etc/ssh/sshd_config.d/20-env.conf
done

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
mkdir -p /root/.claude/output-styles
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

exec "$@"
