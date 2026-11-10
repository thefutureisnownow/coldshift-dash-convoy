# The Convoy protocol

What a relay and its clients (the car app, the convoy page in a browser, the tools) say to each other. Every relay keeps to it: the Cloudflare Worker (`worker/`), and the relays on a local network. `tests/conformance` checks a running relay against it.

## What a relay knows

A **convoy** is an unguessable token (24 characters of URL-safe base64, from 18 random bytes): its link is `<relay>/c/<token>`, also shown as a QR code. If its starter chose one, it also has a **password**, kept only as a salted hash. Joining takes the link and the password.

**Members** are random ids (6 to 64 of `A-Z a-z 0-9 _ -`) chosen by the client, with the name, colour and car picture each sends. A member is a `vehicle` (a car) or a `watcher` (someone following along in a browser). Nothing else is known about anyone: no accounts, no addresses kept beyond the rate limits below.

A convoy ends when its starter ends it, or after a while (12 hours by default) with nobody in it. The latest 100 messages and the sound of the latest 30 voice notes are kept with it, and go with it.

## HTTP

| | |
|---|---|
| `GET /` | a page saying what this is (convoys are not started from it) |
| `POST /api/convoys` | start one: `{"name"?, "password"?, "start_key"?}` → `{"token", "name", "link", "password": bool, "key"}` |
| `POST /api/convoys/<token>/end` | end it for everyone: `{"key"}` → `{"ended": true}`; 403 with another key, 404 once it has ended |
| `GET /c/<token>` | the convoy's page (served for any token: the page says if it has ended) |
| `GET /c/<token>/qr.png?scale=` | the link as a QR code: error correction M, a border of 2 modules, black on white, `scale` pixels a module (2 to 20, 8 if not given). 404 for a convoy that has ended |
| `GET /c/<token>/qr.svg` | the same as SVG, 8 units a module |
| `GET /c/<token>/voice/<id>.m4a` | a voice note's sound (`audio/mp4`); 404 once it is no longer kept |
| `GET /static/<file>` | the pages' stylesheet and map styles |
| `GET /health` | `ok` |
| `GET /ws?c=<token>` | the WebSocket (below) |

The pages and the stylesheet are served `Cache-Control: no-cache`: a cached page could talk an older protocol.

