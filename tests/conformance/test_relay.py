"""The relay's contract (PROTOCOL.md), checked against a running relay through HTTP and WebSocket only.

    CONVOY_URL=http://127.0.0.1:8787 CONVOY_START_KEY=… .venv/bin/python -m unittest discover -s tests/conformance

CONVOY_URL is the relay (`npx wrangler dev` serves one at http://127.0.0.1:8787). CONVOY_START_KEY is its start key:
the tests start their convoys with it, and check that a start without it, or with a wrong one, is refused.
Every test starts convoys of its own (about 20 in all), so a relay's limit on starts per address must allow that.
"""
import asyncio
import base64
import json
import os
import time
import unittest

import aiohttp

URL = os.environ.get("CONVOY_URL", "").rstrip("/")
START_KEY = os.environ.get("CONVOY_START_KEY", "")
SOUND = b"\x00\x00\x00\x18ftypM4A " + b"\x01" * 2000
WAIT = float(os.environ.get("CONVOY_WAIT", "5"))     # seconds to wait for a frame (a deployed relay is further away)


@unittest.skipUnless(URL and START_KEY, "set CONVOY_URL to the relay to check, and CONVOY_START_KEY to its start key")
class RelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = aiohttp.ClientSession()
        self.sockets = []

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.http.close()

    async def start(self, name="Sunday run", password=""):
        body = {"name": name, "password": password, "start_key": START_KEY}
        async with self.http.post(URL + "/api/convoys", json=body) as r:
            self.assertEqual(r.status, 200, await r.text())
            return await r.json()

    async def connect(self, token):
        ws = await self.http.ws_connect(URL.replace("http", "ws", 1) + "/ws?c=" + token)
        self.sockets.append(ws)
        return ws

    async def join(self, token, member, name, role="vehicle", car="", password=""):
        ws = await self.connect(token)
        await ws.send_str(json.dumps({"t": "hello", "token": token, "member": member, "name": name, "role": role,
                                      "car": car, "password": password}))
        return ws, await self.frame(ws)

    async def frame(self, ws):
        msg = await asyncio.wait_for(ws.receive(), WAIT)
        self.assertEqual(msg.type, aiohttp.WSMsgType.TEXT, "the connection closed: %r" % (msg,))
        return json.loads(msg.data)

    async def next(self, ws, t):
        while True:
            d = await self.frame(ws)
            if d["t"] == t:
                return d

    async def quiet(self, ws):
        """True if no message, voice note or ack arrives in a short while (members coming and going may)."""
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), 0.6)
            except asyncio.TimeoutError:
                return True
            if msg.type != aiohttp.WSMsgType.TEXT or json.loads(msg.data)["t"] in ("msg", "voice", "ack"):
                return False

    async def get(self, path):
        async with self.http.get(path if path.startswith("http") else URL + path) as r:
            return r.status, r.headers.get("Content-Type", "").split(";")[0], r.headers, await r.read()

    async def test_start_join_and_see_each_other(self):
        c = await self.start()
        self.assertTrue(c["link"].endswith("/c/" + c["token"]))
        self.assertEqual((c["name"], c["password"]), ("Sunday run", False))
        self.assertGreaterEqual(len(c["key"]), 20)
        a, wa = await self.join(c["token"], "car-granadilla", "Granadilla", car="iVBOR")
        self.assertEqual((wa["t"], wa["name"], wa["locked"], wa["you"]), ("welcome", "Sunday run", False, "car-granadilla"))
        self.assertEqual(wa["link"], c["link"])
        b, wb = await self.join(c["token"], "car-raspberry", "Peppermint Crisp")
        self.assertEqual({m["name"] for m in wb["members"]}, {"Granadilla", "Peppermint Crisp"})
        self.assertEqual([m["car"] for m in wb["members"] if m["id"] == "car-granadilla"], ["iVBOR"])
        self.assertEqual((await self.next(a, "member"))["member"]["name"], "Peppermint Crisp")

        await a.send_str(json.dumps({"t": "pos", "lat": -34.03, "lon": 23.05, "speed": 22.2, "bearing": 270}))
        p = await self.next(b, "pos")
        self.assertEqual((p["member"], p["lat"], p["speed"], p["bearing"]), ("car-granadilla", -34.03, 22.2, 270))
        await a.send_str(json.dumps({"t": "pos", "lat": 95, "lon": 23.05}))              # off the earth: dropped
        await a.send_str(json.dumps({"t": "pos", "lat": -34.04, "lon": 23.06}))
        self.assertEqual((await self.next(b, "pos"))["lat"], -34.04)

        w, ww = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        self.assertEqual([m["pos"]["lat"] for m in ww["members"] if m["id"] == "car-granadilla"], [-34.04])
        self.assertEqual([m["role"] for m in ww["members"] if m["id"] == "web-ops"], ["watcher"])
        await w.send_str(json.dumps({"t": "msg", "text": "  Fuel stop at Sedgefield  "}))
        for ws in (a, b, w):
            m = await self.next(ws, "msg")
            self.assertEqual((m["name"], m["text"], m["from"]), ("Ops", "Fuel stop at Sedgefield", "web-ops"))

        await b.close()
        left = await self.next(a, "left")
        self.assertEqual(left["member"], "car-raspberry")
        b, wb = await self.join(c["token"], "car-raspberry", "Peppermint Crisp")
        self.assertEqual([m["text"] for m in wb["messages"]], ["Fuel stop at Sedgefield"])
        # a member that left without "leave" is still known, offline until it comes back
        self.assertTrue([m["online"] for m in wb["members"] if m["id"] == "car-raspberry"][0])

    async def test_leave_forgets_the_member(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        await b.send_str(json.dumps({"t": "leave"}))
        await self.next(a, "left")
        _, w = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        self.assertNotIn("car-malva", [m["id"] for m in w["members"]])

    async def test_ping(self):
        c = await self.start()
        ws = await self.connect(c["token"])
        await ws.send_str('{"t":"ping"}')                      # before the hello too
        self.assertEqual(await self.frame(ws), {"t": "pong"})
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        await a.send_str('{"t":"ping"}')
        self.assertEqual(await self.next(a, "pong"), {"t": "pong"})

    async def test_the_start_key(self):
        for body, error in (({"name": "x"}, "this host needs its start key to start a convoy"),
                            ({"name": "x", "start_key": START_KEY + "?"}, "wrong start key")):
            async with self.http.post(URL + "/api/convoys", json=body) as r:
                self.assertEqual(r.status, 403)
                self.assertEqual(await r.json(), {"error": error, "code": "start_key"})

    async def test_a_password_is_needed_and_guessing_is_limited(self):
        c = await self.start(password="tart and cheesecake")
        self.assertTrue(c["password"])
        _, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp")
        self.assertEqual((w["t"], w["code"], w["error"]), ("error", "password", "this convoy needs its password"))
        _, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="guess")
        self.assertEqual((w["code"], w["error"]), ("password", "wrong password"))
        ws, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="tart and cheesecake")
        self.assertEqual((w["t"], w["locked"]), ("welcome", True))
        await ws.close()
        for i in range(10):
            await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="guess %d" % i)
        # the right one too is turned away for a while from here
        _, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="tart and cheesecake")
        self.assertEqual(w["code"], "wait")
        # another convoy is not
        d = await self.start(password="pw")
        _, w = await self.join(d["token"], "car-raspberry", "Peppermint Crisp", password="pw")
        self.assertEqual(w["t"], "welcome")

    async def test_unknown_convoys_have_ended(self):
        _, w = await self.join("x" * 24, "car-granadilla", "Granadilla")
        self.assertEqual((w["t"], w["code"]), ("error", "ended"))

    async def test_the_starter_ends_it_for_everyone(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-raspberry", "Raspberry")
        url = URL + "/api/convoys/%s/end" % c["token"]
        async with self.http.post(url, json={"key": "not it"}) as r:
            self.assertEqual(r.status, 403)
        async with self.http.post(url, json={"key": c["key"]}) as r:
            self.assertEqual((r.status, await r.json()), (200, {"ended": True}))
        for ws in (a, b):
            self.assertEqual((await self.next(ws, "ended"))["error"], "the convoy was ended by its starter")
            msg = await asyncio.wait_for(ws.receive(), WAIT)
            while msg.type == aiohttp.WSMsgType.TEXT:          # the others' "left" may come first
                msg = await asyncio.wait_for(ws.receive(), WAIT)
            self.assertIn(msg.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))
        _, w = await self.join(c["token"], "car-granadilla", "Granadilla")
        self.assertEqual(w["code"], "ended")
        async with self.http.post(url, json={"key": c["key"]}) as r:
            self.assertEqual(r.status, 404)

    async def test_qr_codes(self):
        c = await self.start()
        status, kind, _, body = await self.get("/c/%s/qr.png?scale=4" % c["token"])
        self.assertEqual((status, kind, body[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))
        _, _, _, big = await self.get("/c/%s/qr.png?scale=8" % c["token"])
        width = lambda png: int.from_bytes(png[16:20], "big")                   # noqa: E731 (from IHDR)
        self.assertEqual(width(big), 2 * width(body))
        self.assertEqual(width(body) % 4, 0)
        status, kind, _, body = await self.get("/c/%s/qr.svg" % c["token"])
        self.assertEqual((status, kind), (200, "image/svg+xml"))
        self.assertIn(b"<svg", body)
        self.assertEqual((await self.get("/c/%s/qr.png" % ("y" * 24)))[0], 404)

    async def test_bad_hellos_are_refused(self):
        c = await self.start()
        ws = await self.connect(c["token"])
        await ws.send_str(json.dumps({"t": "msg", "text": "hi"}))
        self.assertEqual((await self.frame(ws))["code"], "bad")
        _, w = await self.join(c["token"], "x", "A")                            # too short a member id
        self.assertEqual((w["error"], w["code"]), ("that is not a convoy link", "bad"))

    async def test_pages(self):
        c = await self.start()
        status, kind, headers, body = await self.get("/")
        self.assertEqual((status, kind, headers.get("Cache-Control")), (200, "text/html", "no-cache"))
        self.assertIn(b"dash.coldshift.racing", body)
        self.assertNotIn(b"/api/convoys", body)                    # the page starts no convoys
        status, kind, headers, body = await self.get("/c/" + c["token"])
        self.assertEqual((status, kind, headers.get("Cache-Control")), (200, "text/html", "no-cache"))
        self.assertIn(b"<title>Convoy</title>", body)
        self.assertEqual((await self.get("/health"))[3], b"ok")
        status, _, headers, css = await self.get("/static/site.css")
        self.assertEqual((status, headers.get("Cache-Control")), (200, "no-cache"))
        self.assertIn(b"prefers-color-scheme: dark", css)
        for name in ("dark", "light"):
            status, kind, _, body = await self.get("/static/map-%s.json" % name)
            self.assertEqual((status, kind, json.loads(body)["version"]), (200, "application/json", 8))

    async def test_voice_notes(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        await a.send_str(json.dumps({"t": "voice", "ms": 4200, "data": base64.b64encode(SOUND).decode()}))
        v = await self.next(b, "voice")
        self.assertEqual((v["from"], v["name"], v["ms"]), ("car-granadilla", "Granadilla", 4200))
        self.assertEqual(v["path"], "/c/%s/voice/%s.m4a" % (c["token"], v["id"]))
        self.assertNotIn("data", v)
        self.assertEqual((await self.next(a, "voice"))["id"], v["id"])           # the sender hears it back too
        status, kind, _, body = await self.get(v["path"])
        self.assertEqual((status, kind, body), (200, "audio/mp4", SOUND))
        _, w = await self.join(c["token"], "watch-1", "Mum", role="watcher")
        self.assertEqual([m["t"] for m in w["messages"]], ["voice"])

        # not a sound file, too long, too big, not base64: dropped
        for bad in ({"ms": 1000, "data": base64.b64encode(b"hello there, not audio").decode()},
                    {"ms": 31001, "data": base64.b64encode(SOUND).decode()},
                    {"ms": 1000, "data": base64.b64encode(SOUND + b"\0" * 192 * 1024).decode()},
                    {"ms": 1000, "data": "not base64!"}):
            await a.send_str(json.dumps(dict(bad, t="voice")))
        await a.send_str(json.dumps({"t": "msg", "text": "after"}))
        self.assertEqual((await self.next(b, "msg"))["text"], "after")

        # only the latest 30 are kept
        for _ in range(30):
            await a.send_str(json.dumps({"t": "voice", "ms": 1000, "data": base64.b64encode(SOUND).decode()}))
            await self.next(b, "voice")
        self.assertEqual((await self.get(v["path"]))[0], 404)
        self.assertEqual((await self.get("/c/%s/voice/nope.m4a" % c["token"]))[0], 404)

    async def test_resends_are_dropped_and_late_ones_marked(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        await a.send_str(json.dumps({"t": "msg", "text": "Fuel stop", "cid": "m1"}))
        m = await self.next(b, "msg")
        self.assertEqual(m["cid"], "m1")
        self.assertNotIn("late", m)
        self.assertEqual((await self.next(a, "msg"))["cid"], "m1")

        a2, w = await self.join(c["token"], "car-granadilla", "Granadilla")
        self.assertEqual([x.get("cid") for x in w["messages"]], ["m1"])
        await a2.send_str(json.dumps({"t": "msg", "text": "Fuel stop", "cid": "m1", "age": 5000}))
        self.assertEqual(await self.next(a2, "ack"), {"t": "ack", "cid": "m1"})
        await b.send_str(json.dumps({"t": "msg", "text": "Ok", "cid": "m1"}))     # ids are per member
        self.assertEqual((await self.next(a2, "msg"))["text"], "Ok")
        self.assertEqual((await self.next(b, "msg"))["text"], "Ok")

        before = int(time.time() * 1000)
        await a2.send_str(json.dumps({"t": "msg", "text": "Pull over", "cid": "m2", "age": 120000}))
        m = await self.next(b, "msg")
        self.assertEqual(m["late"], 120)
        self.assertLessEqual(m["time"], before - 115000)       # a little room for the two clocks
        await a2.send_str(json.dumps({"t": "msg", "text": "Old", "cid": "m3", "age": 10 ** 9}))
        self.assertEqual((await self.next(b, "msg"))["late"], 15 * 60)
        await a2.send_str(json.dumps({"t": "msg", "text": "  ", "cid": "m4"}))
        self.assertEqual((await self.next(a2, "ack"))["cid"], "m4")

    async def test_long_text_is_cut(self):
        c = await self.start(name="N" * 100)
        self.assertEqual(c["name"], "N" * 60)
        a, w = await self.join(c["token"], "car-granadilla", "B" * 100)
        self.assertEqual([m["name"] for m in w["members"]], ["B" * 40])
        await a.send_str(json.dumps({"t": "msg", "text": "x" * 900}))
        self.assertEqual(len((await self.next(a, "msg"))["text"]), 500)

    async def test_private_messages_reach_only_the_sender_and_recipient(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        d, _ = await self.join(c["token"], "car-lemon", "Lemon Drizzle")
        b2, _ = await self.join(c["token"], "car-malva", "Malva Pudding")          # a second connection of the recipient
        await a.send_str(json.dumps({"t": "msg", "text": "Psst", "to": "car-malva", "cid": "p1"}))
        for ws in (a, b, b2):
            m = await self.next(ws, "msg")
            self.assertEqual((m["text"], m["from"], m["to"], m["to_name"], m["cid"]),
                             ("Psst", "car-granadilla", "car-malva", "Malva Pudding", "p1"))
        self.assertTrue(await self.quiet(d))
        await a.send_str(json.dumps({"t": "msg", "text": "All"}))
        m = await self.next(d, "msg")
        self.assertEqual(m["text"], "All")
        self.assertNotIn("to", m)
        await a.send_str(json.dumps({"t": "msg", "text": "Me", "to": "car-granadilla"}))
        m = await self.next(d, "msg")
        self.assertEqual((m["text"], "to" in m), ("Me", False))

    async def test_watchers_and_vehicles_message_each_other_privately(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        w, _ = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        w2, _ = await self.join(c["token"], "web-mum", "Mum", role="watcher")
        await w.send_str(json.dumps({"t": "msg", "text": "Where are you?", "to": "car-granadilla"}))
        for ws in (w, a):
            m = await self.next(ws, "msg")
            self.assertEqual((m["name"], m["text"], m["to"], m["to_name"]),
                             ("Ops", "Where are you?", "car-granadilla", "Granadilla"))
        await a.send_str(json.dumps({"t": "msg", "text": "Sedgefield", "to": "web-ops"}))
        for ws in (a, w):
            m = await self.next(ws, "msg")
            self.assertEqual((m["text"], m["to"], m["to_name"]), ("Sedgefield", "web-ops", "Ops"))
        await w.send_str(json.dumps({"t": "msg", "text": "Did you see?", "to": "web-mum"}))
        self.assertEqual((await self.next(w2, "msg"))["text"], "Did you see?")
        self.assertEqual((await self.next(w, "msg"))["text"], "Did you see?")
        self.assertTrue(await self.quiet(a))

    async def test_the_welcome_history_holds_only_ones_you_may_see(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        d, _ = await self.join(c["token"], "car-lemon", "Lemon Drizzle")
        for who, to, text in ((a, "", "shared"), (a, "car-malva", "a to b"), (b, "car-granadilla", "b to a"),
                              (d, "car-malva", "d to b"), (d, "car-nobody", "d to nobody")):
            # one at a time: the order of frames on different connections is not the relay's to keep
            await who.send_str(json.dumps(dict({"t": "msg", "text": text}, **({"to": to} if to else {}))))
            while (await self.next(who, "msg"))["text"] != text:
                pass

        async def history(member):
            ws, w = await self.join(c["token"], member, "Someone")
            await ws.close()
            return [m["text"] for m in w["messages"]]
        self.assertEqual(await history("car-granadilla"), ["shared", "a to b", "b to a"])
        self.assertEqual(await history("car-malva"), ["shared", "a to b", "b to a", "d to b"])
        self.assertEqual(await history("car-lemon"), ["shared", "d to b", "d to nobody"])
        self.assertEqual(await history("web-ops"), ["shared"])

    async def test_a_private_voice_note(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        d, _ = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        await a.send_str(json.dumps({"t": "voice", "ms": 3000, "to": "car-malva",
                                     "data": base64.b64encode(SOUND).decode()}))
        for ws in (a, b):
            v = await self.next(ws, "voice")
            self.assertEqual((v["to"], v["to_name"], v["ms"]), ("car-malva", "Malva Pudding", 3000))
        self.assertTrue(await self.quiet(d))
        status, _, _, body = await self.get(v["path"])
        self.assertEqual((status, body), (200, SOUND))
        _, w = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        self.assertEqual(w["messages"], [])
        _, w = await self.join(c["token"], "car-malva", "Malva Pudding")
        self.assertEqual([m["t"] for m in w["messages"]], ["voice"])

    async def test_a_private_message_to_nobody_goes_to_the_sender_only(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        await a.send_str(json.dumps({"t": "msg", "text": "Hello?", "to": "car-nobody", "cid": "n1"}))
        m = await self.next(a, "msg")
        self.assertEqual((m["text"], m["to"], m["to_name"], m["cid"]), ("Hello?", "car-nobody", "", "n1"))
        self.assertTrue(await self.quiet(b))
        await b.close()
        await self.next(a, "left")
        await a.send_str(json.dumps({"t": "msg", "text": "You there?", "to": "car-malva"}))
        self.assertEqual((await self.next(a, "msg"))["to_name"], "Malva Pudding")
        b, w = await self.join(c["token"], "car-malva", "Malva Pudding")
        self.assertEqual([m["text"] for m in w["messages"]], ["You there?"])

    async def test_a_resent_private_message_is_acked_not_delivered_again(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        await a.send_str(json.dumps({"t": "msg", "text": "Psst", "to": "car-malva", "cid": "p1"}))
        await self.next(b, "msg")
        await a.close()
        a2, w = await self.join(c["token"], "car-granadilla", "Granadilla")
        self.assertEqual([x.get("cid") for x in w["messages"]], ["p1"])
        await a2.send_str(json.dumps({"t": "msg", "text": "Psst", "to": "car-malva", "cid": "p1", "age": 4000}))
        self.assertEqual(await self.next(a2, "ack"), {"t": "ack", "cid": "p1"})
        self.assertTrue(await self.quiet(b))
        await a2.send_str(json.dumps({"t": "msg", "text": " ", "to": "car-malva", "cid": "p2"}))
        self.assertEqual((await self.next(a2, "ack"))["cid"], "p2")


ADMIN_KEY = os.environ.get("CONVOY_ADMIN_KEY", "")


@unittest.skipUnless(URL and START_KEY and ADMIN_KEY, "set CONVOY_ADMIN_KEY too, to check the owner's /api/admin/")
class OwnerTest(RelayTest):
    """The relay's owner sees the convoys (never what is said in them) and may end one. Optional for a relay."""

    async def admin(self, method, path, key=ADMIN_KEY):
        headers = {"Authorization": "Bearer " + key} if key else {}
        async with self.http.request(method, URL + "/api/admin" + path, headers=headers,
                                     json={} if method == "POST" else None) as r:
            return r.status, await r.json()

    async def test_the_owner_key_is_needed(self):
        for key in ("", ADMIN_KEY + "?"):
            status, reply = await self.admin("GET", "/convoys", key)
            self.assertEqual((status, reply["code"]), (403, "admin_key"))

    async def test_the_owner_sees_the_convoys_and_ends_one(self):
        c = await self.start(name="Owner check", password="pw")
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla", password="pw")
        w, _ = await self.join(c["token"], "web-ops", "Ops", role="watcher", password="pw")
        await a.send_str(json.dumps({"t": "msg", "text": "secret plans"}))
        await self.next(a, "msg")
        status, reply = await self.admin("GET", "/convoys")
        self.assertEqual(status, 200)
        mine = [x for x in reply["convoys"] if x["token"] == c["token"]]
        self.assertEqual(len(mine), 1)
        x = mine[0]
        self.assertEqual((x["name"], x["locked"], x["link"]), ("Owner check", True, c["link"]))
        self.assertEqual((x["vehicles"], x["online_vehicles"], x["watchers"], x["online_watchers"]), (1, 1, 1, 1))
        self.assertEqual((x["messages"], x["voice_notes"]), (1, 0))
        self.assertNotIn("secret plans", json.dumps(reply))                 # never what is said
        self.assertLessEqual(x["started"], x["active"])

        status, reply = await self.admin("POST", "/convoys/%s/end" % c["token"])
        self.assertEqual((status, reply), (200, {"ended": True}))
        for ws in (a, w):
            self.assertEqual((await self.next(ws, "ended"))["error"], "the convoy was ended by the relay's owner")
        status, reply = await self.admin("GET", "/convoys")
        self.assertNotIn(c["token"], [x["token"] for x in reply["convoys"]])
        status, _ = await self.admin("POST", "/convoys/%s/end" % c["token"])
        self.assertEqual(status, 404)

    async def test_a_convoy_ended_by_its_starter_leaves_the_list(self):
        c = await self.start(name="Starter ends")
        async with self.http.post(URL + "/api/convoys/%s/end" % c["token"], json={"key": c["key"]}) as r:
            self.assertEqual(r.status, 200)
        _, reply = await self.admin("GET", "/convoys")
        self.assertNotIn(c["token"], [x["token"] for x in reply["convoys"]])


def load_tests(loader, tests, pattern):
    # OwnerTest adds its own tests only, not RelayTest's again
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(RelayTest))
    if URL and START_KEY and ADMIN_KEY:
        for name in loader.getTestCaseNames(OwnerTest):
            if name not in loader.getTestCaseNames(RelayTest):
                suite.addTest(OwnerTest(name))
    return suite


if __name__ == "__main__":
    unittest.main()
