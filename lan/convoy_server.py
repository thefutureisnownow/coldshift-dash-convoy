"""The convoy relay: cars driving together see each other on the map and send each other messages.

Nothing is kept about anyone. A convoy is an unguessable token (its link, /c/<token>, also as a QR code)
and, if its starter chose one, a password (kept as a scrypt hash): joining takes both. Members are random
ids with the name and car picture each sends, in memory only. Convoys themselves (token, name, password
hash, times) are saved in DATA_DIR/convoys.json so a restart keeps them, until idle for IDLE_HOURS.

HTTP
    GET  /                                  the front page: start a convoy
    POST /api/convoys    {"name"?, "password"?, "start_key"?} -> {"token", "name", "link", "password": bool, "key"}
                         a start needs the relay's start key (START_KEY): without it, or with a wrong one,
                         403 {"error", "code": "start_key"}; a relay with no start key set starts none:
                         403 {"error", "code": "disabled"}; too many starts from one address: 429 {"error", "code": "busy"}
    POST /api/convoys/<token>/end  {"key"}  ends it for everyone (the key is the starter's, given once)
    GET  /c/<token>                         the watch page (follow the convoy, send messages)
    GET  /static/<file>                     the pages' stylesheet and map styles
    GET  /c/<token>/qr.png, /qr.svg         the convoy's link as a QR code (?scale=, PNG)
    GET  /c/<token>/voice/<id>.m4a          a voice note's sound (below)
    GET  /health                            "ok"

WebSocket /ws?c=<token>, JSON text frames (the query lets a relay route the socket to its convoy before the hello;
this one does not need it, the Cloudflare one does). {"t": "ping"} is answered {"t": "pong"} at any time, before
the hello too: a client that sends nothing else for a while (a watching browser) keeps its connection known to be
alive with it. The first other frame from the client is
    {"t": "hello", "token", "password"?, "member", "name", "colour"?, "car"? (PNG, base64),
     "role": "vehicle" | "watcher"}
the server answers
    {"t": "welcome", "name", "you", "link", "locked", "members": [member...], "messages": [message...]}
or {"t": "error", "error", "code": "ended" | "password" | "wait" | "full" | "bad"} and closes. Then:
    {"t": "pos", "lat", "lon", "speed", "bearing", "time"}       client: where it is now
    {"t": "pos", "member", "lat", ...}                           server: where another member is
    {"t": "msg", "text", "cid"?, "age"?, "to"?}                  client: a message to the convoy (or one member)
    {"t": "msg", "id", "time", "from", "name", "text", "cid"?, "late"?, "to"?, "to_name"?}   server: a message (the sender's too)
    {"t": "voice", "ms", "data": AAC in MPEG-4 (.m4a), base64, "cid"?, "age"?, "to"?}   client: a voice note (push to talk)
    {"t": "voice", "id", "time", "from", "name", "ms", "path", "cid"?, "late"?, "to"?, "to_name"?}  server: a voice note, from path
    {"t": "ack", "cid"}                                          server: had that one already (or it was empty)
    {"t": "member", "member": {...}}                             server: someone joined or changed
    {"t": "left", "member": id}                                  server: someone's connection closed
    {"t": "ended", "error"}                                      server: its starter ended it; then it closes
A phone keeps what it sends until it comes back (the server sends it to everyone, the sender too) and sends
it again after reconnecting, so nothing said on a bad connection is lost: "cid" is its own id for it (a
resend the server has had answers "ack", once is enough), "age" how long it waited, in ms (up to 15 min:
its time is then when it was said, and "late", in seconds, marks one that waited 30 s or more).

A private message ("to": a member's id, vehicle or watcher alike; not for secrecy, the relay sees everything, but so as
not to clutter the shared feed) goes only to that member's connections (it may have several) and the sender's own,
carrying "to" and "to_name" (the recipient's name). "to" naming no member the convoy knows (online or not) is
delivered to the sender only, still with "to" and an empty "to_name", so it is not lost silently and not broadcast;
"to" being the sender's own id is no private message at all. Private messages and voice notes are kept in the
history like the others, but a member's welcome holds one only if that member is its "from" or its "to"; a private
voice note is served at the same path as any (its id unguessable). Without "to" nothing differs. An older server
ignores "to" and broadcasts such a message to everyone, as an older app never receives private messages for others.

A member is {"id", "name", "colour", "car", "role", "online", "pos"}. Messages and voice notes share the
history in the welcome. A voice note's sound is kept in memory with the convoy, the latest KEEP_VOICES, at
GET /c/<token>/voice/<id>.m4a (its id unguessable, like the token); an older one's path answers 404.

PUBLIC_URL (e.g. https://convoy.coldshift.racing) is the address in links and QR codes; without it, the
address the request came to (X-Forwarded-Proto and -Host from a reverse proxy count).

On a local network the server can be found without typing its address: it answers LocalNet discovery
probes (Development/localnet's format, as Coldshift Racing finds games), JSON over UDP on DISCOVERY_PORT,
47660: a port of its own, so a game host on the same laptop (LocalNet's 47650-47653) does not push it
elsewhere, and only that one, since the phones probe only it:
    probe  {"localnet": "probe", "game": "coldshift.convoy", "v": 1, "sent"}
    reply  {"localnet": "host", "game", "v", "sent", "id", "name", "host", "port", "players", "max", "state": "lobby",
            "code": any convoy locked, "convoys": [{"name", "token", "locked", "cars"}] (those someone is in),
            "url": this machine's address on that network (older apps; the phones now use the reply's source address)}
DISCOVERY=0 turns it off (a server on the internet has no use for it); DISCOVERY_URL gives the address to
reply with when the server cannot see it (in Docker), else it is this machine's on the network asked on.
"""
import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
from collections import OrderedDict, deque

