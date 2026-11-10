// One convoy: a Durable Object named by the convoy's token, holding its sockets (with the WebSocket Hibernation
// API, so a quiet convoy costs nothing), and in its SQLite storage the convoy itself, its members, the latest
// messages and voice notes, and recent message ids (to drop resends). Positions are never written to storage:
// each car's latest rides on its socket (serializeAttachment), and is stored only when the car's last socket
// closes, so a convoy on the move stays far inside the free plan's daily writes. See PROTOCOL.md.
import { DurableObject } from "cloudflare:workers";
import { qrPng, qrSvg } from "./qr.js";
import {
  ID_RE, TOKEN_RE, baseUrl, body, clientAddress, fromHex, hashPassword, hex, json, randomHex, randomToken,
  sameSecret, sha256, text,
} from "./util.js";

const MAX_MEMBERS = 30;
const MAX_TEXT = 500;
const MAX_CAR = 64 * 1024; // the car's top-down picture, base64
const MAX_VOICE = 192 * 1024; // 30 s at the app's 24 kbit/s is about 90 KB
const MAX_VOICE_MS = 31000;
const MAX_FRAME = 320 * 1024; // a voice note in base64, with room
const KEEP_MESSAGES = 100;
const KEEP_VOICES = 30;
const KEEP_CIDS = 1000;
const MAX_AGE_MS = 15 * 60 * 1000; // how long a message may have waited on the phone
const LATE_MS = 30 * 1000; // ... before it is marked late
const WRONG_PER_WINDOW = 10; // wrong passwords from one address in WRONG_WINDOW_MS turn it away
const WRONG_WINDOW_MS = 10 * 60 * 1000;
const SILENT_MS = 45 * 1000; // a socket silent this long is taken for gone (cars send a position every 15 s at most)
const CHECK_MS = 30 * 1000; // how often, while sockets are open
const ACTIVE_WRITE_MS = 60 * 1000; // the convoy's last-active time is written at most this often
const PING = '{"t":"ping"}';
const PONG = '{"t":"pong"}';

