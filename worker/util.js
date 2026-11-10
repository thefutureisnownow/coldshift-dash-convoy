// Small helpers shared by the Worker and its Durable Objects.

export const TOKEN_RE = /^[A-Za-z0-9_-]{20,64}$/;
export const ID_RE = /^[A-Za-z0-9_-]{6,64}$/;

export function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...headers },
  });
}

export function text(body, status = 200, headers = {}) {
  return new Response(body, { status, headers: { "Content-Type": "text/plain; charset=utf-8", ...headers } });
}

/** `n` random bytes as URL-safe base64 without padding (Python's secrets.token_urlsafe(n)). */
export function randomToken(n) {
  return base64url(crypto.getRandomValues(new Uint8Array(n)));
}

/** `n` random bytes as hex (Python's secrets.token_hex(n)). */
export function randomHex(n) {
  return hex(crypto.getRandomValues(new Uint8Array(n)));
}

export function base64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function hex(bytes) {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function fromHex(s) {
  const out = new Uint8Array(s.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
  return out;
}

const encoder = new TextEncoder();

export async function sha256(s) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(String(s))));
}

/** Whether two secrets are the same, in a time that does not depend on where they differ. */
export async function sameSecret(a, b) {
  const [x, y] = await Promise.all([sha256(a ?? ""), sha256(b ?? "")]);
  return crypto.subtle.timingSafeEqual(x, y);
}

/** A password's hash: PBKDF2-SHA256, 100,000 rounds (the most Workers allow), 32 bytes, as hex. */
export async function hashPassword(password, salt) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: 100000 }, key, 256);
  return hex(bits);
}

/** The address the request came from: Cloudflare sets CF-Connecting-IP and a client cannot. */
export function clientAddress(request) {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

/** The address of this relay in links and QR codes. */
export function baseUrl(request, env) {
  return (env.PUBLIC_URL || new URL(request.url).origin).replace(/\/+$/, "");
}

/** A request body as an object ({} if it is not JSON or not an object). */
export async function body(request) {
  try {
    const data = await request.json();
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}
