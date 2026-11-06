// Generates the simple PWA icons (no dependencies): node scripts/generate-icons.mjs
// Draws a brand-blue rounded square with a white microphone using signed-distance shapes, then encodes PNGs.
import { deflateSync } from 'node:zlib';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const outDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'public', 'icons');
mkdirSync(outDir, { recursive: true });

const crcTable = new Uint32Array(256).map((_, n) => {
  let c = n;
  for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const out = Buffer.alloc(8 + data.length + 4);
  out.writeUInt32BE(data.length, 0);
  body.copy(out, 4);
  out.writeUInt32BE(crc32(body), 8 + data.length);
  return out;
}

function encodePng(size, rgba) {
  const stride = size * 4;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y += 1) {
    raw[y * (stride + 1)] = 0;
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8;
  header[9] = 6;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

const sdSegment = (px, py, ax, ay, bx, by) => {
  const pax = px - ax;
  const pay = py - ay;
  const bax = bx - ax;
  const bay = by - ay;
  const h = Math.max(0, Math.min(1, (pax * bax + pay * bay) / (bax * bax + bay * bay)));
  return Math.hypot(pax - bax * h, pay - bay * h);
};

const sdRoundedBox = (px, py, half, radius) => {
  const qx = Math.abs(px - 0.5) - (half - radius);
  const qy = Math.abs(py - 0.5) - (half - radius);
  return Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - radius;
};

// Microphone glyph in unit coordinates; `scale` shrinks it toward the centre for maskable safe zones.
function micDistance(x, y, scale) {
  const u = (x - 0.5) / scale + 0.5;
  const v = (y - 0.5) / scale + 0.5;
  const t = 0.034;
  const capsule = sdSegment(u, v, 0.5, 0.3, 0.5, 0.45) - 0.095;
  const ring = Math.abs(Math.hypot(u - 0.5, v - 0.46) - 0.18) - t;
  const holder = v >= 0.46 ? ring : Math.min(Math.hypot(u - 0.32, v - 0.46) - t, Math.hypot(u - 0.68, v - 0.46) - t);
  const stem = sdSegment(u, v, 0.5, 0.64, 0.5, 0.74) - t;
  const base = sdSegment(u, v, 0.38, 0.75, 0.62, 0.75) - t;
  return Math.min(capsule, holder, stem, base) * scale;
}

function render(size, { maskable }) {
  const rgba = Buffer.alloc(size * size * 4);
  const ss = 3;
  const px = 1 / size;
  const glyphScale = maskable ? 0.74 : 0.9;
  const top = [0x2b, 0x88, 0xd8];
  const bottom = [0x0c, 0x3b, 0x5e];
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let sy = 0; sy < ss; sy += 1) {
        for (let sx = 0; sx < ss; sx += 1) {
          const u = (x + (sx + 0.5) / ss) / size;
          const v = (y + (sy + 0.5) / ss) / size;
          const inside = maskable ? 1 : Math.max(0, Math.min(1, 0.5 - sdRoundedBox(u, v, 0.5, 0.22) / px));
          if (inside <= 0) continue;
          const mix = (u + v) / 2;
          let cr = top[0] + (bottom[0] - top[0]) * mix;
          let cg = top[1] + (bottom[1] - top[1]) * mix;
          let cb = top[2] + (bottom[2] - top[2]) * mix;
          const glyph = Math.max(0, Math.min(1, 0.5 - micDistance(u, v, glyphScale) / px));
          cr += (255 - cr) * glyph;
          cg += (255 - cg) * glyph;
          cb += (255 - cb) * glyph;
          r += cr * inside;
          g += cg * inside;
          b += cb * inside;
          a += inside;
        }
      }
      const i = (y * size + x) * 4;
      const n = ss * ss;
      const alpha = a / n;
      rgba[i] = alpha > 0 ? Math.round(r / a) : 0;
      rgba[i + 1] = alpha > 0 ? Math.round(g / a) : 0;
      rgba[i + 2] = alpha > 0 ? Math.round(b / a) : 0;
      rgba[i + 3] = Math.round(alpha * 255);
    }
  }
  return encodePng(size, rgba);
}

const targets = [
  ['icon-192.png', 192, false],
  ['icon-512.png', 512, false],
  ['icon-maskable-512.png', 512, true],
  ['apple-touch-icon.png', 180, true],
];

for (const [name, size, maskable] of targets) {
  writeFileSync(join(outDir, name), render(size, { maskable }));
  console.log(`wrote public/icons/${name} (${size}x${size}${maskable ? ', full-bleed' : ''})`);
}
