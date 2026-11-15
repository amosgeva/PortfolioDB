// Shared chart primitives: tick ladder, suspect-point detection, sparklines and
// the categorical palette. Pure functions over numbers; markup through html``.

import { html } from '../core/html.js';
import { THOUSAND, MILLION } from '../core/format.js';

// Ticks are stepped by repeated addition, so the last one can land a float
// hair above the maximum; this is the tolerance that keeps it.
const TICK_TOL = 1 / (THOUSAND * MILLION);

// Gridlines at fixed fractions of the data range land on values like
// 8,912.47, which is why they were never labelled. Ticks are chosen on the
// 1/2/5 ladder instead, so every line falls on a number a person would say.
// Rounding the ideal step UP to the next rung halves the ladder whenever the
// ideal lands just above one, so the rungs are scored against the target.
export function niceTicks(lo, hi, target) {
  const span = hi - lo;
  if (!(span > 0) || !Number.isFinite(span)) return [];
  const want = target || 4;
  const mag = Math.pow(10, Math.floor(Math.log(span / want) / Math.LN10));
  let best = null;
  [1, 2, 2.5, 5, 10].forEach((m) => {
    const step = m * mag;
    if (!(step > 0)) return;
    const first = Math.ceil(lo / step) * step;
    let n = 0;
    // Capped as well as bounded: a step that underflows would spin forever.
    for (let t = first; t <= hi + step * TICK_TOL && n < 24; t += step) n++;
    if (n < 2) return;
    const score = Math.abs(n - want) + (n < 3 ? 2 : 0);
    if (!best || score < best.score) best = { step, first, n, score };
  });
  if (!best) return [];
  const out = [];
  for (let i = 0, v = best.first; i < best.n; i++, v += best.step) out.push(v);
  return out;
}

// A snapshot the arithmetic cannot support: an isolated point that both of its
// neighbours tower over. A portfolio does not halve and recover inside one
// session - that shape is a partial collection. Measured against the
// neighbours rather than an average, so a real sustained fall never trips it.
const SUSPECT_RATIO = 1.6;
export function suspectIdx(vals) {
  const bad = {};
  let n = 0;
  for (let i = 1; i < vals.length - 1; i++) {
    const v = vals[i];
    if (v > 0 && vals[i - 1] > v * SUSPECT_RATIO && vals[i + 1] > v * SUSPECT_RATIO) { bad[i] = 1; n++; }
  }
  bad.count = n;
  return bad;
}
// Extent over the points we believe: excluding a suspect point from the domain
// is what restores the rest of the series to a readable scale.
export function cleanExtent(vals, bad) {
  let lo = Infinity, hi = -Infinity;
  vals.forEach((v, i) => { if (bad[i]) return; if (v < lo) lo = v; if (v > hi) hi = v; });
  if (lo === Infinity) { lo = Math.min.apply(null, vals); hi = Math.max.apply(null, vals); }
  return { min: lo, max: hi, range: (hi - lo) || 1 };
}
// The stroke is the data, so it breaks at a suspect point. The area wash is
// decoration, so it carries the last believed value across (`hold`).
export function sparkPath(hist, w, h, pad, bad, ext, hold) {
  const W = w || 84, H = h || 28, P = pad || 2, B = bad || {};
  const e = ext || cleanExtent(hist, B);
  let out = '', pen = 'M', last = null;
  hist.forEach((v0, i) => {
    let v = v0;
    if (B[i]) { if (!hold) { pen = 'M'; return; } v = last == null ? v : last; } else { last = v; }
    const x = P + (i / (hist.length - 1)) * (W - P * 2);
    const y = P + (1 - (v - e.min) / e.range) * (H - P * 2);
    out += (out ? ' ' : '') + pen + x.toFixed(1) + ',' + y.toFixed(1);
    pen = 'L';
  });
  return out;
}

// Monotonic gradient ids: two sparklines in one DOM must never share a fill.
let seq = 0;
export function nextId(prefix) { seq += 1; return prefix + seq; }

export function sparkSVG(hist, up, w, h) {
  const W = w || 84, H = h || 28;
  if (!hist || hist.length < 2) return html``;
  const bad = suspectIdx(hist), ext = cleanExtent(hist, bad);
  const color = up ? 'var(--up)' : 'var(--down)';
  const d = sparkPath(hist, W, H, 2, bad, ext);
  const fillD = sparkPath(hist, W, H, 2, bad, ext, true) + ' L' + (W - 2) + ',' + (H - 2) + ' L2,' + (H - 2) + ' Z';
  const id = nextId('g');
  return html`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".22"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><path d="${fillD}" fill="url(#${id})"/><path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

// One categorical ramp for every chart, solved in OKLCH rather than picked.
// Each hue sits at L 56-64% so it clears 3:1 on both --surface values and
// carries a 4.5:1 label. Hues stay >=18 degrees clear of --up, --down and
// --accent: a category must never borrow the colour that means profit, loss
// or "clickable". Interleaved so consecutive slices sit >=77 degrees apart;
// index 9 is reserved for the Other bucket.
export const CAT = ['#00a19f', '#a45b8d', '#277bb2', '#ac6231', '#8f63a9', '#009cba', '#af596f', '#756cb8', '#927102', '#6b7d23'];
export const CAT_OTHER = CAT[9];
// Cash is not a holding, so it is the one slice carrying no hue at all.
export const CAT_CASH = '#72767d';

// Hashes over CAT[0..8] only: index 9 belongs to the Other bucket.
export function symColor(sym) {
  let h = 0;
  const s = String(sym);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return CAT[h % 9];
}

// The label colour for an arbitrary fill, measured rather than listed.
export const INK_ON_FILL = '#151b24';
export function relLum(hex) {
  let h = String(hex).replace('#', '');
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  const c = [0, 2, 4].map((i) => Number.parseInt(h.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
let inkLum = null;
export function onFill(hex) {
  if (inkLum === null) inkLum = relLum(INK_ON_FILL);
  const L = relLum(hex);
  return (1.05 / (L + 0.05)) >= ((L + 0.05) / (inkLum + 0.05)) ? '#fff' : INK_ON_FILL;
}
