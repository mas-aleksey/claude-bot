#!/bin/sh
# The shared headless browser: claude reaches it through MCP (@playwright/mcp
# --cdp-endpoint), a human watches and clicks through chrome://inspect. One window, one
# profile — a login done by hand is the same session claude continues in.
#
# Chrome serves DevTools on 127.0.0.1 only and silently ignores --remote-debugging-address.
# socat hands it out as a unix socket in /data rather than a port: a port in the instance's
# network would give every neighbour there a browser with the user's cookies, while the
# socket on the host is open to group 1000 alone. From a laptop:
#   ssh -N -L 9222:<host path of /data>/cdp.sock <user>@<host>   # then chrome://inspect
#
# Loops instead of a restart policy: the processes live next to the bot, not in a container
# of their own, so a crashed browser comes back here.
B=$(ls -d /opt/pw/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null | head -1)
[ -x "$B" ] || { echo "browser.sh: no chrome-headless-shell in /opt/pw" >&2; exit 0; }
mkdir -p /data/browser

# headless-shell introduces itself as `HeadlessChrome/…`, and some sites turn that word
# away — WhatsApp Web answers "works with Google Chrome 100+". The major is taken from the
# binary, so the agent never lags behind the engine after a rebuild.
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/$("$B" --version | grep -o '[0-9]*\.' | head -1)0.0.0 Safari/537.36"

while :; do
    socat UNIX-LISTEN:/data/cdp.sock,fork,mode=660,group=1000,unlink-early TCP:127.0.0.1:9222
    sleep 2
done &

while :; do
    # The profile lock remembers the hostname, and that changes with every recreate — Chrome
    # takes a stale lock for a live browser on another machine. There is never a second
    # browser on this profile, so clearing it is safe.
    rm -f /data/browser/Singleton*
    "$B" --no-sandbox --remote-debugging-port=9222 --user-data-dir=/data/browser \
        --user-agent="$UA" about:blank > /tmp/browser.log 2>&1
    sleep 2
done