A client sends a `User-Agent` of its own (the app's, `coldshift-dash-desktop`, …): Cloudflare's browser check, on by default for a domain on Cloudflare, refuses some libraries' defaults, such as Python's `Python-urllib`, with 403 (error 1010).

**Starting a convoy.** The name is cut to 60 characters (empty: "Convoy"), the password to 200. `key` is the starter's key (only its SHA-256 is kept): it alone can end the convoy, so the client keeps it. `link` uses the relay's public address if it has one set, else the address the request came to.

Starting a convoy needs the relay's **start key**, which its owner gives only to the apps they approve. A start without it is refused with 403 `{"error": "this host needs its start key to start a convoy", "code": "start_key"}`, and with a wrong one `{"error": "wrong start key", "code": "start_key"}`: a client asks its user for the key then, and keeps it for that relay. A relay whose owner has set no start key starts no convoys at all: 403 `{"error": "this relay cannot start convoys until its owner sets a start key", "code": "disabled"}`. Joining never needs the key. Too many starts from one address in an hour: 429 `{"error", "code": "busy"}`.

## The WebSocket

`/ws?c=<token>`, JSON in text frames. The query lets a relay send the socket to its convoy before the hello; the Cloudflare relay needs it, and answers a socket without it with an error (`code` "bad") and closes it.

**Staying in touch.** `{"t": "ping"}` (exactly that text) is answered `{"t": "pong"}` at any time, before the hello too. A client sends a frame at least every 20 seconds (a position, or a ping): a relay may close a connection it has heard nothing on for 45 seconds, and the others then hear the member has left. WebSocket protocol pings keep the connection open but do not count, as some relays answer them without the convoy hearing of it.

**Joining.** The first frame (pings aside) is the hello:

    {"t": "hello", "token", "password"?, "member", "name", "colour"?, "car"? (PNG, base64), "role": "vehicle" | "watcher"}

The name is cut to 40 characters, the colour to 9 (`#rrggbb`), and a car picture over 64 KB is dropped. A car picture is the car from above, front up, 96 px tall, on a transparent background. The relay answers

    {"t": "welcome", "name", "you", "link", "locked": bool, "members": [member...], "messages": [message...]}

or `{"t": "error", "error", "code"}` and closes:

| code | why |
|---|---|
| `bad` | no hello first, or a token or member id that cannot be one |
| `ended` | no such convoy: ended, or the link is wrong |
| `password` | none given ("this convoy needs its password"), or a wrong one ("wrong password") |
| `wait` | 10 wrong passwords for this convoy from one address in 10 minutes: try again later |
| `full` | 30 members already, and this is not one of them |

A member is `{"id", "name", "colour", "car", "role", "online", "pos"}`: `pos` is its latest position (or null), `online` whether it has a connection open. A member id the convoy knows may connect again (after a drop, or from a second device): it is the same member.

**Then, from the client:**

    {"t": "pos", "lat", "lon", "speed", "bearing", "time"}       where it is now (m/s, degrees, ms since 1970)
    {"t": "msg", "text", "cid"?, "age"?, "to"?}                  a message (500 characters at most)
    {"t": "voice", "ms", "data", "cid"?, "age"?, "to"?}          a voice note: AAC in MPEG-4 (.m4a), base64,
                                                                 192 KB and 31 seconds at most
    {"t": "leave"}                                               leaving for good: forgotten, then closed

**And from the relay:**

    {"t": "pos", "member", "lat", "lon", "speed", "bearing", "time"}     another member moved (not sent back to the sender)
    {"t": "msg", "id", "time", "from", "name", "text", "cid"?, "late"?, "to"?, "to_name"?}
    {"t": "voice", "id", "time", "from", "name", "ms", "path", "cid"?, "late"?, "to"?, "to_name"?}
    {"t": "ack", "cid"}                                          that one was had already (or was empty)
    {"t": "member", "member": {...}}                             someone joined, or joined again
    {"t": "left", "member": id}                                  one of someone's connections closed
    {"t": "ended", "error"}                                      the starter ended the convoy; then it closes

A message or voice note goes to everyone, the sender included: the sender's copy is its confirmation. A voice note's sound is fetched from its `path`.

**Nothing lost on a bad connection.** A client keeps what it sends until the relay sends it back, and sends it again after reconnecting. `cid` is the client's own id for it (40 characters at most, unique for that member): a resend the relay has had already is answered `ack` and not delivered again, and one with nothing in it (empty text, a voice note that is not one) is answered `ack` too, so it is not sent forever. `age` is how long it waited on the client, in ms (15 minutes at most): its `time` is then when it was said, and `late` (seconds) marks one that waited 30 seconds or more.

**Private messages.** `to` is a member's id (vehicle or watcher). Such a message or voice note goes only to that member's connections and the sender's, with `to` and `to_name` (the recipient's name). A `to` naming no member the convoy knows goes to the sender alone, with an empty `to_name`, so it is neither lost silently nor shared; `to` being the sender's own id is no private message. They are private so as not to clutter the shared feed, not for secrecy: the relay sees everything.

**History.** The welcome's `messages` are the convoy's latest 100, oldest first, holding a private one only for its sender and recipient.

## The relay's owner (optional)

A relay may let its owner see what convoys there are, so as to notice use that should not be there, and end one. The Cloudflare relay does, with its owner key (`ADMIN_KEY`, a secret only the owner has, never the apps'); with none set, these do not exist (404). The key goes in `Authorization: Bearer <key>`; without it, or with a wrong one, 403 `{"error", "code": "admin_key"}`, and 10 wrong ones from one address in an hour turn it away for the hour (429).

| | |
|---|---|
| `GET /api/admin/convoys` | `{"convoys": [convoy...]}`, the latest started first |
| `POST /api/admin/convoys/<token>/end` | ends it for everyone, as its starter would: they hear `{"t": "ended", "error": "the convoy was ended by the relay's owner"}`. 404 once it has ended |

A convoy here is `{"token", "link", "name", "started", "active", "country", "locked", "vehicles", "online_vehicles", "watchers", "online_watchers", "messages", "voice_notes"}`: times in ms since 1970, `country` the two letters of where it was started from (Cloudflare's guess; empty if unknown), `locked` whether it has a password, and how many messages and voice notes it holds. Never what is said in it, nor anyone's address: the owner sees the link, but joining a convoy with a password still needs it.