import segno
import socket as sockets
from aiohttp import WSMsgType, web

IDLE_HOURS = 12
MAX_MEMBERS = 30
MAX_TEXT = 500
MAX_CAR = 64 * 1024
MAX_VOICE, MAX_VOICE_MS = 192 * 1024, 31000        # 30 s at the app's 24 kbit/s is about 90 KB
MAX_FRAME = 320 * 1024                              # a voice note in base64, with room
KEEP_VOICES = 30
KEEP_CIDS = 1000
MAX_AGE_MS, LATE_MS = 15 * 60 * 1000, 30 * 1000     # how long one may have waited on the phone; marked late after
MAX_PASSWORD = 200
KEEP_MESSAGES = 100
CREATES_PER_HOUR = 60
WRONG_PER_WINDOW, WRONG_WINDOW = 10, 600            # wrong passwords from one address in 10 minutes
DISCOVERY_GAME, DISCOVERY_VERSION, DISCOVERY_PORT = "coldshift.convoy", 1, 47660
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.environ.get("SITE_DIR") or os.path.join(os.path.dirname(HERE), "site")   # the pages, shared with the Worker


def now_ms():
    return int(time.time() * 1000)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex(), hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Convoy:
    def __init__(self, token, name, salt="", pw="", created=None, active=None, key=""):
        self.token, self.name = token, name
        self.salt, self.pw = salt, pw           # empty: no password
        self.key = key                          # the starter's key, hashed: it may end the convoy
        self.created = created or time.time()
        self.active = active or time.time()
        self.members = {}                       # id -> member dict
        self.sockets = {}                       # ws -> member id
        self.messages = deque(maxlen=KEEP_MESSAGES)
        self.voices = OrderedDict()             # voice note id -> its .m4a, the latest KEEP_VOICES
        self.cids = OrderedDict()               # (member, its id for a message) -> True, the latest KEEP_CIDS

    def seen(self, member, cid):
        return (member, cid) in self.cids

    def saw(self, member, cid):
        self.cids[(member, cid)] = True
        while len(self.cids) > KEEP_CIDS:
            self.cids.popitem(last=False)

    def keep_voice(self, data):
        vid = secrets.token_urlsafe(12)
        self.voices[vid] = data
        while len(self.voices) > KEEP_VOICES:
            self.voices.popitem(last=False)
        return vid

    def check(self, password):
        if not self.pw:
            return True
        _, h = hash_password(str(password or ""), bytes.fromhex(self.salt))
        return hmac.compare_digest(h, self.pw)

    def saved(self):
        return {"name": self.name, "salt": self.salt, "pw": self.pw, "created": self.created, "active": self.active,
                "key": self.key}

    def may_end(self, key):
        return bool(self.key) and hmac.compare_digest(hash_key(str(key or "")), self.key)

    def public(self, m):
        return {k: m.get(k) for k in ("id", "name", "colour", "role", "online", "pos", "car")}

    def history(self, member):
        """The messages `member` may be shown: the shared ones, and private ones from or to it."""
        return [m for m in self.messages if "to" not in m or member in (m["from"], m["to"])]

    async def send_all(self, data, but=None, only=None):
        """To every connection but `but`; with `only` (member ids), to just those members' connections."""
        text = json.dumps(data)
        for ws, mid in list(self.sockets.items()):
            if ws is not but and not ws.closed and (only is None or mid in only):
                try:
                    await ws.send_str(text)
                except (ConnectionError, RuntimeError):
                    pass


