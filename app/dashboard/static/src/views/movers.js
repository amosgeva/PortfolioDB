// Market Movers: breadth bar, gainers/losers and the sector heatmap.

import { $, $all, setText } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { list } from '../core/state.js';
import * as F from '../core/format.js';
import { symTrigger, subName } from '../ui/symbols.js';
import { isDark } from '../ui/theme.js';

const TOP_N = 6;

/**
 * Gainers and losers, filtered by sign BEFORE slicing. Without the filter a
 * short list is padded from the other direction, so "Top gainers" printed
 * losers beneath a breadth bar saying how many symbols advanced. The MCP's
 * get_top_movers had the same bug (audit X-7); this is the dashboard's copy,
 * kept pure so a test pins it.
 */
export function splitMovers(stocks, n) {
  const k = n || TOP_N;
  // Unknown day change (null) is excluded - you cannot rank what you cannot measure.
  const all = stocks.filter((s) => s.dayPct != null && (s.dayPct !== 0 || (s.hist || []).length > 1));
  return {
    gainers: all.filter((s) => s.dayPct > 0).sort((a, b) => b.dayPct - a.dayPct).slice(0, k),
    losers: all.filter((s) => s.dayPct < 0).sort((a, b) => a.dayPct - b.dayPct).slice(0, k),
  };
}

// Sign-and-size, composed per theme: light goes pale and takes ink, dark goes
// deep and takes white, so a tile's label is readable across the whole scale.
function heatColor(p) {
  const m = Math.min(Math.abs(p) / 3.2, 1);
  if (p == null || Math.abs(p) < 0.08) return isDark() ? 'oklch(38% 0.012 260)' : 'oklch(72% 0.015 260)';
  const hue = p >= 0 ? 152 : 26;
  return isDark()
    ? 'oklch(' + (52 - m * 16).toFixed(1) + '% ' + (0.06 + m * 0.13).toFixed(3) + ' ' + hue + ')'
    : 'oklch(' + (80 - m * 16).toFixed(1) + '% ' + (0.04 + m * 0.16).toFixed(3) + ' ' + hue + ')';
}

function moverRow(s, rank) {
  return html`<div class="mv" ${symTrigger(s.sym)}><span class="mv__rank">${rank}</span><span class="mv__sym"><b>${s.sym}</b>${subName(s.sym, s.name)}</span><span class="mv__px">${F.money(s.price)}</span><span class="mv__chg ${F.chgCls(s.dayPct)}">${F.pct(s.dayPct)}</span></div>`;
}
function moverList(rows, word) {
  if (!rows.length) return html`<div class="empty">No symbol ${word} today.</div>`;
  // Say when a list is short because the day was, not because it was cut.
  const note = rows.length < TOP_N
    ? html`<div class="empty" style="padding:12px 6px">Only ${rows.length} symbol${rows.length > 1 ? 's' : ''} ${word} today.</div>` : '';
  return html`${rows.map((s, i) => moverRow(s, i + 1))}${note}`;
}

export function renderMovers() {
  const { gainers, losers } = splitMovers(list());
  setHTML($('#gainers'), moverList(gainers, 'advanced'));
  setHTML($('#losers'), moverList(losers, 'declined'));
}

const heatState = { sector: 'All', query: '' };
function sectors() {
  const set = {};
  list().forEach((s) => { const key = s.sector || 'Other'; set[key] = 1; });
  return Object.keys(set).sort((a, b) => a.localeCompare(b));
}
function tileHTML(s) {
  return html`<div class="heat__tile" ${symTrigger(s.sym, s.sym + ' ' + F.pct(s.dayPct) + ' — open details')} style="background:${heatColor(s.dayPct)}" title="${(s.name || s.sym) + ' · ' + F.money(s.price)}"><div><b>${s.sym}</b>${subName(s.sym, s.name, 'div')}</div><div class="pc">${F.pct(s.dayPct)}</div></div>`;
}
function heatVisible(s) {
  if (heatState.sector !== 'All' && (s.sector || 'Other') !== heatState.sector) return false;
  if (heatState.query) {
    const q = heatState.query.toLowerCase();
    if (String(s.sym).toLowerCase().indexOf(q) < 0 && String(s.name || '').toLowerCase().indexOf(q) < 0) return false;
  }
  return true;
}
export function renderHeat() {
  const host = $('#heat');
  if (!host) return;
  const groups = sectors().map((sec) => {
    const items = list().filter((s) => (s.sector || 'Other') === sec && heatVisible(s));
    if (!items.length) return null;
    const known = items.filter((s) => s.dayPct != null);
    const avg = known.length ? known.reduce((a, s) => a + s.dayPct, 0) / known.length : 0;
    items.sort((a, b) => (b.dayPct == null ? -Infinity : b.dayPct) - (a.dayPct == null ? -Infinity : a.dayPct));
    return html`<div class="heat__sector-title">${sec}<span class="num ${avg >= 0 ? 'up' : 'down'}" style="font-weight:600">${F.pct(avg)}</span><span style="color:var(--faint);font-weight:400;font-size:var(--fs-micro)">${items.length} names</span></div>${items.map(tileHTML)}`;
  }).filter(Boolean);
  setHTML(host, groups.length ? html`${groups}` : html`<div class="empty">No symbols match your filter.</div>`);
}

function renderChips() {
  const host = $('#sector-chips');
  if (!host) return;
  const chips = ['All'].concat(sectors());
  setHTML(host, html`${chips.map((c) => html`<button type="button" class="chip ${c === heatState.sector ? 'is-active' : ''}" data-sector="${c}" aria-pressed="${String(c === heatState.sector)}">${c}</button>`)}`);
  $all('[data-sector]', host).forEach((b) => b.addEventListener('click', () => {
    heatState.sector = b.getAttribute('data-sector'); renderChips(); renderHeat();
  }));
}

function renderBreadth() {
  const all = list();
  const adv = all.filter((s) => s.dayPct > 0).length, dec = all.filter((s) => s.dayPct < 0).length, unch = all.length - adv - dec;
  const advPct = all.length ? (adv / all.length * 100) : 0;
  setText($('#breadth-sub'), 'advancers vs decliners · ' + all.length + ' names tracked');
  setHTML($('#breadth-bar'), html`<div style="display:flex;height:10px;border-radius:99px;overflow:hidden;background:var(--surface-3)" role="img" aria-label="${adv + ' advancing, ' + unch + ' flat, ' + dec + ' declining'}"><div style="width:${advPct.toFixed(2)}%;background:var(--up)"></div><div style="flex:1;background:var(--down)"></div></div>`);
  setHTML($('#breadth-legend'), html`<span class="up">▲ ${adv} advancing</span><span style="color:var(--muted)">● ${unch} flat</span><span class="down">▼ ${dec} declining</span>`);
}

export function initMovers() {
  renderMovers(); renderChips(); renderHeat(); renderBreadth();
  const search = $('#mv-search');
  if (search) search.addEventListener('input', () => { heatState.query = search.value.trim(); renderHeat(); });
}
