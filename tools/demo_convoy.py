"""Fills a convoy with pretend cars so the site has something to show: four cars driving the N2 from
Knysna, each with its top-down car picture (two share a colour), a conversation (grouped
messages, a late one, a voice note), private messages (a car to the watcher with the page open, a car to another car; the
cars answer a private message to them, privately), one car dropping out and coming back. It starts a convoy on the
server unless given the link of one, prints the page's address, and keeps going until Ctrl+C.

    .venv/bin/python tools/demo_convoy.py --start-key …           # wrangler dev's http://127.0.0.1:8787
    .venv/bin/python tools/demo_convoy.py --server https://convoy.example.com --start-key … --password tart
    .venv/bin/python tools/demo_convoy.py --join http://127.0.0.1:8787/c/<token> --password …
    .venv/bin/python tools/demo_convoy.py --join … --cars raspberry,lemon     # beside real cars

A relay first: `npx wrangler dev` (the start key is START_KEY in .dev.vars).
The car pictures (car-*.png, 48 x 96 like the apps send: two more cars, the built-in look, a recoloured
copy) and the voice note (test-voice.m4a) are beside it. A watcher, as the page itself is, has no car, so it shows initials.
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import time

import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sim_car import ROUTE, bearing, dist  # noqa: E402


def along(metres):
    """(lat, lon, bearing) this far along the route, stopping at its end."""
    i = 0
    while i < len(ROUTE) - 2 and metres > dist(ROUTE[i], ROUTE[i + 1]):
        metres -= dist(ROUTE[i], ROUTE[i + 1])
        i += 1
    seg = dist(ROUTE[i], ROUTE[i + 1])
    f = max(0.0, min(1.0, metres / seg))
    return (ROUTE[i][0] + f * (ROUTE[i + 1][0] - ROUTE[i][0]), ROUTE[i][1] + f * (ROUTE[i + 1][1] - ROUTE[i][1]),
            bearing(ROUTE[i], ROUTE[i + 1]))


CARS = [
    # name, colour, top-down picture file | None, speed km/h, metres behind the front car, drops out
    dict(id="demo-granadilla", name="Granadilla", colour="#2f7bff", car="car-granadilla.png", kmh=62, back=0),
    dict(id="demo-malva", name="Malva Pudding", colour="#d6336c", car="car-malva.png", kmh=58, back=450),
    dict(id="demo-raspberry", name="Peppermint Crisp", colour="#2f7bff", car="car-builtin.png", kmh=55, back=1100, drops=True),
    dict(id="demo-lemon", name="Lemon Drizzle", colour="#f2a900", car="car-lemon.png", kmh=60, back=1900),
]


def b64(b):
    return base64.b64encode(b).decode()


async def drive(session, server, token, password, c, voice):
    car = b64(open(os.path.join(HERE, c["car"]), "rb").read()) if c["car"] else ""
    start = time.time()
    online_until = start + 45 if c.get("drops") else None
    away = 25
    v = c["kmh"] / 3.6
    s = 3000 - c["back"]                       # all of them already on the road
    done = set()                               # the SCRIPT lines this car has said
    watcher = [""]
    while True:
        try:
            async with session.ws_connect(server.replace("http", "ws", 1) + "/ws?c=" + token) as ws:
                hello = {"t": "hello", "token": token, "password": password, "member": c["id"], "name": c["name"],
                         "colour": c["colour"], "role": "vehicle", "car": car}
                await ws.send_str(json.dumps(hello))
                print("%s joined" % c["name"], flush=True)

                async def drain():
                    # who the page's watcher is (the cars message it, once it has joined), and private messages to answer
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        d = json.loads(msg.data)
                        if d["t"] == "welcome":
                            live = [m["id"] for m in d["members"] if m["role"] == "watcher" and m["online"]]
                            watcher[0] = live[-1] if live else ""
                        elif d["t"] == "member" and d["member"]["role"] == "watcher" and d["member"]["online"]:
                            watcher[0] = d["member"]["id"]
                        elif d["t"] == "left" and d["member"] == watcher[0]:
                            watcher[0] = ""
                        elif d["t"] == "msg" and d.get("to") == c["id"] and d["from"] != c["id"]:
                            await asyncio.sleep(2)
                            await ws.send_str(json.dumps({"t": "msg", "to": d["from"], "text": "%s here: got your private message" % c["name"]}))
                reader = asyncio.create_task(drain())
                while not reader.done():
                    if online_until and time.time() > online_until:
                        print("%s drops out" % c["name"], flush=True)
                        await ws.close()
                        online_until = None
                        await asyncio.sleep(away)
                        break
                    if s >= 0:
                        lat, lon, b = along(s)
                        await ws.send_str(json.dumps({"t": "pos", "lat": lat, "lon": lon, "speed": v, "bearing": b,
                                                      "time": int(time.time() * 1000)}))
                    s += v * 2
                    for n, (after, who, kind, body, *rest) in enumerate(SCRIPT):
                        if who == c["id"] and n not in done and time.time() - start > after:
                            to = rest[0] if rest else ""
                            if to == "@watcher":
                                to = watcher[0]
                                if not to:
                                    continue                # nobody is watching yet: say it when someone is
                            done.add(n)
                            extra = {"to": to} if to else {}
                            if kind == "msg":
                                await ws.send_str(json.dumps(dict(extra, t="msg", text=body)))
                            elif kind == "late":
                                await ws.send_str(json.dumps(dict(extra, t="msg", text=body, age=95000, cid="late-%d" % n)))
                            else:
                                await ws.send_str(json.dumps(dict(extra, t="voice", ms=2400, data=voice)))
                    await asyncio.sleep(2)
                reader.cancel()
        except aiohttp.ClientError as e:
            print("%s: %s" % (c["name"], e), flush=True)
        await asyncio.sleep(2)


# seconds after the start, who, what, text, and to (a member's id, or "@watcher" for whoever has the page open): groups of
# several in a row, a late one, a voice note, a long one, private ones
SCRIPT = [
    (4, "demo-granadilla", "msg", "Morning all, heading out of Knysna now"),
    (6, "demo-granadilla", "msg", "Fuel at the Sedgefield garage, then coffee at the lagoon?"),
    (9, "demo-malva", "msg", "Sounds good, right behind you"),
    (11, "demo-malva", "msg", "Map: https://www.openstreetmap.org/#map=12/-34.0344/23.0530"),
    (13, "demo-granadilla", "msg", "Hello from the lead car. This one is only for you, it is not in the shared messages.", "@watcher"),
    (14, "demo-raspberry", "late", "Sorry, no signal in the Phantom Pass tunnel, this is from ten minutes ago"),
    (16, "demo-malva", "msg", "Granadilla, between us: coffee is on you today", "demo-granadilla"),
    (17, "demo-lemon", "voice", ""),
    (19, "demo-granadilla", "msg", "Ha, only if you find parking first", "demo-malva"),
    (21, "demo-granadilla", "msg", "Heard you Lemon. Keep it under 100 through the speed trap."),
    (24, "demo-lemon", "msg", "Ha, on 60. You lot are the ones going to be late for the coffee, a long message to try the wrapping of a "
                              "bubble on a narrow phone screen as well as a wide one."),
    (27, "demo-lemon", "voice", "", "@watcher"),
    (32, "demo-raspberry", "msg", "Back with you, through the pass now"),
    (36, "demo-lemon", "msg", "Lagoon car park is quiet, plenty of space"),
]


async def main(a):
    voice = b64(open(os.path.join(HERE, "test-voice.m4a"), "rb").read())
    async with aiohttp.ClientSession() as session:
        if a.join:
            server, token = a.join.rstrip("/").rsplit("/c/", 1)
        else:
            server = a.server.rstrip("/")
            async with session.post(server + "/api/convoys", json=dict({"name": a.convoy, "password": a.password},
                                                                       **({"start_key": a.start_key} if a.start_key else {}))) as r:
                made = await r.json()
                if r.status != 200:
                    raise SystemExit("could not start a convoy: %s" % made.get("error"))
            token = made["token"]
            print("key (to end it): %s" % made["key"], flush=True)
        print("\nOpen: %s/c/%s\n" % (server, token), flush=True)
        cars = [c for c in CARS if not a.cars or c["id"].split("-", 1)[1] in a.cars.split(",")]
        await asyncio.gather(*[drive(session, server, token, a.password, c, voice) for c in cars])


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="http://127.0.0.1:8787")
    p.add_argument("--join", help="the link of a convoy that exists: <server>/c/<token> (else one is started)")
    p.add_argument("--password", default="")
    p.add_argument("--start-key", default="", help="the relay's start key, if it has one")
    p.add_argument("--convoy", default="Sunday run")
    p.add_argument("--cars", default="", help="only these, e.g. raspberry,lemon (when real phones are the other two)")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