class Relay:
    def __init__(self, data_dir=None):
        self.file = os.path.join(data_dir, "convoys.json") if data_dir else None
        self.convoys = {}                       # token -> Convoy
        self.creates = {}                       # address -> [times]
        self.wrong = {}                         # (address, convoy) -> [times]
        if self.file and os.path.exists(self.file):
            with open(self.file, encoding="utf-8") as f:
                for token, c in json.load(f).items():
                    self.convoys[token] = Convoy(token, **c)

    def save(self):
        if not self.file:
            return
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file + ".part", "w", encoding="utf-8") as f:
            json.dump({t: c.saved() for t, c in self.convoys.items()}, f)
        os.replace(self.file + ".part", self.file)

    def make(self, name, password=""):
        """A new convoy, and its starter's key (only its hash is kept)."""
        token, key = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        salt, pw = hash_password(password) if password else ("", "")
        c = Convoy(token, (name or "Convoy").strip()[:60] or "Convoy", salt, pw, key=hash_key(key))
        self.convoys[token] = c
        self.save()
        return c, key

    def drop_idle(self):
        cut = time.time() - IDLE_HOURS * 3600
        gone = [t for t, c in self.convoys.items() if not c.sockets and c.active < cut]
        for t in gone:
            del self.convoys[t]
        self.save()

    @staticmethod
    def _limit(table, key, most, window):
        """True while `key` has had fewer than `most` in the last `window` seconds (and counts this one)."""
        t = time.time()
        times = [x for x in table.get(key, []) if x > t - window]
        if len(times) >= most:
            table[key] = times
            return False
        table[key] = times + [t]
        return True

    def allow_create(self, ip):
        return self._limit(self.creates, ip, int(os.environ.get("STARTS_PER_HOUR") or CREATES_PER_HOUR), 3600)

    def blocked(self, ip, token):
        t = time.time()
        return len([x for x in self.wrong.get((ip, token), []) if x > t - WRONG_WINDOW]) >= WRONG_PER_WINDOW

    def wrong_password(self, ip, token):
        self._limit(self.wrong, (ip, token), WRONG_PER_WINDOW, WRONG_WINDOW)


RELAY = web.AppKey("relay", Relay)


def base_url(request):
    if os.environ.get("PUBLIC_URL"):
        return os.environ["PUBLIC_URL"].rstrip("/")
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return "%s://%s" % (proto, host)


def link(request, token):
    return "%s/c/%s" % (base_url(request), token)


def remote(request):
    """The client's address. X-Forwarded-For is anyone's to set, so it counts only behind a proxy (TRUST_PROXY=1)
    that sets it: then the last address in it is the one the proxy saw (earlier ones came from the client)."""
    if os.environ.get("TRUST_PROXY") == "1" and request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[-1].strip()
    return request.remote or ""


def start_key_ok(given):
    want = os.environ.get("START_KEY", "")
    return bool(want) and hmac.compare_digest(hash_key(str(given or "")), hash_key(want))


