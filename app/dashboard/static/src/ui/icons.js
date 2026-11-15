// Inline SVG glyphs. Literal templates; the shared ones are built from data.
import { html } from '../core/html.js';
import { item } from '../core/dict.js';
// The rail/tab bar glyphs, shared with the native pages (audit R-3). esbuild
// inlines the JSON into app.js. No import attribute: that spelling is ES2025,
// which Codacy's ESLint 8 cannot parse; `npm test` loads tests/json-hook.mjs
// so Node reads the import the way esbuild does.
import NAV from '../../nav.json';

// One template per shape nav.json may use, writing exactly the attributes
// app/dashboard/nav.py's svg() writes, in the same order. Values land in
// quoted attribute slots, so html escapes them; an element or attribute name
// can never come from the data.
const SHAPES = {
  path: (s) => html`<path d="${s.d}"/>`,
  rect: (s) => html`<rect x="${s.x}" y="${s.y}" width="${s.width}" height="${s.height}" rx="${s.rx}"/>`,
  circle: (s) => html`<circle cx="${s.cx}" cy="${s.cy}" r="${s.r}"/>`,
  line: (s) => html`<line x1="${s.x1}" y1="${s.y1}" x2="${s.x2}" y2="${s.y2}"/>`,
};

/** A nav.json glyph as Markup. Throws on an unknown icon or shape, so a bad
 *  edit to nav.json fails the tests instead of drawing a blank. */
export function icon(name) {
  const i = item(NAV.icons, name);
  if (!i || typeof i !== 'object') throw new Error('nav.json has no icon ' + name);
  const shapes = i.shapes.map((s) => {
    const draw = item(SHAPES, s.el);
    if (typeof draw !== 'function') throw new Error('nav.json icon ' + name + ': unknown shape ' + s.el);
    return draw(s);
  });
  return html`<svg viewBox="0 0 24 24" fill="none" stroke="${i.stroke}" stroke-width="${i.width}" stroke-linecap="${i.cap}" stroke-linejoin="${i.join}" aria-hidden="true">${shapes}</svg>`;
}

export const ARROW_UP = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true"><path d="M7 14l5-5 5 5"/></svg>`;
export const ARROW_DN = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true"><path d="M7 10l5 5 5-5"/></svg>`;
// Matches the topbar snapshot pill's glyph: the same mark means the same thing
// wherever collection freshness is in question.
export const CLOCK = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:12px;height:12px;vertical-align:-1px;opacity:.8" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`;
export const SUN = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`;
export const MOON = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`;
export const CHECK = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>`;
export const X = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>`;
export const CHEVRON = html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>`;
export const FEED = {
  up: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>`,
  down: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7l6 6 4-4 8 8"/><path d="M17 17h4v-4"/></svg>`,
  // The rail's Alerts glyph: the same bell in both places.
  bell: icon('bell'),
  news: html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h13v14H6a2 2 0 0 1-2-2zM17 8h3v9a2 2 0 0 1-2 2"/><path d="M8 9h6M8 13h6"/></svg>`,
};
