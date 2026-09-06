# Example sandbox

A working project sandbox, with the project-specific bits taken out. Copy the directory,
rename a few things, and it runs.

```bash
cp -r example ../my-project && cd ../my-project
sed -i 's/example/my-project/g' docker-compose.yml   # service names, container names, image
cp .env.example .env                                 # then fill it in
docker compose up -d --build
```

What `sed` covers: `name:`, the service prefixes, `container_name` and the image tag. Two
things it does not, so check them by hand:

- the published ssh port (`127.0.0.1:2222`) — one per sandbox, bump it for the next;
- the `Dockerfile`, which is where this project's own tooling goes.

The base image has to be built first — see the repository README.

## What is here

| | |
| --- | --- |
| `docker-compose.yml` | the bot and its dind sidecar, with the reasoning in comments |
| `Dockerfile` | a layer over `claude-bot:latest`; a commented-out example of adding a CLI |
| `.env.example` | every variable the compose file reads |
| `skills/` | this project's own skills, mounted as `20-project` |

`USER_DATA` and `CLAUDE_BOT` are absolute paths, so this directory can live anywhere —
next to the project, in an infrastructure repository, wherever the rest of that machine's
configuration is kept.