async def create(request):
    relay = request.app[RELAY]
    try:
        body = await request.json() if request.can_read_body else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if not os.environ.get("START_KEY"):                  # locked until its owner sets a start key
        return web.json_response({"error": "this relay cannot start convoys until its owner sets a start key",
                                  "code": "disabled"}, status=403)
    if not start_key_ok(body.get("start_key")):
        return web.json_response({"error": "this host needs its start key to start a convoy" if not body.get("start_key")
                                  else "wrong start key", "code": "start_key"}, status=403)
    if not relay.allow_create(remote(request)):
        return web.json_response({"error": "too many convoys started from here; try again later", "code": "busy"},
                                 status=429)
    password = str(body.get("password") or "")[:MAX_PASSWORD]
    c, key = relay.make(str(body.get("name") or "Convoy"), password)
    return web.json_response({"token": c.token, "name": c.name, "link": link(request, c.token), "password": bool(password),
                              "key": key})


async def end(request):
    """The starter ends the convoy: everyone in it is told, and it is gone."""
    relay = request.app[RELAY]
    c = relay.convoys.get(request.match_info["token"])
    if c is None:
        return web.json_response({"error": "this convoy has ended already"}, status=404)
    try:
        body = await request.json() if request.can_read_body else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict) or not c.may_end(body.get("key")):
        return web.json_response({"error": "only the convoy's starter can end it"}, status=403)
    del relay.convoys[c.token]
    relay.save()
    await c.send_all({"t": "ended", "error": "the convoy was ended by its starter"})
    for ws in list(c.sockets):
        await ws.close()
    return web.json_response({"ended": True})


def _page(name):
    # always the server's latest page (a cached copy would talk an old protocol)
    return web.FileResponse(os.path.join(SITE, name), headers={"Cache-Control": "no-cache"})


async def front(request):
    return _page("index.html")


async def watch(request):
    return _page("watch.html")


async def qr(request):
    token, kind = request.match_info["token"], request.match_info["kind"]
    if token not in request.app[RELAY].convoys:
        raise web.HTTPNotFound(text="no such convoy")
    code = segno.make(link(request, token), error="m")
    out = io.BytesIO()
    if kind == "svg":
        code.save(out, kind="svg", scale=8, border=2, dark="#000000", light="#ffffff")
        return web.Response(body=out.getvalue(), content_type="image/svg+xml")
    try:
        scale = max(2, min(20, int(request.query.get("scale", "8"))))
    except ValueError:
        scale = 8
    code.save(out, kind="png", scale=scale, border=2, dark="#000000", light="#ffffff")
    return web.Response(body=out.getvalue(), content_type="image/png")


def voice_data(b64):
    """A voice note's sound from base64, or None: an MPEG-4 file (its "ftyp" box first) no bigger than MAX_VOICE."""
    if not isinstance(b64, str) or len(b64) > MAX_VOICE * 4 // 3 + 8:
        return None
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if 12 <= len(data) <= MAX_VOICE and data[4:8] == b"ftyp" else None


async def voice(request):
    c = request.app[RELAY].convoys.get(request.match_info["token"])
    data = c.voices.get(request.match_info["id"]) if c else None
    if data is None:
        raise web.HTTPNotFound(text="that voice note is gone")
    return web.Response(body=data, content_type="audio/mp4", headers={"Cache-Control": "private, max-age=86400"})


async def health(request):
    return web.Response(text="ok")


