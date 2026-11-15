// Symbol presentation: the drawer trigger attributes, the row button, the
// subtitle and the letter badge with its logo.
//
// A symbol is operator-entered text - the CSV importer and the CLIs accept
// whatever string a row carries - so it is interpolated through html`` like
// any other payload text. These helpers used to be where it reached the page
// unescaped (audit A-3).

import { html, safeUrl } from '../core/html.js';
import { app } from '../core/state.js';
import { symColor } from '../charts/common.js';

// Attributes that turn a [data-sym] element into a real control. One delegated
// handler opens the drawer, so each trigger has to carry its own role, tab stop
// and name. Table rows are the exception: a <tr> keeps its row semantics and
// gets a .sym-open button in its first cell instead.
export function symTrigger(sym, label) {
  return html`data-sym="${sym}" role="button" tabindex="0" aria-label="${label || 'Open ' + sym + ' details'}"`;
}

export function symOpenBtn(sym, inner) {
  return html`<button type="button" class="sym-open" aria-label="${'Open ' + sym + ' details'}">${inner}</button>`;
}

// A ticker whose name is just the ticker - VOO, GLD - stacked the same letters
// twice and read as a rendering fault. Emit the subtitle only when it says
// something the symbol does not.
export function subName(sym, name, kind) {
  const n = (name == null ? '' : String(name)).trim();
  if (!n || n.toUpperCase() === String(sym).toUpperCase()) return html``;
  if (kind === 'div') return html`<div class="nm">${n}</div>`;
  if (kind === 'muted') return html`<span style="font-size:var(--fs-micro);color:var(--muted)">${n}</span>`;
  return html`<span>${n}</span>`;
}

const CDN = 'https://assets.parqet.com/logos/symbol/';

/** The logo URL for a symbol, or null. The self-hosted data URI from the
 *  payload (fetch_ticker_logos.py) needs no request. The public CDN discloses
 *  the symbol to a third party, so it is used only when the operator opted in
 *  (config.logo_cdn) - a fresh install with no logo cache otherwise leaked its
 *  whole holdings list (audit A-5). */
export function logoUrl(sym) {
  const cached = safeUrl((app.D.logos || {})[sym], { allowDataImage: true });
  if (cached) return cached;
  if (app.cfg.logoCdn) return CDN + encodeURIComponent(sym) + '?format=png&size=64';
  return null;
}

// Ticker logo over the letter badge. A logo that fails to load is removed by
// the delegated error listener in main.js, leaving the letters underneath -
// no inline onerror attribute, so the page carries no event-handler markup.
export function symBadge(sym) {
  const s = String(sym);
  const url = logoUrl(s);
  return html`<span class="sym-badge" style="background:${symColor(s)}">${s.replace('.B', '').slice(0, 4)}${url ? html`<img class="sym-logo" loading="lazy" referrerpolicy="no-referrer" alt="" src="${url}"/>` : ''}</span>`;
}
