"""Checks private messages against any relay that speaks the convoy protocol (the Python server, or a phone hosting
the convoy): joins three pretend members (two vehicles and a watcher, with fresh ids each run) to a convoy that
exists, sends messages and voice notes with "to", and prints PASS or FAIL for each part of the contract:

  - a private message reaches only its sender and its recipient (all of the recipient's connections), carrying
    "to" and "to_name", and with the sender's "cid"; a shared one still reaches everyone, with no "to";
  - a watcher can send one to a vehicle, a vehicle to a watcher, a vehicle to a vehicle;
  - a private voice note: the same, and its sound is fetched from its "path";
  - "to" naming nobody goes to the sender only, with an empty "to_name";
  - a resend with the same "cid" is acked and not delivered again;
  - after reconnecting, the welcome history holds a private message only for its sender and its recipient.

It only uses the protocol (see convoy_server.py's docstring), so it also works against a phone: start a convoy there
and give its link. The exit status is 1 if anything failed.

    .venv/bin/python tools/check_private.py http://127.0.0.1:8099 http://127.0.0.1:8099/c/<token> [--password …]
    .venv/bin/python tools/check_private.py http://192.168.0.20:47660 http://192.168.0.20:47660/c/<token>
"""
import argparse
import asyncio
import base64
import json
import secrets
import sys

import aiohttp

# a tiny MPEG-4 file: the "ftyp" box first is all a relay looks for
SOUND = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42" + bytes(range(256)) * 4
WAIT = 0.7                      # how long to wait for a message that should not come


class Peer:
    """One connection to the convoy: what it was welcomed with, and every message and ack it gets."""
    def __init__(self, name, member, role):
        self.name, self.member, self.role = name, member, role
        self.ws, self.welcome, self.frames, self.reader = None, None, [], None

    async def join(self, session, base, token, password):
        self.ws = await session.ws_connect(base.replace("http", "ws", 1) + "/ws?c=" + token, heartbeat=20)
        await self.ws.send_str(json.dumps({"t": "hello", "token": token, "password": password, "member": self.member,
                                           "name": self.name, "colour": "#2f7bff", "role": self.role}))
        first = json.loads((await asyncio.wait_for(self.ws.receive(), 10)).data)
        if first.get("t") != "welcome":
            raise RuntimeError("%s could not join: %s" % (self.name, first.get("error") or first))
        self.welcome = first
        self.reader = asyncio.create_task(self.read())
        return self

    async def read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                d = json.loads(msg.data)
                if d.get("t") in ("msg", "voice", "ack"):
                    self.frames.append(d)

    async def say(self, text=None, to=None, cid=None, age=None):
        d = {"t": "msg", "text": text}
        await self.send(d, to, cid, age)

    async def speak(self, to=None, cid=None):
        await self.send({"t": "voice", "ms": 2500, "data": base64.b64encode(SOUND).decode()}, to, cid, None)

    async def send(self, d, to, cid, age):
        for k, v in (("to", to), ("cid", cid), ("age", age)):
            if v is not None:
                d[k] = v
        await self.ws.send_str(json.dumps(d))

    def heard(self, text):
        """The messages and voice notes with this text (or, for a voice note, this cid) it has been sent."""
        return [f for f in self.frames if f["t"] in ("msg", "voice") and (f.get("text") == text or f.get("cid") == text)]

    async def close(self):
        await self.ws.close()
        self.reader.cancel()


results = []


def check(name, ok, why=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "   (%s)" % why), flush=True)


async def waits_for(peer, text, seconds=5):
    for _ in range(int(seconds / 0.05)):
        if peer.heard(text):
            return peer.heard(text)[0]
        await asyncio.sleep(0.05)
    return None