async def socket(request):
    relay = request.app[RELAY]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_FRAME)
    await ws.prepare(request)
    convoy, me = None, None

    async def fail(error, code):
        await ws.send_str(json.dumps({"t": "error", "error": error, "code": code}))
        await ws.close()

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            t = data.get("t")
            if t == "ping":
                await ws.send_str('{"t":"pong"}')
                continue
            if convoy is None:
                if t != "hello":
                    await fail("say hello first", "bad")
                    break
                token, mid = str(data.get("token", "")), str(data.get("member", ""))
                if not TOKEN_RE.match(token) or not ID_RE.match(mid):
                    await fail("that is not a convoy link", "bad")
                    break
                c = relay.convoys.get(token)
                if c is None:
                    await fail("this convoy has ended (or the link is wrong)", "ended")
                    break
                ip = remote(request)
                if relay.blocked(ip, token):
                    await fail("too many wrong passwords from here: wait a few minutes", "wait")
                    break
                if not c.check(data.get("password")):
                    if data.get("password"):
                        relay.wrong_password(ip, token)  # a guess (a first visit without one is not)
                    await fail("this convoy needs its password" if not data.get("password") else "wrong password",
                               "password")
                    break
                if mid not in c.members and len(c.members) >= MAX_MEMBERS:
                    await fail("the convoy is full", "full")
                    break
                convoy = c
                car = str(data.get("car") or "")
                prev = convoy.members.get(mid, {})
                me = {"id": mid, "name": str(data.get("name") or "Car")[:40], "colour": str(data.get("colour") or "")[:9],
                      "car": car if len(car) <= MAX_CAR else "", "role": "watcher" if data.get("role") == "watcher" else "vehicle",
                      "online": True, "pos": prev.get("pos")}
                convoy.members[mid] = me
                convoy.sockets[ws] = mid
                convoy.active = time.time()
                await ws.send_str(json.dumps({
                    "t": "welcome", "name": convoy.name, "you": mid, "link": link(request, token), "locked": bool(convoy.pw),
                    "members": [convoy.public(m) for m in convoy.members.values()], "messages": convoy.history(mid)}))
                await convoy.send_all({"t": "member", "member": convoy.public(me)}, but=ws)
                continue
            convoy.active = time.time()
            if t == "pos":
                try:
                    pos = {"lat": float(data["lat"]), "lon": float(data["lon"]), "speed": float(data.get("speed") or 0),
                           "bearing": float(data.get("bearing") or 0), "time": int(data.get("time") or now_ms())}
                except (KeyError, TypeError, ValueError):
                    continue
                if not (-90 <= pos["lat"] <= 90 and -180 <= pos["lon"] <= 180):
                    continue
                me["pos"] = pos
                await convoy.send_all(dict(pos, t="pos", member=me["id"]), but=ws)
            elif t in ("msg", "voice"):
                cid = str(data.get("cid") or "")[:40]
                to = str(data.get("to") or "")[:64]
                if to == me["id"]:
                    to = ""                              # to oneself is no private message
                if cid and convoy.seen(me["id"], cid):
                    await ws.send_str(json.dumps({"t": "ack", "cid": cid}))    # a resend of one it has
                    continue
                m = None
                if t == "msg":
                    text = str(data.get("text") or "").strip()[:MAX_TEXT]
                    if text:
                        m = {"t": "msg", "id": secrets.token_hex(6), "text": text}
                else:
                    try:
                        ms = int(data.get("ms") or 0)
                    except (TypeError, ValueError):
                        ms = 0
                    sound = voice_data(data.get("data"))
                    if sound and 0 < ms <= MAX_VOICE_MS:
                        vid = convoy.keep_voice(sound)
                        m = {"t": "voice", "id": vid, "ms": ms, "path": "/c/%s/voice/%s.m4a" % (convoy.token, vid)}
                if m is None:
                    if cid:
                        await ws.send_str(json.dumps({"t": "ack", "cid": cid}))  # nothing to send: not again
                    continue
                try:
                    age = max(0, min(MAX_AGE_MS, int(data.get("age") or 0)))
                except (TypeError, ValueError):
                    age = 0
                m.update({"time": now_ms() - age, "from": me["id"], "name": me["name"]})
                if age >= LATE_MS:
                    m["late"] = age // 1000
                if cid:
                    m["cid"] = cid
                    convoy.saw(me["id"], cid)
                if to:
                    m["to"], m["to_name"] = to, convoy.members[to]["name"] if to in convoy.members else ""
                convoy.messages.append(m)
                # a private one reaches only the sender and the recipient (an unknown recipient: the sender alone)
                await convoy.send_all(m, only=({me["id"], to} if to in convoy.members else {me["id"]}) if to else None)
            elif t == "leave":
                convoy.members.pop(me["id"], None)
                break
    finally:
        if convoy is not None and ws in convoy.sockets:
            mid = convoy.sockets.pop(ws)
            if mid in convoy.members and mid not in convoy.sockets.values():
                convoy.members[mid]["online"] = False
            await convoy.send_all({"t": "left", "member": mid})
    return ws


