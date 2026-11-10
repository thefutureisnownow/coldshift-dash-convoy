import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import convoy_server  # noqa: E402


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        os.environ["START_KEY"] = "test-start-key"
        self.data = tempfile.mkdtemp()
        await self.start_server()

    async def start_server(self):
        self.app = convoy_server.make_app(self.data)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.data, ignore_errors=True)

    async def start(self, name="Sunday run", password=""):
        r = await self.client.post("/api/convoys", json={"name": name, "password": password, "start_key": "test-start-key"})
        self.assertEqual(r.status, 200)
        return await r.json()

    async def join(self, token, member, name, role="vehicle", car="", password=""):
        ws = await self.client.ws_connect("/ws")
        hello = {"t": "hello", "token": token, "member": member, "name": name, "role": role, "car": car, "password": password}
        await ws.send_str(json.dumps(hello))
        return ws, json.loads((await ws.receive()).data)

    async def next(self, ws, t):
        while True:
            d = json.loads((await asyncio.wait_for(ws.receive(), 2)).data)
            if d["t"] == t:
                return d

    async def test_start_join_and_see_each_other(self):
        c = await self.start()
        self.assertTrue(c["link"].endswith("/c/" + c["token"]))
        self.assertFalse(c["password"])
        self.assertNotIn("code", c)
        a, wa = await self.join(c["token"], "car-granadilla", "Granadilla", car="iVBOR")
        self.assertEqual((wa["t"], wa["name"], wa["locked"]), ("welcome", "Sunday run", False))
        b, wb = await self.join(c["token"], "car-raspberry", "Peppermint Crisp")
        self.assertEqual({m["name"] for m in wb["members"]}, {"Granadilla", "Peppermint Crisp"})
        self.assertEqual([m["car"] for m in wb["members"] if m["id"] == "car-granadilla"], ["iVBOR"])
        self.assertEqual((await self.next(a, "member"))["member"]["name"], "Peppermint Crisp")

        await a.send_str(json.dumps({"t": "pos", "lat": -34.03, "lon": 23.05, "speed": 22.2, "bearing": 270}))
        p = await self.next(b, "pos")
        self.assertEqual((p["member"], p["lat"], p["speed"]), ("car-granadilla", -34.03, 22.2))

        w, ww = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        self.assertEqual([m["pos"]["lat"] for m in ww["members"] if m["id"] == "car-granadilla"], [-34.03])
        await w.send_str(json.dumps({"t": "msg", "text": "  Fuel stop at Sedgefield  "}))
        for ws in (a, b, w):
            m = await self.next(ws, "msg")
            self.assertEqual((m["name"], m["text"]), ("Ops", "Fuel stop at Sedgefield"))

        await b.close()
        self.assertEqual((await self.next(a, "left"))["member"], "car-raspberry")
        b, wb = await self.join(c["token"], "car-raspberry", "Peppermint Crisp")
        self.assertEqual([m["text"] for m in wb["messages"]], ["Fuel stop at Sedgefield"])
        for ws in (a, b, w):
            await ws.close()

    async def test_a_password_is_needed_and_guessing_is_limited(self):
        c = await self.start(password="tart and cheesecake")
        self.assertTrue(c["password"])
        with open(os.path.join(self.data, "convoys.json")) as f:
            self.assertNotIn("tart and cheesecake", f.read())          # only its hash is kept
        _, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp")
        self.assertEqual((w["t"], w["code"]), ("error", "password"))
        _, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="guess")
        self.assertEqual((w["code"], w["error"]), ("password", "wrong password"))
        ws, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="tart and cheesecake")
        self.assertEqual((w["t"], w["locked"]), ("welcome", True))
        await ws.close()
        for i in range(convoy_server.WRONG_PER_WINDOW):
            await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="guess %d" % i)
        # the right one too is turned away for a while from here
        _, w = await self.join(c["token"], "car-raspberry", "Peppermint Crisp", password="tart and cheesecake")
        self.assertEqual(w["code"], "wait")

    async def test_convoys_outlast_a_restart_and_unknown_ones_have_ended(self):
        c = await self.start(password="pw")
        await self.client.close()
        await self.start_server()
        ws, w = await self.join(c["token"], "car-granadilla", "Granadilla", password="pw")
        self.assertEqual((w["t"], w["name"]), ("welcome", "Sunday run"))
        await ws.close()
        _, w = await self.join("x" * 24, "car-granadilla", "Granadilla")
        self.assertEqual((w["t"], w["code"]), ("error", "ended"))

    async def test_the_starter_ends_it_for_everyone(self):
        c = await self.start()
        self.assertTrue(len(c["key"]) >= 20)
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-raspberry", "Raspberry")
        url = "/api/convoys/%s/end" % c["token"]
        self.assertEqual((await self.client.post(url, json={"key": "not it"})).status, 403)
        self.assertEqual((await self.client.post(url, json={"key": c["key"]})).status, 200)
        for ws in (a, b):
            self.assertEqual((await self.next(ws, "ended"))["error"], "the convoy was ended by its starter")
        _, w = await self.join(c["token"], "car-granadilla", "Granadilla")
        self.assertEqual(w["code"], "ended")
        self.assertEqual((await self.client.post(url, json={"key": c["key"]})).status, 404)

    async def test_qr_codes(self):
        c = await self.start()
        r = await self.client.get("/c/%s/qr.png?scale=4" % c["token"])
        self.assertEqual((r.status, r.content_type), (200, "image/png"))
        self.assertEqual((await r.read())[:8], b"\x89PNG\r\n\x1a\n")
        r = await self.client.get("/c/%s/qr.svg" % c["token"])
        self.assertIn(b"<svg", await r.read())
        self.assertEqual((await self.client.get("/c/%s/qr.png" % ("y" * 24))).status, 404)

    async def test_bad_hellos_are_refused(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_str(json.dumps({"t": "msg", "text": "hi"}))
        self.assertEqual(json.loads((await ws.receive()).data)["t"], "error")
        _, w = await self.join("short", "car-1234", "A")
        self.assertEqual((w["error"], w["code"]), ("that is not a convoy link", "bad"))

    async def test_answers_localnet_probes(self):
        loop = asyncio.get_running_loop()
        got = loop.create_future()

        class Probe(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if not got.done():
                    got.set_result(json.loads(data))
        relay = convoy_server.Discovery(self.app, 8080)
        server, _ = await loop.create_datagram_endpoint(lambda: relay, local_addr=("127.0.0.1", 0))
        port = server.get_extra_info("sockname")[1]
        client, _ = await loop.create_datagram_endpoint(Probe, local_addr=("127.0.0.1", 0))
        client.sendto(json.dumps({"localnet": "probe", "game": "another game", "v": 1}).encode(), ("127.0.0.1", port))
        client.sendto(json.dumps({"localnet": "probe", "game": "coldshift.convoy", "v": 1, "sent": 5}).encode(),
                      ("127.0.0.1", port))
        reply = await asyncio.wait_for(got, 2)
        self.assertEqual((reply["localnet"], reply["game"], reply["sent"], reply["port"]), ("host", "coldshift.convoy", 5, 8080))
        self.assertEqual(reply["url"], "http://127.0.0.1:8080")
        self.assertEqual((reply["convoys"], reply["code"]), ([], False))       # nobody in a convoy yet

        # a convoy someone is in is listed, to join by tapping; an empty one is not
        locked = await self.start("Sunday run", password="tart")
        await self.start("Nobody here")
        ws, _ = await self.join(locked["token"], "car-malva", "Malva Pudding", password="tart")
        got = loop.create_future()
        client.sendto(json.dumps({"localnet": "probe", "game": "coldshift.convoy", "v": 1, "sent": 6}).encode(),
                      ("127.0.0.1", port))
        reply = await asyncio.wait_for(got, 2)
        self.assertEqual(reply["convoys"], [{"name": "Sunday run", "token": locked["token"], "locked": True, "cars": 1}])
        self.assertTrue(reply["code"])
        server.close()
        client.close()

    async def test_pages(self):
        c = await self.start()
        self.assertIn("dash.coldshift.racing", await (await self.client.get("/")).text())
        self.assertIn("<title>Convoy</title>", await (await self.client.get("/c/" + c["token"])).text())
        self.assertEqual(await (await self.client.get("/health")).text(), "ok")

    async def test_the_site_has_its_stylesheet_and_both_pages_use_it(self):
        c = await self.start()
        css = await self.client.get("/static/site.css")
        self.assertEqual((css.status, css.headers["Cache-Control"]), (200, "no-cache"))
        self.assertIn("prefers-color-scheme: dark", await css.text())
        for page in ("/", "/c/" + c["token"]):
            self.assertIn("/static/site.css", await (await self.client.get(page)).text())

    async def test_the_map_styles_are_served_as_json(self):
        for name in ("dark", "light"):
            r = await self.client.get("/static/map-%s.json" % name)
            self.assertEqual((r.status, r.content_type), (200, "application/json"))
            style = await r.json()
            self.assertEqual(style["version"], 8)
            self.assertEqual(style["sources"]["openmaptiles"]["url"], "https://tiles.openfreemap.org/planet")
            ids = [layer["id"] for layer in style["layers"]]
            self.assertEqual(len(ids), len(set(ids)))
            for layer in style["layers"]:
                self.assertTrue(layer["type"] == "background" or layer["source"] in style["sources"])

    async def test_links_use_the_public_address(self):
        os.environ["PUBLIC_URL"] = "https://convoy.example.com/"
        try:
            c = await self.start()
            self.assertEqual(c["link"], "https://convoy.example.com/c/" + c["token"])
        finally:
            del os.environ["PUBLIC_URL"]

    async def test_voice_notes(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        sound = b"\x00\x00\x00\x18ftypM4A " + b"\x01" * 2000
        await a.send_str(json.dumps({"t": "voice", "ms": 4200, "data": base64.b64encode(sound).decode()}))
        v = await self.next(b, "voice")
        self.assertEqual((v["from"], v["name"], v["ms"]), ("car-granadilla", "Granadilla", 4200))
        self.assertNotIn("data", v)
        self.assertEqual((await self.next(a, "voice"))["id"], v["id"])      # the sender hears it back too
        r = await self.client.get(v["path"])
        self.assertEqual((r.status, r.content_type, await r.read()), (200, "audio/mp4", sound))
        # it is in the history for whoever joins later
        _, w = await self.join(c["token"], "watch-1", "Mum", role="watcher")
        self.assertEqual([m["t"] for m in w["messages"]], ["voice"])

        # not a sound file, too long, too big: dropped
        for bad in ({"ms": 1000, "data": base64.b64encode(b"hello there, not audio").decode()},
                    {"ms": convoy_server.MAX_VOICE_MS + 1, "data": base64.b64encode(sound).decode()},
                    {"ms": 1000, "data": base64.b64encode(sound + b"\0" * convoy_server.MAX_VOICE).decode()},
                    {"ms": 1000, "data": "not base64!"}):
            await a.send_str(json.dumps(dict(bad, t="voice")))
        await a.send_str(json.dumps({"t": "msg", "text": "after"}))
        self.assertEqual((await self.next(b, "msg"))["text"], "after")
        self.assertEqual(len(self.app[convoy_server.RELAY].convoys[c["token"]].voices), 1)

        # only the latest are kept
        relay_c = self.app[convoy_server.RELAY].convoys[c["token"]]
        for _ in range(convoy_server.KEEP_VOICES):
            relay_c.keep_voice(sound)
        self.assertEqual((await self.client.get(v["path"])).status, 404)
        self.assertEqual((await self.client.get("/c/%s/voice/nope.m4a" % c["token"])).status, 404)

    async def test_resends_are_dropped_and_late_ones_marked(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        await a.send_str(json.dumps({"t": "msg", "text": "Fuel stop", "cid": "m1"}))
        m = await self.next(b, "msg")
        self.assertEqual(m["cid"], "m1")
        self.assertNotIn("late", m)
        self.assertEqual((await self.next(a, "msg"))["cid"], "m1")       # the sender's confirmation

        # a resend after reconnecting (its confirmation lost): only the sender hears, "ack"
        a2, w = await self.join(c["token"], "car-granadilla", "Granadilla")
        self.assertEqual([x.get("cid") for x in w["messages"]], ["m1"])
        await a2.send_str(json.dumps({"t": "msg", "text": "Fuel stop", "cid": "m1", "age": 5000}))
        self.assertEqual(await self.next(a2, "ack"), {"t": "ack", "cid": "m1"})
        # another member may use the same id: ids are per member
        await b.send_str(json.dumps({"t": "msg", "text": "Ok", "cid": "m1"}))
        self.assertEqual((await self.next(a2, "msg"))["text"], "Ok")
        self.assertEqual((await self.next(b, "msg"))["text"], "Ok")

        # one that waited 2 minutes on the phone: said 2 minutes ago, marked late
        before = convoy_server.now_ms()
        await a2.send_str(json.dumps({"t": "msg", "text": "Pull over", "cid": "m2", "age": 120000}))
        m = await self.next(b, "msg")
        self.assertEqual(m["late"], 120)
        self.assertLessEqual(m["time"], before - 119000)
        # an age beyond 15 minutes counts as 15
        await a2.send_str(json.dumps({"t": "msg", "text": "Old", "cid": "m3", "age": 10 ** 9}))
        self.assertEqual((await self.next(b, "msg"))["late"], 15 * 60)
        # an empty one is acknowledged, not sent
        await a2.send_str(json.dumps({"t": "msg", "text": "  ", "cid": "m4"}))
        self.assertEqual((await self.next(a2, "ack"))["cid"], "m4")

    async def quiet(self, ws):
        """True if no message, voice note or ack arrives on this connection in a short while (members coming and going do)."""
        while True:
            try:
                d = json.loads((await asyncio.wait_for(ws.receive(), 0.3)).data)
            except asyncio.TimeoutError:
                return True
            if d["t"] in ("msg", "voice", "ack"):
                return False

    async def test_private_messages_reach_only_the_sender_and_recipient(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        d, _ = await self.join(c["token"], "car-lemon", "Lemon Drizzle")
        b2, _ = await self.join(c["token"], "car-malva", "Malva Pudding")        # a second connection of the recipient
        await a.send_str(json.dumps({"t": "msg", "text": "Psst", "to": "car-malva", "cid": "p1"}))
        for ws in (a, b, b2):
            m = await self.next(ws, "msg")
            self.assertEqual((m["text"], m["from"], m["to"], m["to_name"], m["cid"]),
                             ("Psst", "car-granadilla", "car-malva", "Malva Pudding", "p1"))
        self.assertTrue(await self.quiet(d))
        # a shared one after it is for everyone and carries no "to"
        await a.send_str(json.dumps({"t": "msg", "text": "All"}))
        m = await self.next(d, "msg")
        self.assertEqual(m["text"], "All")
        self.assertNotIn("to", m)
        # to oneself is no private message
        await a.send_str(json.dumps({"t": "msg", "text": "Me", "to": "car-granadilla"}))
        m = await self.next(d, "msg")
        self.assertEqual((m["text"], "to" in m), ("Me", False))
        for ws in (a, b, b2, d):
            await ws.close()

    async def test_watchers_and_vehicles_message_each_other_privately(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        w, _ = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        w2, _ = await self.join(c["token"], "web-mum", "Mum", role="watcher")
        await w.send_str(json.dumps({"t": "msg", "text": "Where are you?", "to": "car-granadilla"}))
        for ws in (w, a):
            m = await self.next(ws, "msg")
            self.assertEqual((m["name"], m["text"], m["to"], m["to_name"]), ("Ops", "Where are you?", "car-granadilla", "Granadilla"))
        await a.send_str(json.dumps({"t": "msg", "text": "Sedgefield", "to": "web-ops"}))
        for ws in (a, w):
            m = await self.next(ws, "msg")
            self.assertEqual((m["text"], m["to"], m["to_name"]), ("Sedgefield", "web-ops", "Ops"))
        # watcher to watcher
        await w.send_str(json.dumps({"t": "msg", "text": "Did you see?", "to": "web-mum"}))
        self.assertEqual((await self.next(w2, "msg"))["text"], "Did you see?")
        self.assertEqual((await self.next(w, "msg"))["text"], "Did you see?")
        self.assertTrue(await self.quiet(a))
        for ws in (a, w, w2):
            await ws.close()

    async def test_the_welcome_history_holds_only_ones_you_may_see(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        d, _ = await self.join(c["token"], "car-lemon", "Lemon Drizzle")
        for who, to, text in ((a, "", "shared"), (a, "car-malva", "a to b"), (b, "car-granadilla", "b to a"),
                              (d, "car-malva", "d to b"), (d, "car-nobody", "d to nobody")):
            await who.send_str(json.dumps(dict({"t": "msg", "text": text}, **({"to": to} if to else {}))))
        for _ in range(3):                      # what the last of them sees: the shared one and its own two
            await self.next(d, "msg")
        await asyncio.sleep(0.1)

        async def history(member):
            ws, w = await self.join(c["token"], member, "Someone")
            await ws.close()
            return [m["text"] for m in w["messages"]]
        self.assertEqual(await history("car-granadilla"), ["shared", "a to b", "b to a"])
        self.assertEqual(await history("car-malva"), ["shared", "a to b", "b to a", "d to b"])
        self.assertEqual(await history("car-lemon"), ["shared", "d to b", "d to nobody"])
        self.assertEqual(await history("web-ops"), ["shared"])
        for ws in (a, b, d):
            await ws.close()

    async def test_a_private_voice_note(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        d, _ = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        sound = b"\x00\x00\x00\x18ftypM4A " + b"\x01" * 2000
        await a.send_str(json.dumps({"t": "voice", "ms": 3000, "to": "car-malva", "data": base64.b64encode(sound).decode()}))
        for ws in (a, b):
            v = await self.next(ws, "voice")
            self.assertEqual((v["to"], v["to_name"], v["ms"]), ("car-malva", "Malva Pudding", 3000))
        self.assertTrue(await self.quiet(d))
        r = await self.client.get(v["path"])
        self.assertEqual((r.status, await r.read()), (200, sound))
        _, w = await self.join(c["token"], "web-ops", "Ops", role="watcher")
        self.assertEqual(w["messages"], [])
        _, w = await self.join(c["token"], "car-malva", "Malva Pudding")
        self.assertEqual([m["t"] for m in w["messages"]], ["voice"])
        for ws in (a, b, d):
            await ws.close()

    async def test_a_private_message_to_nobody_goes_to_the_sender_only(self):
        c = await self.start()
        a, _ = await self.join(c["token"], "car-granadilla", "Granadilla")
        b, _ = await self.join(c["token"], "car-malva", "Malva Pudding")
        await a.send_str(json.dumps({"t": "msg", "text": "Hello?", "to": "car-nobody", "cid": "n1"}))
        m = await self.next(a, "msg")
        self.assertEqual((m["text"], m["to"], m["to_name"], m["cid"]), ("Hello?", "car-nobody", "", "n1"))
        self.assertTrue(await self.quiet(b))
        # a member who has been and gone is still known
        await b.close()
        await self.next(a, "left")
        await a.send_str(json.dumps({"t": "msg", "text": "You there?", "to": "car-malva"}))
        self.assertEqual((await self.next(a, "msg"))["to_name"], "Malva Pudding")
        b, w = await self.join(c["token"], "car-malva", "Malva Pudding")
        self.assertEqual([m["text"] for m in w["messages"]], ["You there?"])
        await a.close()
        await b.close()

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
        # an empty private one is acked as well
        await a2.send_str(json.dumps({"t": "msg", "text": " ", "to": "car-malva", "cid": "p2"}))
        self.assertEqual((await self.next(a2, "ack"))["cid"], "p2")
        await a2.close()
        await b.close()

    async def test_no_start_key_set_means_no_convoys_started(self):
        del os.environ["START_KEY"]
        r = await self.client.post("/api/convoys", json={"name": "x", "start_key": ""})
        self.assertEqual((r.status, (await r.json())["code"]), (403, "disabled"))
        r = await self.client.post("/api/convoys", json={"name": "x", "start_key": "anything"})
        self.assertEqual((r.status, (await r.json())["code"]), (403, "disabled"))

    async def test_a_start_needs_the_start_key(self):
        for body in ({"name": "x"}, {"name": "x", "start_key": "wrong"}):
            r = await self.client.post("/api/convoys", json=body)
            self.assertEqual((r.status, (await r.json())["code"]), (403, "start_key"))

    async def test_idle_convoys_go(self):
        c = await self.start()
        relay = self.app[convoy_server.RELAY]
        relay.convoys[c["token"]].active -= convoy_server.IDLE_HOURS * 3600 + 1
        relay.drop_idle()
        self.assertNotIn(c["token"], relay.convoys)
        with open(os.path.join(self.data, "convoys.json")) as f:
            self.assertNotIn(c["token"], f.read())


if __name__ == "__main__":
    unittest.main()
