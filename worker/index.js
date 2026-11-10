// The Convoy relay on Cloudflare Workers. PROTOCOL.md is the contract; tests/conformance checks it.
//
// This Worker serves the pages and starts convoys; each convoy is a Durable Object (Convoy, named by its token)
// that holds its sockets, members, messages and voice notes. Limits (one per address and kind) counts convoys
// started and wrong owner keys; Registry lists the convoys for the relay's owner (/api/admin/, with ADMIN_KEY).
import { Convoy } from "./convoy.js";
import { Limits } from "./limits.js";
import { Registry } from "./registry.js";
import { TOKEN_RE, baseUrl, body, clientAddress, json, randomToken, sameSecret, sha256, hex, text } from "./util.js";

export { Convoy, Limits, Registry };

const PAGE_HEADERS = { "Cache-Control": "no-cache" }; // a cached page could talk an old protocol
const STATIC = /^\/static\/([a-z0-9-]+\.(css|json))$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;
    let m;

    if (path === "/health") return text("ok");
    if (path === "/" && (method === "GET" || method === "HEAD")) return asset(env, url, "/index.html");
    if ((m = path.match(STATIC)) && method === "GET") return asset(env, url, "/" + m[1]);
    if (path === "/api/convoys" && method === "POST") return start(request, env);
    if ((m = path.match(/^\/api\/convoys\/([^/]+)\/end$/)) && method === "POST") return toConvoy(env, m[1], request);
    if ((m = path.match(/^\/c\/([^/]+)\/?$/)) && method === "GET") return asset(env, url, "/watch.html");
    if ((m = path.match(/^\/c\/([^/]+)\/(qr\.(png|svg)|voice\/[A-Za-z0-9_-]+\.m4a)$/)) && method === "GET")
      return toConvoy(env, m[1], request);
    if (path === "/ws") return socket(request, env, url);
    if (path.startsWith("/api/admin/")) return admin(request, env, path);
    return text("not found", 404);
  },
};

async function asset(env, url, name) {
  const r = await env.ASSETS.fetch(new URL(name, url));
  if (!r.ok) return text("not found", 404);
  const out = new Response(r.body, r);
  out.headers.set("Cache-Control", PAGE_HEADERS["Cache-Control"]);
  return out;
}

function toConvoy(env, token, request) {
  if (!TOKEN_RE.test(token)) return text("no such convoy", 404);
  return env.CONVOY.get(env.CONVOY.idFromName(token)).fetch(request);
}

async function socket(request, env, url) {
  if (request.headers.get("Upgrade") !== "websocket") return text("expected a WebSocket", 426);
  const token = url.searchParams.get("c") || "";
  if (TOKEN_RE.test(token)) return env.CONVOY.get(env.CONVOY.idFromName(token)).fetch(request);
  // an app from before /ws?c=<token>: say why, then close
  const [client, server] = Object.values(new WebSocketPair());
  server.accept();
  server.send(JSON.stringify({ t: "error", error: "this host needs a newer app: update it", code: "bad" }));
  server.close(1008, "update the app");
  return new Response(null, { status: 101, webSocket: client });
}

async function start(request, env) {
  const data = await body(request);
  // locked until its owner sets a start key: a relay is never open to anyone by default
  if (!env.START_KEY)
    return json({ error: "this relay cannot start convoys until its owner sets a start key", code: "disabled" }, 403);
  if (!(await sameSecret(data.start_key, env.START_KEY))) {
    const error = data.start_key ? "wrong start key" : "this host needs its start key to start a convoy";
    return json({ error, code: "start_key" }, 403);
  }
  const limits = env.LIMITS.get(env.LIMITS.idFromName(clientAddress(request)));
  if (!(await limits.allow(parseInt(env.STARTS_PER_HOUR || "60", 10), 3600)))
    return json({ error: "too many convoys started from here; try again later", code: "busy" }, 429);

  const token = randomToken(18);
  const key = randomToken(18);
  const password = String(data.password || "").slice(0, 200);
  const convoy = env.CONVOY.get(env.CONVOY.idFromName(token));
  const name = await convoy.create(token, String(data.name || "Convoy"), password, hex(await sha256(key)));
  await registry(env).add({ token, name, started: Date.now(), country: request.cf?.country || "", locked: Boolean(password) });
  return json({ token, name, link: `${baseUrl(request, env)}/c/${token}`, password: Boolean(password), key });
}

function registry(env) {
  return env.REGISTRY.get(env.REGISTRY.idFromName("registry"));
}

// ---- the relay's owner: what convoys there are, and ending one (ADMIN_KEY; none set: none of this exists) ----

async function admin(request, env, path) {
  if (!env.ADMIN_KEY) return text("not found", 404);
  const limits = env.LIMITS.get(env.LIMITS.idFromName("admin:" + clientAddress(request)));
  if (await limits.blocked(10, 3600)) return json({ error: "too many wrong owner keys from here; try again later", code: "busy" }, 429);
  const given = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  if (!given || !(await sameSecret(given, env.ADMIN_KEY))) {
    await limits.count(3600);
    return json({ error: "that is not this relay's owner key", code: "admin_key" }, 403);
  }
  let m;
  if (path === "/api/admin/convoys" && request.method === "GET") {
    const base = baseUrl(request, env);
    const listed = registry(env).list();
    const rows = await Promise.all((await listed).map(async (c) => {
      const status = await env.CONVOY.get(env.CONVOY.idFromName(c.token)).status();
      if (!status) {
        await registry(env).remove(c.token); // ended without saying (cannot happen, but keep the list true)
        return null;
      }
      return { token: c.token, link: `${base}/c/${c.token}`, country: c.country, ...status };
    }));
    return json({ convoys: rows.filter(Boolean) });
  }
  if ((m = path.match(/^\/api\/admin\/convoys\/([A-Za-z0-9_-]{20,64})\/end$/)) && request.method === "POST") {
    const ended = await env.CONVOY.get(env.CONVOY.idFromName(m[1])).endByOwner();
    if (!ended) {
      await registry(env).remove(m[1]);
      return json({ error: "no such convoy (it has ended already)" }, 404);
    }
    return json({ ended: true });
  }
  return text("not found", 404);
}
