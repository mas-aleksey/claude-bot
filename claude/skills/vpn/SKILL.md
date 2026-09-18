---
name: vpn
description: Bring the sandbox's own VPN tunnels up, down and switch between them, and keep two tunnels from fighting over the routing table. Use on "/vpn", "start the VPN", "switch to the other VPN", "add a tunnel", "I can't reach <private address>", their Russian equivalents ("подними VPN", "переключи VPN", "добавь туннель", "не достучаться до <адрес>"), and whenever a private subnet stops answering.
allowed-tools: Bash, Read, AskUserQuestion
---

# vpn — the sandbox's own tunnels

The tunnels belong to you, not to the host. They are containers in your own docker
daemon, declared in one compose file, and you start and stop them yourself.

Check whose daemon you are talking to before anything else:

```bash
echo "${DOCKER_HOST:-host socket}"
```

`tcp://localhost:2375` — your dind, carry on. Anything else means this is not a
sandbox: stop and say so, the tunnels here are not yours to touch.

## Where everything lives

```
/projects/.vpn/docker-compose.yml   one service per tunnel
/projects/.vpn/<name>.ovpn          config, mounted into the container as /data/vpn/
/projects/.vpn/<name>-pass.txt      key passphrase, if the key is encrypted
```

The directory sits next to the work repositories, not inside one — secrets must not
end up under `git add` of a project. It is mounted from the host, so the files
survive anything you do to your docker.

`network_mode: host` in that file means the netns of dind, which is also yours: a
tunnel started there shows up in your own `ip route`. `/projects` is the only path
both you and your docker daemon see, which is why the configs live here and not
under `/data`.

## Everyday commands

```bash
C="docker compose -f /projects/.vpn/docker-compose.yml"
$C ps                    # what is up
$C up -d <tunnel>        # start one
$C stop <tunnel>         # stop it — its routes leave with the interface
$C logs --tail 30 <tunnel>
ip route                 # the result: whose subnets are reachable right now
```

You cannot change routes directly — the bot has no `NET_ADMIN` and `ip route add`
answers `Operation not permitted`. That is by design: routes arrive with a tunnel
and leave with it, so there is nothing to clean up by hand.

## The one rule: a single routing table

Every tunnel lands in the same netns, so all of them share one routing table.
Two tunnels live side by side only while:

- their subnets do not overlap, and
- at most one of them pulls a default route (`redirect-gateway`).

Break either condition and the late arrival loses. Its `ip route add` fails with
`File exists`, openvpn logs the error and keeps running — the tunnel looks healthy
and silently routes nothing. Never diagnose this from `docker ps`; read the log.

### Before starting a second tunnel

```bash
ip route                                      # subnets already claimed
grep -E '^(route|redirect-gateway)' /projects/.vpn/<new>.ovpn
$C logs <running> | grep 'ip route add'       # what the running one pulled
```

Overlap or a second default route → **ask the user which tunnel they need now**.
If they do not care or do not answer, take the default: stop the conflicting
tunnel first, then start the new one. Never start both and hope — that is the case
that looks fine and works nowhere.

### After starting

```bash
$C logs --tail 30 <tunnel> | grep -E 'Initialization Sequence Completed|File exists|ERROR'
ip route | grep <expected subnet>
```

`Initialization Sequence Completed` plus the expected subnets in `ip route` is the
proof. `File exists` means you hit the conflict above.

## Adding a tunnel

1. Put `<name>.ovpn` in `/projects/.vpn/`, `chmod 600`.
2. Copy a service block in the compose file, change `container_name` and
   `VPN_CONFIG_FILE`. Keep `KILL_SWITCH: "off"`.
3. The image takes neither a key passphrase nor pull filters through env — those go
   into the `.ovpn` itself, as its first lines:

```
askpass /data/vpn/<name>-pass.txt        # only if the key is encrypted
pull-filter ignore "redirect-gateway"    # only if the server pushes a default route
```

Without the filter a server that pushes `redirect-gateway` drags every packet you
send into its tunnel, Telegram long-poll included — you go dark and cannot fix it
from inside.

## What not to do

- `KILL_SWITCH: "on"` — its rules are namespace-wide and nailed to `tun0`. Switched
  on for one tunnel it cuts off the others and you along with them.
- Starting a tunnel outside the compose file (`docker run` with a config baked into
  an image). It works and it is invisible: nobody finds it, nothing restarts it.
- Assuming your containers inherit the tunnel. Each gets its own netns; only
  `--network host` puts one in yours.
