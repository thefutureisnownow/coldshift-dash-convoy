"""What convoys there are on a relay, for its owner, and ending one: the relay's /api/admin/ with its owner key
(ADMIN_KEY), which only its owner has. Shown for each: its name, when it started and was last active, the country
it was started from, its cars and watchers (and how many are online), how many messages and voice notes it
holds, and whether it has a password. Never what is said in it; a convoy with a password needs it to be joined,
as for anyone.

    .venv/bin/python tools/convoys.py https://convoy.example.com                   # list them
    .venv/bin/python tools/convoys.py https://convoy.example.com end <token>       # end one, for everyone in it

The owner key comes from $CONVOY_ADMIN_KEY, or from ADMIN_KEY= in the file --keys names (KEY=value lines, as
~/.config/coldshift-convoy.env).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

AGENT = "coldshift-dash-convoy-tools"          # Cloudflare's browser check turns away Python's own name


def owner_key(path):
    if os.environ.get("CONVOY_ADMIN_KEY"):
        return os.environ["CONVOY_ADMIN_KEY"]
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            for line in f:
                if line.startswith("ADMIN_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise SystemExit("no owner key: set CONVOY_ADMIN_KEY, or put ADMIN_KEY=… in %s" % path)


def call(relay, key, method, path):
    req = urllib.request.Request(relay.rstrip("/") + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"Authorization": "Bearer " + key, "User-Agent": AGENT,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        if e.code == 404 and not msg:
            msg = "this relay has no owner key set (ADMIN_KEY), or is too old for this"
        raise SystemExit("%s: %s" % (relay, msg or "it said %d" % e.code))
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit("cannot reach %s (%s)" % (relay, getattr(e, "reason", e)))


def ago(ms):
    s = max(0, time.time() - ms / 1000)
    return "%d s" % s if s < 90 else "%d min" % (s / 60) if s < 5400 else "%.1f h" % (s / 3600)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("relay", help="the relay's address, e.g. https://convoy.example.com")
    p.add_argument("action", nargs="?", default="list", choices=["list", "end"])
    p.add_argument("token", nargs="?", help="the convoy to end (from the list)")
    p.add_argument("--keys", default="~/.config/coldshift-convoy.env", help="a file with ADMIN_KEY=… in it")
    p.add_argument("--json", action="store_true", help="the list as the relay gives it")
    a = p.parse_args()
    key = owner_key(a.keys)
    if a.action == "end":
        if not a.token:
            raise SystemExit("which convoy? give its token (from the list)")
        call(a.relay, key, "POST", "/api/admin/convoys/%s/end" % a.token)
        print("ended: everyone in it was told")
        return
    convoys = call(a.relay, key, "GET", "/api/admin/convoys")["convoys"]
    if a.json:
        json.dump(convoys, sys.stdout, indent=1)
        print()
        return
    if not convoys:
        print("no convoys")
        return
    print("%d convoy%s:\n" % (len(convoys), "" if len(convoys) == 1 else "s"))
    for c in convoys:
        print("%s%s" % (c["name"], "  (password)" if c["locked"] else "  (no password)"))
        print("  started %s ago%s, last active %s ago" % (ago(c["started"]), " from " + c["country"] if c["country"] else "",
                                                          ago(c["active"])))
        print("  cars %d (%d online), watchers %d (%d online), %d messages, %d voice notes" % (
            c["vehicles"], c["online_vehicles"], c["watchers"], c["online_watchers"], c["messages"], c["voice_notes"]))
        print("  %s\n" % c["token"])


if __name__ == "__main__":
    main()
