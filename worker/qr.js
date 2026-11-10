// A convoy's link as a QR code, as the LAN relay draws it with segno: error correction M, a border of 2 modules,
// black on white. SVG at 8 px a module; PNG at `scale` px (the app asks for 8, the desktop tool for 6).
import { encode } from "uqr";

function modules(link) {
  return encode(link, { ecc: "M", border: 2 }).data; // rows of booleans, the border included
}

export function qrSvg(link, scale = 8) {
  const rows = modules(link);
  const n = rows.length;
  let path = "";
  rows.forEach((row, y) => {
    for (let x = 0; x < n; x++) {
      if (!row[x]) continue;
      let run = 1;
      while (x + run < n && row[x + run]) run++;
      path += `M${x} ${y}h${run}v1h-${run}z`;
      x += run - 1;
    }
  });
  const px = n * scale;
  return (
    `<?xml version="1.0" encoding="utf-8"?>\n` +
    `<svg xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" viewBox="0 0 ${n} ${n}" shape-rendering="crispEdges">` +
    `<rect width="${n}" height="${n}" fill="#ffffff"/><path fill="#000000" d="${path}"/></svg>\n`
  );
}

export async function qrPng(link, scale = 8) {
  const rows = modules(link);
  const n = rows.length;
  const px = n * scale;
  // 8-bit greyscale, each row led by filter type 0 (none)
  const raw = new Uint8Array(px * (px + 1));
  for (let y = 0; y < px; y++) {
    const row = rows[Math.floor(y / scale)];
    const at = y * (px + 1);
    raw[at] = 0;
    for (let x = 0; x < px; x++) raw[at + 1 + x] = row[Math.floor(x / scale)] ? 0 : 255;
  }
  const idat = await deflate(raw);
  const ihdr = new Uint8Array(13);
  const v = new DataView(ihdr.buffer);
  v.setUint32(0, px);
  v.setUint32(4, px);
  ihdr.set([8, 0, 0, 0, 0], 8); // bit depth 8, greyscale, deflate, adaptive filtering, no interlace
  return concat([
    new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", new Uint8Array(0)),
  ]);
}

async function deflate(bytes) {
  // "deflate" here is zlib's format (RFC 1950), which is what PNG's IDAT holds
  const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

function chunk(type, data) {
  const out = new Uint8Array(12 + data.length);
  const v = new DataView(out.buffer);
  v.setUint32(0, data.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  v.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
  return out;
}

function concat(parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
