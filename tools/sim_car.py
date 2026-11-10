"""A pretend car for trying convoys with one phone: joins a convoy by its link (and password), drives from
Knysna towards Sedgefield on the N2 sending positions every 3 s, says hello, and prints the messages it gets.

    .venv/bin/python tools/sim_car.py http://192.168.0.10:8080/c/<token> [--password …] [--name "Malva Pudding"]
"""
import argparse
import asyncio
import base64
import json
import math
import os
import time

import aiohttp

# the N2 from Knysna's waterfront westwards, roughly
ROUTE = [(-34.0376, 23.0420), (-34.0358, 23.0300), (-34.0300, 23.0050), (-34.0376, 22.9738),
         (-34.0330, 22.9300), (-34.0220, 22.8700), (-34.0150, 22.8020), (-34.0081, 22.7469)]


def dist(p, q):
    return math.hypot(math.radians(q[1] - p[1]) * math.cos(math.radians(p[0])), math.radians(q[0] - p[0])) * 6371000


def bearing(p, q):
    return (math.degrees(math.atan2(math.radians(q[1] - p[1]) * math.cos(math.radians(p[0])),
                                    math.radians(q[0] - p[0]))) + 360) % 360


async def main(a):
    car = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-car.png"), "rb").read()
    async with aiohttp.ClientSession() as s:
        server, token = a.link.rstrip("/").rsplit("/c/", 1)
        async with s.ws_connect(server.replace("http", "ws", 1) + "/ws?c=" + token) as ws:
            await ws.send_str(json.dumps({"t": "hello", "token": token, "password": a.password,
                                          "member": "sim-" + a.name.lower().replace(" ", "-"),
                                          "name": a.name, "colour": "#d6336c", "role": "vehicle",
                                          "car": base64.b64encode(car).decode()}))

            async def read():
                async for m in ws:
                    d = json.loads(m.data)
                    if d["t"] == "welcome":
                        print("joined", d["name"], flush=True)
                    elif d["t"] == "error":
                        print("refused:", d["error"], flush=True)
                    elif d["t"] == "msg":
                        print("message from %s: %s" % (d["name"], d["text"]), flush=True)
            reader = asyncio.create_task(read())
            v, i, along, said = a.kmh / 3.6, 0, 0.0, False
            start = time.time()
            while i < len(ROUTE) - 1:
                seg = dist(ROUTE[i], ROUTE[i + 1])
                if along > seg:
                    along -= seg
                    i += 1
                    continue
                f = along / seg
                lat = ROUTE[i][0] + f * (ROUTE[i + 1][0] - ROUTE[i][0])
                lon = ROUTE[i][1] + f * (ROUTE[i + 1][1] - ROUTE[i][1])
                await ws.send_str(json.dumps({"t": "pos", "lat": lat, "lon": lon, "speed": v,
                                              "bearing": bearing(ROUTE[i], ROUTE[i + 1]), "time": int(time.time() * 1000)}))
                if not said and time.time() - start > a.say_after:
                    await ws.send_str(json.dumps({"t": "msg", "text": a.say}))
                    said = True
                along += v * 3
                await asyncio.sleep(3)
            print("arrived", flush=True)
            await asyncio.sleep(a.stay)
            reader.cancel()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("link", help="the convoy's link: <server>/c/<token>")
    p.add_argument("--password", default="")
    p.add_argument("--name", default="Malva Pudding")
    p.add_argument("--kmh", type=float, default=50)
    p.add_argument("--say", default="Malva Pudding here, right behind you")
    p.add_argument("--say-after", type=float, default=8)
    p.add_argument("--stay", type=float, default=600, help="seconds to stay connected after arriving")
    asyncio.run(main(p.parse_args()))
