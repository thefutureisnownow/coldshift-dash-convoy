# Security and privacy

What protects a convoy, what the relay and Cloudflare can see, what is kept and for how long. For anyone running a relay, and for anyone driving in a convoy on one.

## What protects a convoy

- **The link is the secret.** A convoy's token is 18 random bytes: it cannot be guessed, only passed on. Anyone holding the link can watch the convoy's page and, without a password, join it.
- **The password is a second lock.** With one, a link passed on (a screenshot of the QR code, a forwarded message) is not enough to join. It is kept only as a salted hash (PBKDF2-SHA256, 100,000 rounds), and 10 wrong guesses for a convoy from one address in 10 minutes turn that address away for a while. Use one for any convoy whose link might travel.
- **The starter's key ends it.** Starting a convoy returns a key that only the starter's device keeps (the relay keeps its SHA-256): with it, the convoy can be ended for everyone at once.
- **The start key keeps your relay yours.** Only apps holding it can start convoys; a relay with none set starts none, so a fresh copy is never open to anyone. It goes to phones from the desktop app, never in a link or QR code, and convoys are not started from the relay's web page. 60 starts an hour from one address is the limit even with it.

Starting convoys is limited per address from the `CF-Connecting-IP` header, which Cloudflare sets and a client cannot.

- **The owner key lets you watch over your relay.** With one set (`ADMIN_KEY`, yours alone, never the apps'), you can list the convoys on it and end one: their names, when they started and were last active, the country each was started from, how many cars and watchers, messages and voice notes, and whether they have a password. Never what is said in them, nor anyone's address. You also get each convoy's link, so a convoy without a password is open to you as to anyone holding its link; one with a password still needs it. 10 wrong owner keys from one address in an hour turn it away for the hour.

## Who can see what

**On the way: encrypted.** The app and the page talk to a Cloudflare relay over HTTPS and secure WebSockets (TLS 1.2 or later; the app turns TLS 1.2 on itself on Android 4.4, where it is off by default). Cloudflare ends TLS at its edge, and the relay runs inside Cloudflare, so there is no second, unencrypted hop to a server somewhere else.

**At the relay: readable.** It is a relay, not end-to-end encryption: the relay's code, and so Cloudflare and whoever runs the relay, can read positions, names, car pictures, messages and voice notes. Private messages are private from the rest of the convoy, not from the relay. Choose a relay you trust as you would a group chat's.

**On a local network: not encrypted.** A phone or laptop hosting a convoy on its own Wi-Fi serves plain HTTP: anyone on that network can read it.

**Third parties in the page.** The convoy page loads its map library (MapLibre GL JS) from unpkg.com and the map tiles from OpenFreeMap, so those see a browser's address and which parts of the map it looks at, as with any web map. The app draws its own offline maps and makes no such requests.

## What is kept, and for how long

| | kept | until |
|---|---|---|
| the convoy: token, name, password hash, starter's key hash, times | Durable Object storage | it is ended, or nobody is in it for `IDLE_HOURS` (12) |
| members: id, name, colour, car picture, role, last position when they left | Durable Object storage | the same, or the member leaves the convoy |
| messages: the latest 100 (text, sender, time) | Durable Object storage | the same; older ones are deleted as new ones come |
| voice notes: the sound of the latest 30 | Durable Object storage | the same |
| positions while driving | in memory, never written | the connection closes |
| wrong passwords: address and time | Durable Object storage | 10 minutes |
| convoys started: address and times | Durable Object storage | an hour after the last |
| the list for the relay's owner: each convoy's token, name, start time, country it was started from, and whether it has a password | Durable Object storage | the convoy ends |
| wrong owner keys: address and times | Durable Object storage | an hour after the last |

Ending a convoy, or its idle time running out, deletes everything about it. There are no accounts, no cookies and no analytics; the relay logs nothing itself. Cloudflare keeps its own records of requests to its network, as for any site on it: see [Cloudflare's privacy policy](https://www.cloudflare.com/privacypolicy/). Workers Logs (which would keep the relay's own output) are off unless you turn them on.

In the browser, the convoy page keeps its member id and the name you typed (local storage), a convoy's password for the tab (session storage), and a starter's key (local storage), so they come back on reload.

## Phones on Android 4.4

Android 4.4's own list of certificate authorities predates the ones Cloudflare uses, so the car app carries their root certificates itself and trusts them after the phone's own. It carries Let's Encrypt's (ISRG Root X1, X2) and Google Trust Services' (GTS Root R1, R4). Cloudflare's free certificates come from Let's Encrypt or Google Trust Services, but can also come from SSL.com, and its backup certificates from Sectigo: if your relay's certificate is from one of those, phones on Android 4.4 refuse it until the app carries their roots too. Check which one your relay has:

```sh
openssl s_client -connect convoy.example.com:443 -servername convoy.example.com </dev/null 2>/dev/null | openssl x509 -noout -issuer
```

Newer phones use their own up-to-date list and are not affected.

## Reporting a problem

If you find a way to join a convoy without its link, guess passwords faster than the limit, read another convoy's messages, or anything else in this vein, please email support@tfinn.co.za rather than opening a public issue.