class Discovery(asyncio.DatagramProtocol):
    """Answers LocalNet probes for the convoy game with this server's address on the network asked on."""
    def __init__(self, app, port):
        self.app, self.port, self.transport = app, port, None
        self.id = secrets.token_hex(4)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            probe = json.loads(data.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(probe, dict) or probe.get("localnet") != "probe" or probe.get("game") != DISCOVERY_GAME:
            return
        here = local_address(addr[0])
        relay = self.app[RELAY]
        online = sum(1 for c in relay.convoys.values() for _ in c.sockets)
        name = os.environ.get("DISCOVERY_NAME") or sockets.gethostname().split(".")[0]
        url = os.environ.get("DISCOVERY_URL") or "http://%s:%d" % (here, self.port)
        # the convoys someone is in, to join by tapping (a password still guards a locked one)
        live = [{"name": c.name, "token": c.token, "locked": bool(c.pw),
                 "cars": len({m for m in c.sockets.values() if c.members.get(m, {}).get("role") == "vehicle"})}
                for c in relay.convoys.values() if c.sockets]
        reply = {"localnet": "host", "game": DISCOVERY_GAME, "v": DISCOVERY_VERSION, "sent": probe.get("sent", 0),
                 "id": self.id, "name": name, "host": name,
                 # the phones connect to the address the reply came from, on this port (DISCOVERY_URL's in Docker)
                 "port": int(url.rsplit(":", 1)[1]) if re.match(r"^https?://[^/]+:\d+$", url) else self.port,
                 "players": online, "max": 0, "state": "lobby", "code": any(c["locked"] for c in live),
                 "convoys": live, "url": url}
        self.transport.sendto(json.dumps(reply).encode("utf-8"), addr)


def local_address(peer):
    """This machine's address on the network that reaches `peer` (no packet is sent)."""
    s = sockets.socket(sockets.AF_INET, sockets.SOCK_DGRAM)
    try:
        s.connect((peer, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def discovery(app):
    transport = None
    if os.environ.get("DISCOVERY", "1") != "0":
        loop = asyncio.get_running_loop()
        port = int(os.environ.get("PORT", "8080"))
        try:
            transport, _ = await loop.create_datagram_endpoint(lambda: Discovery(app, port),
                                                               local_addr=("0.0.0.0", DISCOVERY_PORT))
        except OSError as e:
            print("Discovery is off: UDP %d is taken (%s); the phones cannot find this server by themselves"
                  % (DISCOVERY_PORT, e), flush=True)
    yield
    if transport:
        transport.close()


async def sweep(app):
    async def loop():
        while True:
            await asyncio.sleep(600)
            app[RELAY].drop_idle()
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    app[RELAY].save()                           # the latest activity times


async def fresh_static(request, response):
    # the site's stylesheet goes with its pages: always the server's latest
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


def make_app(data_dir=None, discover=False):
    app = web.Application(client_max_size=MAX_FRAME)
    app.on_response_prepare.append(fresh_static)
    app[RELAY] = Relay(data_dir)
    app.router.add_get("/", front)
    app.router.add_post("/api/convoys", create)
    app.router.add_post("/api/convoys/{token}/end", end)
    app.router.add_get("/c/{token}", watch)
    app.router.add_get("/c/{token}/qr.{kind:png|svg}", qr)
    app.router.add_get("/c/{token}/voice/{id}.m4a", voice)
    app.router.add_get("/health", health)
    app.router.add_get("/ws", socket)
    app.router.add_static("/static/", SITE)
    app.cleanup_ctx.append(sweep)
    if discover:
        app.cleanup_ctx.append(discovery)
    return app


if __name__ == "__main__":
    web.run_app(make_app(os.environ.get("DATA_DIR", os.path.join(HERE, "data")), discover=True),
                port=int(os.environ.get("PORT", "8080")))