export class Convoy extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    // a convoy nobody is in ends after IDLE_HOURS (12 unless the Worker's vars say otherwise)
    this.idleMs = (parseFloat(env.IDLE_HOURS) || 12) * 3600 * 1000;
    this.queues = new Map(); // socket -> the promise of its frames handled so far (one at a time, in order)
    // answered by the runtime, without waking this object: a browser that only watches stays known to be alive
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair(PING, PONG));
    this.load();
  }

  load() {
    // an object for an unknown token never gets tables: probing tokens leaves nothing behind
    const made = this.sql.exec("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'convoy'").toArray().length;
    this.meta = made ? this.sql.exec("SELECT * FROM convoy WHERE id = 1").toArray()[0] || null : null;
    this.active = this.meta ? this.meta.active : 0;
    this.activeWritten = this.active;
  }

  // ---- made by the Worker -------------------------------------------------------------------------------------

  /** Makes this convoy; returns its name as kept. */
  async create(token, name, password, keyHash) {
    name = String(name).trim().slice(0, 60) || "Convoy";
    let salt = "";
    let pw = "";
    if (password) {
      const s = crypto.getRandomValues(new Uint8Array(16));
      salt = hex(s);
      pw = await hashPassword(password, s);
    }
    const now = Date.now();
    for (const ddl of [
      `CREATE TABLE IF NOT EXISTS convoy (id INTEGER PRIMARY KEY CHECK (id = 1), token TEXT NOT NULL, name TEXT NOT NULL,
         salt TEXT NOT NULL, pw TEXT NOT NULL, key TEXT NOT NULL, created INTEGER NOT NULL, active INTEGER NOT NULL)`,
      "CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, data TEXT NOT NULL)",
      `CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL,
         sender TEXT NOT NULL, recipient TEXT NOT NULL)`,
      "CREATE TABLE IF NOT EXISTS voices (id TEXT PRIMARY KEY, seq INTEGER NOT NULL, sound BLOB NOT NULL)",
      "CREATE TABLE IF NOT EXISTS cids (member TEXT NOT NULL, cid TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY (member, cid))",
      "CREATE TABLE IF NOT EXISTS wrong (ip TEXT NOT NULL, at INTEGER NOT NULL)",
    ])
      this.sql.exec(ddl);
    this.sql.exec(
      "INSERT OR REPLACE INTO convoy (id, token, name, salt, pw, key, created, active) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
      token, name, salt, pw, keyHash, now, now,
    );
    this.load();
    await this.ctx.storage.setAlarm(now + this.idleMs);
    return name;
  }

  // ---- HTTP ---------------------------------------------------------------------------------------------------

  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;
    if (path === "/ws") {
      const [client, server] = Object.values(new WebSocketPair());
      this.ctx.acceptWebSocket(server);
      server.serializeAttachment({ ip: clientAddress(request), base: baseUrl(request, this.env), member: null, last: Date.now() });
      if (this.meta) await this.checkSoon();
      return new Response(null, { status: 101, webSocket: client });
    }
    if (path.endsWith("/end")) {
      if (!this.meta) return json({ error: "this convoy has ended already" }, 404);
      const data = await body(request);
      const given = hex(await sha256(String(data.key ?? "")));
      if (!this.meta.key || !(await sameSecret(given, this.meta.key)))
        return json({ error: "only the convoy's starter can end it" }, 403);
      await this.end();
      return json({ ended: true });
    }
    let m;
    if ((m = path.match(/\/qr\.(png|svg)$/))) {
      if (!this.meta) return text("no such convoy", 404);
      const link = `${baseUrl(request, this.env)}/c/${this.meta.token}`;
      if (m[1] === "svg") return new Response(qrSvg(link), { headers: { "Content-Type": "image/svg+xml" } });
      const q = url.searchParams.get("scale") || "8";
      const scale = /^\s*[-+]?\d+\s*$/.test(q) ? Math.max(2, Math.min(20, parseInt(q, 10))) : 8;
      return new Response(await qrPng(link, scale), { headers: { "Content-Type": "image/png" } });
    }
    if ((m = path.match(/\/voice\/([A-Za-z0-9_-]+)\.m4a$/))) {
      const row = this.meta ? this.sql.exec("SELECT sound FROM voices WHERE id = ?", m[1]).toArray()[0] : null;
      if (!row) return text("that voice note is gone", 404);
      return new Response(row.sound, { headers: { "Content-Type": "audio/mp4", "Cache-Control": "private, max-age=86400" } });
    }
    return text("not found", 404);
  }

  // ---- sockets ------------------------------------------------------------------------------------------------

  async webSocketMessage(ws, message) {
    // a hello waits on its password's hash; whatever the socket sends after it must wait its turn
    const before = this.queues.get(ws) || Promise.resolve();
    const now = before.then(() => this.frame(ws, message)).catch((e) => console.error("frame", e));
    this.queues.set(ws, now);
    await now;
    if (this.queues.get(ws) === now) this.queues.delete(ws);
  }

  async webSocketClose(ws, code, reason) {
    this.closed(ws);
    try {
      ws.close(code === 1005 || code === 1006 ? 1000 : code, reason); // the answer to the client's close
    } catch {}
  }

  async webSocketError(ws) {
    this.closed(ws);
  }

  async frame(ws, message) {
    const a = ws.deserializeAttachment();
    if (!a || a.gone) return;
    if (typeof message !== "string") return;
    if (message.length > MAX_FRAME) {
      ws.close(1009, "too big");
      return this.closed(ws);
    }
    let data;
    try {
      data = JSON.parse(message);
    } catch {
      return;
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) return;
    a.last = Date.now();
    ws.serializeAttachment(a);
    if (data.t === "ping") return ws.send(PONG);
    if (!a.member) return this.hello(ws, a, data);
    if (!this.meta) return ws.close(1000, "ended");
    this.touch();
    const me = this.member(a.member);
    if (!me) return; // left (on another socket)
    if (data.t === "pos") return this.position(ws, a, me, data);
    if (data.t === "msg" || data.t === "voice") return this.message(ws, me, data);
    if (data.t === "leave") {
      this.sql.exec("DELETE FROM members WHERE id = ?", me.id);
      ws.close(1000, "left");
      return this.closed(ws);
    }
  }

  async hello(ws, a, data) {
    const fail = (error, code) => {
      ws.send(JSON.stringify({ t: "error", error, code }));
      a.gone = true;
      ws.serializeAttachment(a);
      ws.close(1000, code);
    };
    if (data.t !== "hello") return fail("say hello first", "bad");
    const token = String(data.token ?? "");
    const mid = String(data.member ?? "");
    if (!TOKEN_RE.test(token) || !ID_RE.test(mid)) return fail("that is not a convoy link", "bad");
    if (!this.meta || token !== this.meta.token) return fail("this convoy has ended (or the link is wrong)", "ended");
    if (this.blocked(a.ip)) return fail("too many wrong passwords from here: wait a few minutes", "wait");
    if (!(await this.check(data.password))) {
      if (data.password) this.sql.exec("INSERT INTO wrong (ip, at) VALUES (?, ?)", a.ip, Date.now()); // a guess
      return fail(data.password ? "wrong password" : "this convoy needs its password", "password");
    }
    const prev = this.member(mid);
    if (!prev && this.sql.exec("SELECT COUNT(*) AS n FROM members").one().n >= MAX_MEMBERS)
      return fail("the convoy is full", "full");
    const car = String(data.car || "");
    const me = {
      id: mid,
      name: String(data.name || "Car").slice(0, 40),
      colour: String(data.colour || "").slice(0, 9),
      car: car.length <= MAX_CAR ? car : "",
      role: data.role === "watcher" ? "watcher" : "vehicle",
      pos: this.livePos(mid) || (prev && prev.pos) || null,
    };
    this.save(me);
    a.member = mid;
    ws.serializeAttachment(a);
    this.touch(true);
    ws.send(JSON.stringify({
      t: "welcome", name: this.meta.name, you: mid, link: `${a.base}/c/${this.meta.token}`, locked: Boolean(this.meta.pw),
      members: this.members(), messages: this.history(mid),
    }));
    this.send({ t: "member", member: this.public(me) }, { but: ws });
  }

  position(ws, a, me, data) {
    const lat = Number(data.lat);
    const lon = Number(data.lon);
    const speed = Number(data.speed || 0);
    const bearing = Number(data.bearing || 0);
    const time = data.time ? Math.trunc(Number(data.time)) : Date.now();
    if (data.lat == null || data.lon == null || ![lat, lon, speed, bearing, time].every(Number.isFinite)) return;
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return;
    const pos = { lat, lon, speed, bearing, time };
    a.pos = pos;
    a.posAt = Date.now();
    ws.serializeAttachment(a);
    this.send({ ...pos, t: "pos", member: me.id }, { but: ws });
  }

  message(ws, me, data) {
    const cid = String(data.cid || "").slice(0, 40);
    let to = String(data.to || "").slice(0, 64);
    if (to === me.id) to = ""; // to oneself is no private message
    if (cid && this.sql.exec("SELECT 1 FROM cids WHERE member = ? AND cid = ?", me.id, cid).toArray().length)
      return ws.send(JSON.stringify({ t: "ack", cid })); // a resend of one it has
    let m = null;
    if (data.t === "msg") {
      const said = String(data.text || "").trim().slice(0, MAX_TEXT);
      if (said) m = { t: "msg", id: randomHex(6), text: said };
    } else {
      const ms = Math.trunc(Number(data.ms || 0)) || 0;
      const sound = voiceData(data.data);
      if (sound && ms > 0 && ms <= MAX_VOICE_MS) {
        const vid = this.keepVoice(sound);
        m = { t: "voice", id: vid, ms, path: `/c/${this.meta.token}/voice/${vid}.m4a` };
      }
    }
    if (!m) {
      if (cid) ws.send(JSON.stringify({ t: "ack", cid })); // nothing to send: not again either
      return;
    }
    const age = Math.max(0, Math.min(MAX_AGE_MS, Math.trunc(Number(data.age || 0)) || 0));
    Object.assign(m, { time: Date.now() - age, from: me.id, name: me.name });
    if (age >= LATE_MS) m.late = Math.floor(age / 1000);
    if (cid) {
      m.cid = cid;
      this.sql.exec("INSERT OR IGNORE INTO cids (member, cid, seq) VALUES (?, ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM cids))", me.id, cid);
      this.sql.exec("DELETE FROM cids WHERE seq <= (SELECT MAX(seq) FROM cids) - ?", KEEP_CIDS);
    }
    let only = null;
    if (to) {
      const them = this.member(to);
      m.to = to;
      m.to_name = them ? them.name : "";
      // a private one reaches only the sender and the recipient (an unknown recipient: the sender alone)
      only = new Set(them ? [me.id, to] : [me.id]);
    }
    this.sql.exec("INSERT INTO messages (data, sender, recipient) VALUES (?, ?, ?)", JSON.stringify(m), me.id, to);
    this.sql.exec("DELETE FROM messages WHERE seq <= (SELECT MAX(seq) FROM messages) - ?", KEEP_MESSAGES);
    this.send(m, { only });
  }

  /** A socket is gone (closed by either side, failed, or silent too long): the others hear "left". */
  closed(ws) {
    const a = ws.deserializeAttachment();
    if (!a || a.gone === "done") return;
    const wasMember = a.member && !a.gone;
    a.gone = "done";
    try {
      ws.serializeAttachment(a);
    } catch {}
    this.queues.delete(ws);
    if (!wasMember || !this.meta) return;
    if (!this.sockets(a.member).length && a.pos) {
      const m = this.member(a.member);
      if (m) this.save({ ...m, pos: a.pos }); // its last position, for whoever joins while it is away
    }
    this.send({ t: "left", member: a.member }, { but: ws });
  }

  // ---- time ---------------------------------------------------------------------------------------------------

  /** While sockets are open, look every CHECK_MS for silent ones; once none are, end the convoy after IDLE_HOURS. */
  async alarm() {
    if (!this.meta) return;
    const now = Date.now();
    for (const ws of this.sockets(null, true)) {
      const a = ws.deserializeAttachment();
      const pinged = this.ctx.getWebSocketAutoResponseTimestamp(ws);
      if (now - Math.max(a.last || 0, pinged ? pinged.getTime() : 0) > SILENT_MS) {
        try {
          ws.close(1001, "silent");
        } catch {}
        this.closed(ws);
      }
    }
    if (this.sockets(null, true).length) {
      this.touch(); // someone is in it: not idle
      return this.ctx.storage.setAlarm(now + CHECK_MS);
    }
    this.writeActive(); // the last activity, from before everyone went
    if (now - this.active >= this.idleMs) return this.wipe();
    return this.ctx.storage.setAlarm(this.active + this.idleMs);
  }

  async checkSoon() {
    const at = await this.ctx.storage.getAlarm();
    if (!at || at > Date.now() + CHECK_MS) await this.ctx.storage.setAlarm(Date.now() + CHECK_MS);
  }

  touch(write = false) {
    this.active = Date.now();
    if (write || this.active - this.activeWritten >= ACTIVE_WRITE_MS) this.writeActive();
  }

  writeActive() {
    if (!this.meta || this.activeWritten === this.active) return;
    this.sql.exec("UPDATE convoy SET active = ? WHERE id = 1", this.active);
    this.activeWritten = this.active;
  }

  /** For the relay's owner: what the convoy is and how busy, never what is said in it (null once it has ended). */
  status() {
    if (!this.meta) return null;
    const members = this.sql.exec("SELECT data FROM members").toArray().map((r) => JSON.parse(r.data));
    const online = new Set(this.sockets().map((ws) => ws.deserializeAttachment().member));
    const count = (table) => this.sql.exec(`SELECT COUNT(*) AS n FROM ${table}`).one().n;
    return {
      name: this.meta.name,
      started: this.meta.created,
      active: Math.max(this.active, this.meta.active),
      locked: Boolean(this.meta.pw),
      vehicles: members.filter((m) => m.role === "vehicle").length,
      watchers: members.filter((m) => m.role === "watcher").length,
      online_vehicles: members.filter((m) => m.role === "vehicle" && online.has(m.id)).length,
      online_watchers: members.filter((m) => m.role === "watcher" && online.has(m.id)).length,
      messages: count("messages"),
      voice_notes: count("voices"),
    };
  }

  /** The relay's owner ends it: as its starter would, saying who did. False if it had ended already. */
  async endByOwner() {
    if (!this.meta) return false;
    await this.end("the convoy was ended by the relay's owner");
    return true;
  }

  async end(why = "the convoy was ended by its starter") {
    this.send({ t: "ended", error: why });
    for (const ws of this.ctx.getWebSockets()) {
      const a = ws.deserializeAttachment() || {};
      a.gone = "done";
      ws.serializeAttachment(a);
      try {
        ws.close(1000, "ended");
      } catch {}
    }
    await this.wipe();
  }

  async wipe() {
    if (this.meta) {
      const registry = this.env.REGISTRY.get(this.env.REGISTRY.idFromName("registry"));
      await registry.remove(this.meta.token);
    }
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
    this.meta = null;
  }

  // ---- state --------------------------------------------------------------------------------------------------

  async check(password) {
    if (!this.meta.pw) return true;
    const h = await hashPassword(String(password || ""), fromHex(this.meta.salt));
    return sameSecret(h, this.meta.pw);
  }

  blocked(ip) {
    const since = Date.now() - WRONG_WINDOW_MS;
    this.sql.exec("DELETE FROM wrong WHERE at <= ?", since);
    return this.sql.exec("SELECT COUNT(*) AS n FROM wrong WHERE ip = ?", ip).one().n >= WRONG_PER_WINDOW;
  }

  keepVoice(sound) {
    const vid = randomToken(12);
    this.sql.exec("INSERT INTO voices (id, seq, sound) VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM voices), ?)", vid, sound);
    this.sql.exec("DELETE FROM voices WHERE seq <= (SELECT MAX(seq) FROM voices) - ?", KEEP_VOICES);
    return vid;
  }

  member(id) {
    const row = this.sql.exec("SELECT data FROM members WHERE id = ?", id).toArray()[0];
    return row ? JSON.parse(row.data) : null;
  }

  save(m) {
    this.sql.exec("INSERT OR REPLACE INTO members (id, data) VALUES (?, ?)", m.id, JSON.stringify(m));
  }

  members() {
    return this.sql.exec("SELECT data FROM members").toArray().map((r) => {
      const m = JSON.parse(r.data);
      return this.public({ ...m, pos: this.livePos(m.id) || m.pos || null });
    });
  }

  public(m) {
    return { id: m.id, name: m.name, colour: m.colour, role: m.role, online: this.sockets(m.id).length > 0, pos: m.pos ?? null, car: m.car };
  }

  /** The messages `member` may be shown: the shared ones, and private ones from or to it. */
  history(member) {
    return this.sql
      .exec("SELECT data FROM messages WHERE recipient = '' OR sender = ? OR recipient = ? ORDER BY seq", member, member)
      .toArray()
      .map((r) => JSON.parse(r.data));
  }

  /** The open sockets of `member` (null: of every member; `any`: also those not past their hello yet). */
  sockets(member = null, any = false) {
    return this.ctx.getWebSockets().filter((ws) => {
      if (ws.readyState !== WebSocket.OPEN) return false;
      const a = ws.deserializeAttachment();
      if (!a || a.gone) return false;
      if (!a.member) return any;
      return member === null || a.member === member;
    });
  }

  /** The latest position any open socket of `member` has sent. */
  livePos(member) {
    let best = null;
    for (const ws of this.sockets(member)) {
      const a = ws.deserializeAttachment();
      if (a.pos && (!best || a.posAt > best.at)) best = { pos: a.pos, at: a.posAt };
    }
    return best ? best.pos : null;
  }

  /** To every member's socket but `but`; with `only` (member ids), to just those members' sockets. */
  send(data, { but = null, only = null } = {}) {
    const s = JSON.stringify(data);
    for (const ws of this.sockets()) {
      if (ws === but) continue;
      if (only && !only.has(ws.deserializeAttachment().member)) continue;
      try {
        ws.send(s);
      } catch {}
    }
  }
}

/** A voice note's sound from base64, or null: an MPEG-4 file (its "ftyp" box first) no bigger than MAX_VOICE. */
function voiceData(b64) {
  if (typeof b64 !== "string" || b64.length > Math.floor((MAX_VOICE * 4) / 3) + 8) return null;
  if (b64.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(b64)) return null;
  let raw;
  try {
    raw = atob(b64);
  } catch {
    return null;
  }
  if (raw.length < 12 || raw.length > MAX_VOICE || raw.slice(4, 8) !== "ftyp") return null;
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