async def reaches(text, expect, others, label):
    """`expect` all hear `text`; `others` do not (after a wait)."""
    got = [await waits_for(p, text) for p in expect]
    await asyncio.sleep(WAIT)
    check("%s: reaches %s" % (label, ", ".join(p.name for p in expect)), all(got),
          "missing at " + ", ".join(p.name for p, g in zip(expect, got) if not g))
    leaked = [p.name for p in others if p.heard(text)]
    check("%s: reaches nobody else" % label, not leaked, "also reached " + ", ".join(leaked))
    return got


def shape(label, m, sender, to, to_name, cid=None):
    ok = (m and m.get("from") == sender.member and m.get("name") == sender.name and m.get("to") == to.member
          and m.get("to_name") == to_name and (cid is None or m.get("cid") == cid) and m.get("id") and m.get("time"))
    check("%s: carries from, name, to, to_name%s" % (label, ", cid" if cid else ""), bool(ok), json.dumps(m))


async def run(a):
    base = a.base.rstrip("/")
    token = a.link.rstrip("/").rsplit("/c/", 1)[-1].rsplit("/", 1)[-1]
    run_id = secrets.token_hex(3)
    async with aiohttp.ClientSession() as session:
        vs = Peer("Check Malva", "chk-red-" + run_id, "vehicle")
        vl = Peer("Check Lemon", "chk-lem-" + run_id, "vehicle")
        wo = Peer("Check Ops", "chk-ops-" + run_id, "watcher")
        try:
            for p in (vs, vl, wo):
                await p.join(session, base, token, a.password)
        except (aiohttp.ClientError, RuntimeError, asyncio.TimeoutError) as e:
            check("join the convoy with a vehicle, another vehicle and a watcher", False, str(e))
            return
        check("join the convoy with a vehicle, another vehicle and a watcher", True)
        everyone = [vs, vl, wo]

        t = "vehicle to vehicle " + run_id
        await vs.say(t, to=vl.member, cid="c1" + run_id)
        got = await reaches(t, [vs, vl], [wo], "vehicle to vehicle")
        shape("vehicle to vehicle", got[1], vs, vl, vl.name, "c1" + run_id)
        shape("vehicle to vehicle (the sender's copy)", got[0], vs, vl, vl.name, "c1" + run_id)

        t = "watcher to vehicle " + run_id
        await wo.say(t, to=vs.member)
        got = await reaches(t, [wo, vs], [vl], "watcher to vehicle")
        shape("watcher to vehicle", got[1], wo, vs, vs.name)

        t = "vehicle to watcher " + run_id
        await vl.say(t, to=wo.member)
        got = await reaches(t, [vl, wo], [vs], "vehicle to watcher")
        shape("vehicle to watcher", got[1], vl, wo, wo.name)

        t = "watcher to watcher " + run_id
        spare = Peer("Check Mum", "chk-mum-" + run_id, "watcher")
        await spare.join(session, base, token, a.password)
        await wo.say(t, to=spare.member)
        await reaches(t, [wo, spare], [vs, vl], "watcher to watcher")

        t = "shared " + run_id
        await wo.say(t)
        got = await reaches(t, everyone + [spare], [], "a shared message")
        check("a shared message has no \"to\"", all("to" not in g for g in got if g), json.dumps(got))

        # the recipient on two connections (a browser tab and a phone): both get it
        twin = Peer("Check Lemon", vl.member, "vehicle")
        await twin.join(session, base, token, a.password)
        t = "to both connections " + run_id
        await wo.say(t, to=vl.member)
        await reaches(t, [wo, vl, twin], [vs, spare], "recipient with two connections")
        await twin.close()

        # a private voice note
        vid = "v1" + run_id
        await vs.speak(to=wo.member, cid=vid)
        got = await reaches(vid, [vs, wo], [vl, spare], "voice note")
        v = got[1]
        check("voice note: carries to, to_name, ms and a path",
              bool(v and v.get("t") == "voice" and v.get("to") == wo.member and v.get("to_name") == wo.name and v.get("ms")
                   and v.get("path")), json.dumps(v))
        if v and v.get("path"):
            try:
                async with session.get(base + v["path"]) as r:
                    body = await r.read()
                check("voice note: its sound is served at its path", r.status == 200 and body == SOUND, "HTTP %d, %d bytes" % (r.status, len(body)))
            except aiohttp.ClientError as e:
                check("voice note: its sound is served at its path", False, str(e))

        # nobody
        t = "to nobody " + run_id
        await vs.say(t, to="chk-nobody-" + run_id, cid="c2" + run_id)
        got = await reaches(t, [vs], [vl, wo, spare], "unknown recipient")
        m = got[0]
        check("unknown recipient: the sender gets it back with to and an empty to_name",
              bool(m and m.get("to") == "chk-nobody-" + run_id and m.get("to_name") == "" and m.get("cid") == "c2" + run_id), json.dumps(m))

        # a resend of one the relay has had, after reconnecting
        await vs.close()
        vs = await Peer(vs.name, vs.member, "vehicle").join(session, base, token, a.password)
        before = len(wo.frames) + len(vl.frames)
        await vs.say("vehicle to vehicle " + run_id, to=vl.member, cid="c1" + run_id, age=4000)
        acked = None
        for _ in range(100):
            acked = [f for f in vs.frames if f["t"] == "ack" and f.get("cid") == "c1" + run_id]
            if acked:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(WAIT)
        check("a resend with the same cid is acked", bool(acked), "no ack")
        again = [f for p in (vs, vl, wo, spare) for f in p.frames if f.get("cid") == "c1" + run_id and f["t"] != "ack"]
        check("a resend with the same cid is not delivered again", len(again) == 1 and len(wo.frames) + len(vl.frames) == before,
              "%d copies" % len(again))

        # what the welcome holds after reconnecting
        def texts(p):
            return {m.get("text") or m.get("cid") for m in p.welcome["messages"] if m.get("t") in ("msg", "voice")}
        names = {"shared": "shared " + run_id, "v2v": "vehicle to vehicle " + run_id, "w2v": "watcher to vehicle " + run_id,
                 "v2w": "vehicle to watcher " + run_id, "w2w": "watcher to watcher " + run_id, "both": "to both connections " + run_id,
                 "voice": vid, "nobody": "to nobody " + run_id}
        for p in (vs, vl, wo, spare):
            await p.close()
        want = {
            "Check Malva (sender of some, recipient of one)": (vs.member, "vehicle",
                {"shared", "v2v", "w2v", "voice", "nobody"}, {"v2w", "w2w", "both"}),
            "Check Lemon (recipient of some)": (vl.member, "vehicle", {"shared", "v2v", "v2w", "both"},
                {"w2v", "w2w", "voice", "nobody"}),
            "Check Ops (a watcher)": (wo.member, "watcher", {"shared", "w2v", "v2w", "w2w", "both", "voice"}, {"v2v", "nobody"}),
            "Check Mum (a watcher who was sent one)": (spare.member, "watcher", {"shared", "w2w"},
                {"v2v", "w2v", "v2w", "both", "voice", "nobody"}),
        }
        for label, (member, role, yes, no) in want.items():
            p = await Peer(label.split(" (")[0], member, role).join(session, base, token, a.password)
            seen = texts(p)
            await p.close()
            missing = [k for k in yes if names[k] not in seen]
            extra = [k for k in no if names[k] in seen]
            check("welcome history after reconnecting, %s" % label, not missing and not extra,
                  "missing %s, should not hold %s" % (missing, extra))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Checks private messages against a convoy relay.")
    p.add_argument("base", help="the relay's address, e.g. http://127.0.0.1:8080 or a phone host's http://<phone>:47660")
    p.add_argument("link", help="the link of a convoy that exists there: <address>/c/<token>")
    p.add_argument("--password", default="")
    a = p.parse_args()
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        pass
    failed = results.count(False)
    print("\n%d checks, %d failed" % (len(results), failed))
    sys.exit(1 if failed or not results else 0)
