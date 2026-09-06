# Environment

You run **inside a docker container** — a sandbox for a single project, not on the
user's machine. Only what is mounted exists: repositories in `/projects`, the bot's
state in `/data`. Another project means asking for a volume, not searching the
filesystem for it.

Nobody is at the keyboard: `vim`, `less`, `git rebase -i` and confirmation prompts
will hang. Non-interactive only, with explicit flags.

You are root, but the files on the host belong to the user. When you create one in
`/projects`, match `chown` to its neighbours — otherwise the user cannot edit it.

A restart wipes everything outside the volumes: `apt install` and edits under
`/usr/local` will not survive one. Anything permanent belongs in the `Dockerfile` of
the claude-bot image.

## Skills

Writing to `/root/.claude/skills` is pointless — `entrypoint` fills it with symlinks.
There are two sources: `/opt/skills/30-project/<name>/SKILL.md` for a skill specific
to this project, `/opt/skills/10-base/<name>/SKILL.md` for one useful to any sandbox.
In doubt, pick `30-project` — it overrides a skill of the same name from `10-base`.

`10-base` is published on GitHub, so internal addresses, issue keys and IPs do not
belong there. You cannot commit a skill from here — `.git` is not part of the mount.
The edit lands in the working tree on the host, and the human commits it.

## Docker — your own daemon, safe to break

`DOCKER_HOST=tcp://localhost:2375` is your own daemon (dind); the home server's
services are out of reach from here. The network namespace is shared with dind, so
ports of the containers you start show up on `localhost` as usual.

**Bind-mount trap:** paths in `docker run -v` are resolved by the daemon, not by you.
`-v /tmp/x:/x` mounts the **daemon's** `/tmp/x`. The only path that matches on both
sides is `/projects` — mounted into dind under the same path exactly for this.
