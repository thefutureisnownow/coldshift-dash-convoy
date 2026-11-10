# Running your own Convoy relay on Cloudflare

The relay runs on Cloudflare Workers, with a Durable Object for each convoy. On Cloudflare's free plan it costs nothing, and nothing at all while nobody is driving: there is no server to keep running between trips.

You need a Cloudflare account (free). A domain of your own on Cloudflare is optional: without one, your copy lives at `https://coldshift-dash-convoy.<your-subdomain>.workers.dev`.

## 1. Deploy it

**With the button** (no tools needed):

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/thefutureisnownow/coldshift-dash-convoy)

It copies this repository to your GitHub account, connects it to your Cloudflare account, and deploys it. When it asks for `START_KEY`, give it a long random secret (step 2): until it has one, your relay starts no convoys. Later changes you push to your copy deploy by themselves.

**Or from a terminal** (Node.js 22 or later):

```sh
git clone https://github.com/thefutureisnownow/coldshift-dash-convoy
cd coldshift-dash-convoy
npm ci
npx wrangler login                     # opens your browser to sign in to Cloudflare
npx wrangler deploy                    # prints your copy's address
```

On a domain of yours (it must be on Cloudflare's DNS), deploy with `--domain` instead:

```sh
npx wrangler deploy --domain convoy.example.com
```

The first deploy may ask you to pick a workers.dev subdomain for your account.

## 2. Set the start key

Starting a convoy needs the start key, so only you and the apps you give it to can start convoys on your relay. Without one, your relay starts none at all (it is never open to anyone by default). Joining a convoy needs only its link, and its password if it has one:

```sh
openssl rand -base64 24                # a good key
npx wrangler secret put START_KEY      # paste it when asked
```

Give it to your phones with the desktop app (Convoy tab: Start key, then Sync to card), or type it in the app, which asks for it the first time it starts a convoy on your relay. It is never in a convoy's link or QR code. Change it the same way, then give your phones the new one. Convoys are not started from the relay's web page.

## 3. Check it

```sh
curl https://<your relay>/health                        # ok
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt
CONVOY_URL=https://<your relay> CONVOY_START_KEY=<your key> CONVOY_WAIT=10 \
  .venv/bin/python -m unittest discover -s tests/conformance
```

The conformance suite starts about 20 convoys of its own (they end by themselves after the idle time), so it needs `STARTS_PER_HOUR` of at least 25: raise it for the run, or run it once an hour at most.

To see it with cars on the map, `tools/demo_convoy.py --server https://<your relay> --start-key <your key>` drives four pretend cars along the N2 and prints the convoy's page.

## 4. Use it

In the car app: **Convoy, Change host**, and type your relay's address (`convoy.example.com`: a bare domain means https). On the convoy page, open your relay's address in a browser.

## 5. See what's happening (optional)

To see the convoys on your relay, so you notice use that shouldn't be there (your start key passed on, say), and end one, give the relay an owner key. It's yours alone, never the apps':

```sh
openssl rand -base64 24                # another key, not the start key
npx wrangler secret put ADMIN_KEY
```

Then, with `ADMIN_KEY=…` in a file only you can read (or in `$CONVOY_ADMIN_KEY`):

```sh
.venv/bin/python tools/convoys.py https://<your relay> --keys ~/.config/convoy-keys.env           # list them
.venv/bin/python tools/convoys.py https://<your relay> end <token> --keys ~/.config/convoy-keys.env  # end one
```

For each convoy it shows its name, when it started and was last active, the country it was started from, its cars and watchers (and how many are online), how many messages and voice notes it holds, and whether it has a password. Never what is said in it: you get its link, but a convoy with a password still needs it to be joined. Ending one tells everyone in it the relay's owner ended it. Only convoys started after the owner key was added are listed. Without an owner key, none of this exists.

## Settings

In `wrangler.jsonc` under `vars` (deploy again after changing them), or in the Cloudflare dashboard under your Worker's **Settings, Variables and secrets**:

| | | |
|---|---|---|
| `START_KEY` | (a secret, not a var) | needed to start a convoy; unset: no convoy can be started |
| `ADMIN_KEY` | (a secret, not a var) | yours alone: list the convoys and end one; unset: nobody can |
| `IDLE_HOURS` | 12 | a convoy with nobody in it ends after this long, its messages and voice notes with it |
| `STARTS_PER_HOUR` | 60 | convoys one address may start in an hour |
| `PUBLIC_URL` | empty | the address in links and QR codes; empty: the address your relay was reached at |

## What it costs

On the free plan, nothing. The free plan's daily limits (they reset at midnight UTC) and what a convoy uses of them:

- **Requests: 100,000 a day.** Each WebSocket message counts as a twentieth of one. A car sends a position every 3 seconds while moving, so five cars driving for eight hours is about 2,400; page loads, QR codes and voice notes add a few hundred.
- **Durable Object duration: 13,000 GB-s a day.** A convoy with cars moving is awake the whole time, at 128 MB: about 28 hours of convoy a day in all. A quiet one (everyone parked, only pings) sleeps and costs nothing.
- **Storage: 100,000 rows written a day, 5 GB stored.** Positions are never written: only members joining, messages (a few rows each), voice notes and the convoy itself. A day's convoy writes a few thousand rows at most.

Over a limit, requests of that kind fail until the next day; nothing is charged. The [Workers Paid plan](https://developers.cloudflare.com/workers/platform/pricing/) (5 USD a month) raises all of them a long way.

## Updating

With the button: pull this repository's changes into your copy on GitHub; it deploys by itself. From a terminal: `git pull && npx wrangler deploy`.

The app and the page follow the protocol in [PROTOCOL.md](../PROTOCOL.md); an update that changes it says so in its release notes.

## Developing

```sh
cp .dev.vars.example .dev.vars          # set START_KEY=dev-key, and STARTS_PER_HOUR=1000 for the tests
npx wrangler dev                        # http://127.0.0.1:8787, with the convoys kept in .wrangler/state
CONVOY_URL=http://127.0.0.1:8787 CONVOY_START_KEY=dev-key .venv/bin/python -m unittest discover -s tests/conformance
```

`worker/index.js` serves the pages (from `site/`) and starts convoys; `worker/convoy.js` is a convoy (its sockets, members, messages and voice notes); `worker/limits.js` counts starts and wrong owner keys per address; `worker/registry.js` lists the convoys for the owner; `worker/qr.js` draws the QR codes.
