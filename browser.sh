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
B=$(ls -d /opt/pw/chromium-*/chrome-linux64/chrome 2>/dev/null | head -1)
[ -x "$B" ] || { echo "browser.sh: no chromium in /opt/pw" >&2; exit 0; }
mkdir -p /data/browser

# Headless chromium introduces itself as `HeadlessChrome/…`, and some sites turn that word
# away — WhatsApp Web answers "works with Google Chrome 100+". The major is taken from the
# binary, so the agent never lags behind the engine after a rebuild.
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/$("$B" --version | grep -o '[0-9]*\.' | head -1)0.0.0 Safari/537.36"

# Screen and language: a bare headless window is 800x600 with en-US only, and next to a
# Portuguese IP and a Lisbon timezone that combination is rare enough for bot checks to
# notice (Temu and Yandex kept asking for captchas). --window-size sets the window only;
# screen.width stays 800 without --screen-info. BROWSER_LANG comes from the instance's
# .env (e.g. pt-PT) — the base has no business knowing where the instance lives. Headless
# sends just "<lang>,<base>;q=0.9" and navigator.languages = [<lang>] however it is set
# (flag or intl.accept_languages in Preferences, both tried) — plausible enough to stop.
SIZE="--window-size=1920,1080 --screen-info={1920x1080}"
LANG_ARG=${BROWSER_LANG:+--lang=$BROWSER_LANG}

while :; do
    socat UNIX-LISTEN:/data/cdp.sock,fork,mode=660,group=1000,unlink-early TCP:127.0.0.1:9222
    sleep 2
done &

while :; do
    # The profile lock remembers the hostname, and that changes with every recreate — Chrome
    # takes a stale lock for a live browser on another machine. There is never a second
    # browser on this profile, so clearing it is safe.
    rm -f /data/browser/Singleton*
    # AutomationControlled off — headless sets navigator.webdriver to true, the first thing a
    # bot check reads; with the feature disabled it is false (checked on this binary).
    "$B" --headless=new --no-sandbox --remote-debugging-port=9222 --user-data-dir=/data/browser \
        --disable-blink-features=AutomationControlled --user-agent="$UA" $SIZE $LANG_ARG \
        about:blank > /tmp/browser.log 2>&1
    sleep 2
done
