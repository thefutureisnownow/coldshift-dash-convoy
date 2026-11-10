# Convoy

Cars driving together see each other on a live map, and send each other messages and push-to-talk voice notes. Anyone following along can watch the convoy in a browser, and message the drivers.

This is the relay the cars talk through, and the convoy's web page. It runs on Cloudflare Workers, free, with nothing running between trips. The car app is [Coldshift Dash](https://dash.coldshift.racing), for Android phones from 4.4 up.

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/thefutureisnownow/coldshift-dash-convoy)

- **[Running your own](docs/cloudflare.md):** deploy it with a start key (no convoy starts without one), check it, what it costs (nothing, on the free plan).
- **[Security and privacy](docs/security.md):** what protects a convoy, what the relay can see, what is kept and for how long.
- **[The protocol](PROTOCOL.md):** what the relay, the app and the page say to each other, for anyone writing a client or another relay.

The app has no relay built in: each phone is given one (and, to start convoys there, its start key) by the desktop app, or has it typed in.

## How it works

A convoy is an unguessable link (also a QR code), with an optional password. Each convoy is a Cloudflare Durable Object that holds the connections of the cars and watchers in it, and keeps its latest 100 messages and 30 voice notes. Positions travel through it but are never stored. A convoy ends when its starter ends it, or 12 hours after the last person leaves, and everything about it goes with it.

```
worker/        the relay: pages and starting convoys (index.js), a convoy (convoy.js), the owner's list, limits, QR codes
site/          the convoy's page, and a front page that says what this is (no build step)
tests/         the conformance suite: checks any running relay against PROTOCOL.md
tools/         the owner's list of convoys (convoys.py), pretend cars (demo_convoy.py, sim_car.py), a private-message check
lan/           a relay for a laptop on a local network (Python), for meets without internet
```

## Developing

```sh
npm ci
cp .dev.vars.example .dev.vars          # START_KEY=dev-key, STARTS_PER_HOUR=1000
npx wrangler dev                        # http://127.0.0.1:8787
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
CONVOY_URL=http://127.0.0.1:8787 CONVOY_START_KEY=dev-key .venv/bin/python -m unittest discover -s tests/conformance
```

## Licence

MIT. See [LICENSE](LICENSE). Map data © OpenStreetMap contributors, tiles by OpenFreeMap; QR codes by [uqr](https://github.com/unjs/uqr) (MIT).
