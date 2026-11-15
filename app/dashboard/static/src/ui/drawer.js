// The side drawer: a symbol's dossier, or a KPI's evidence. Also owns the
// delegated click/keyboard handlers that open it from any [data-sym] or
// [data-kpi] trigger, and the focus trap shared with the command palette.

import { $, $all, trapTab } from '../core/dom.js';
import { html, setHTML, safeUrl } from '../core/html.js';
import { app, get, holdingRows, totals, lotTotal } from '../core/state.js';
import * as F from '../core/format.js';
import { sparkSVG, nextId } from '../charts/common.js';
import { X as X_ICON } from './icons.js';
import { symBadge } from './symbols.js';
import { kpiEvidence } from './evidence.js';
import { spanMs, lotMarkers } from '../views/price-chart.js';

let el, scrim, hd, bd, foot, prevFocus = null;
const hooks = { switchView: () => {}, openFundamentalsFor: () => {}, isPaletteOpen: () => false };
export function setDrawerHooks(h) {
  hooks.switchView = h.switchView;
  hooks.openFundamentalsFor = h.openFundamentalsFor;
  hooks.isPaletteOpen = h.isPaletteOpen;
}

export function isOpen() { return Boolean(el && el.classList.contains('open')); }

function show(label) {
  el.setAttribute('aria-label', label);
  prevFocus = document.activeElement;
  el.classList.add('open'); scrim.classList.add('show');
  bd.scrollTop = 0;
  el.focus();
}
export function closeDrawer() {
  if (!isOpen()) return;
  el.classList.remove('open'); scrim.classList.remove('show');
  if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (e) { /* element gone */ } }
}

function chartHTML(sym) {
  const series = ((app.D.priceHist || {})[sym]) || [];
  const s = get(sym);
  if (series.length < 2) {
    return (s && s.hist && s.hist.length > 1) ? sparkSVG(s.hist, s.hist[s.hist.length - 1] >= s.hist[0], 440, 110) : null;
  }
  const now = series[series.length - 1][0];
  let data = series.filter((p) => p[0] >= now - spanMs('3M'));
  if (data.length < 2) data = series.slice();
  const W = 440, H = 140, padT = 10, padB = 12, padL = 4, padR = 4;
  const xs = data.map((p) => p[0]), ys = data.map((p) => p[1]);
  const xmin = xs[0], xmax = xs[xs.length - 1], xr = (xmax - xmin) || 1;
  const ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys), yr = (ymax - ymin) || 1;
  const X = (t) => padL + (t - xmin) / xr * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - ymin) / yr) * (H - padT - padB);
  const up = ys[ys.length - 1] >= ys[0], color = up ? 'var(--up)' : 'var(--down)';
  const line = data.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join(' ');
  const area = line + ' L' + X(xmax).toFixed(1) + ',' + (H - padB) + ' L' + X(xmin).toFixed(1) + ',' + (H - padB) + ' Z';
  const id = nextId('dg');
  const chg = ys[0] ? (ys[ys.length - 1] - ys[0]) / ys[0] * 100 : 0;
  // The chart behind a cost basis carries its scale at its bounds: the span in
  // dates, the extent in prices - what a trade marker is read against.
  return html`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:140px;display:block" role="img" aria-label="${sym + ' price, last 3 months, ' + F.pct(chg) + ', low ' + F.money(ymin) + ', high ' + F.money(ymax)}"><defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".18"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><path d="${area}" fill="url(#${id})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>${lotMarkers(((app.D.symLots || {})[sym]) || [], X, Y, xmin, xmax, 6)}</svg><div class="dc-x"><span>${F.fmtDate(xmin, 'day')}</span><span>${F.fmtDate(xmax, 'day')}</span></div><div class="dc-foot"><span class="num" style="color:${color}">${F.pct(chg)} over 3 months</span><span class="dc-hl num">${'low ' + F.money(ymin) + ' · high ' + F.money(ymax)}</span></div>`;
}

function box(l, v, cls) { return html`<div><div class="l">${l}</div><div class="v${cls ? ' ' + cls : ''}">${v}</div></div>`; }

export function openDrawer(sym) {
  const s = get(sym);
  if (!s || !el) return;
  const h = (app.D.holdings || []).find((x) => x.sym === sym) || null;
  setHTML(hd, html`${symBadge(sym)}<span class="nm"><b>${sym}</b><span>${(s.name || sym) + ' · ' + (s.sector || '—')}</span></span><button type="button" class="iconbtn drawer__close" data-drawer-close aria-label="Close">${X_ICON}</button>`);
  const parts = [html`<div style="display:flex;align-items:baseline;gap:12px"><span class="drawer__px">${F.money(s.price)}</span><span class="num ${F.chgCls(s.dayPct)}" style="font-size:var(--fs-base);font-weight:600">${F.pct(s.dayPct)} today</span></div>`];
  if (h) {
    const mktVal = s.price * h.qty, cost = h.avgCost * h.qty, gl = mktVal - cost, glPct = cost ? gl / cost * 100 : 0;
    const totalVal = totals(holdingRows()).mktVal + app.cash;
    parts.push(html`<div class="drawer__stats">${box('Quantity', F.qty(h.qty))}${box('Avg cost', html`${F.money(h.avgCost)} <span style="font-size:var(--fs-micro);color:var(--muted);font-family:var(--font)">${app.D.engine || 'FIFO'}</span>`)}${box('Market value', F.money(mktVal))}${box('Cost basis', F.money(cost))}${box('Unrealized', html`${(gl >= 0 ? '+' : '−') + F.money(Math.abs(gl))} <span style="font-size:var(--fs-micro)">${F.pct(glPct)}</span>`, gl >= 0 ? 'up' : 'down')}${box('Weight', totalVal > 0 ? (mktVal / totalVal * 100).toFixed(1) + '%' : '—')}</div>`);
  } else {
    parts.push(html`<div style="margin-top:12px;font-size:var(--fs-sm);color:var(--muted)">Watchlist symbol — no open position.</div>`);
  }
  const chart = chartHTML(sym);
  if (chart) parts.push(html`<div class="drawer__sect">Price · 3M <span class="n">your trades marked</span></div>${chart}`);
  const symAll = (app.D.histLots || []).filter((l) => l.symbol === sym);
  const lots = symAll.slice(0, 12);
  if (lots.length) {
    // Name the whole, so a subset is never mistaken for every lot.
    const total = Math.max(lotTotal(sym), symAll.length);
    const lotLabel = total > lots.length ? 'showing ' + lots.length + ' of ' + total : lots.length + (lots.length === 1 ? ' lot' : ' lots');
    parts.push(html`<div class="drawer__sect">Lots <span class="n">${lotLabel}</span></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Date</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead><tbody>${lots.map((l) => html`<tr><td class="num" style="text-align:left">${l.date}</td><td><span class="tag ${l.side === 'BUY' ? 'tag--up' : 'tag--down'}">${l.side}</span></td><td class="num">${F.qty(l.qty)}</td><td class="num">${F.money(l.price)}</td></tr>`)}</tbody></table></div>`);
  }
  const news = (app.D.news || []).filter((n) => n.sym === sym).slice(0, 5);
  if (news.length) {
    parts.push(html`<div class="drawer__sect">News</div><div class="drawer__news">${news.map((n) => {
      const url = safeUrl(n.url);
      return html`<div style="padding:8px 0;border-bottom:1px solid var(--border)"><div style="font-size:var(--fs-sm);font-weight:600;line-height:1.4">${url ? html`<a href="${url}" target="_blank" rel="noopener noreferrer">${n.title}</a>` : n.title}</div><div style="font-size:var(--fs-micro);color:var(--muted);margin-top:3px">${(n.src || '') + ' · ' + (n.time || '')}</div></div>`;
    })}</div>`);
  }
  setHTML(bd, html`${parts}`);
  setHTML(foot, html`<button type="button" class="btn btn--ghost" data-drawer-close>Close</button>${symAll.length > lots.length ? html`<button type="button" class="btn btn--ghost" data-drawer-history>All lots →</button>` : ''}<button type="button" class="btn" data-drawer-fd>Full fundamentals →</button>`);
  const histBtn = $('[data-drawer-history]', foot);
  if (histBtn) histBtn.addEventListener('click', () => { closeDrawer(); hooks.switchView('history'); });
  $all('[data-drawer-close]', el).forEach((b) => b.addEventListener('click', closeDrawer));
  const fdBtn = $('[data-drawer-fd]', foot);
  if (fdBtn) fdBtn.addEventListener('click', () => { closeDrawer(); hooks.openFundamentalsFor(sym); });
  show(sym + ' details');
}

export function openEvidence(key) {
  const ev = kpiEvidence(key, {
    goHoldings: () => {
      closeDrawer();
      const t = $('#holdings-tbl') || $('#holdings-cards');
      if (t && t.scrollIntoView) t.scrollIntoView({ block: 'center' });
    },
    goView: (v) => { closeDrawer(); hooks.switchView(v); },
  });
  if (!ev || !el) return;
  setHTML(hd, html`<span class="nm"><b>${ev.title}</b><span class="ev-hd-sub">how this is calculated</span></span><span class="drawer__px num">${ev.value}</span><button type="button" class="iconbtn drawer__close" data-drawer-close aria-label="Close">${X_ICON}</button>`);
  // The rows are a decomposition too, so they get the same reconciliation note
  // when their printed values do not sum to the printed total.
  setHTML(bd, html`<p class="ev-what">${ev.what}</p>${ev.eq || ''}${ev.note ? html`<p class="ev-note">${ev.note}</p>` : ''}${ev.table ? html`<div class="drawer__sect">Where it comes from</div>${ev.table}${ev.tableNote || ''}` : ''}`);
  setHTML(foot, html`<button type="button" class="btn btn--ghost" data-drawer-close>Close</button>${ev.route ? html`<button type="button" class="btn btn--primary" data-ev-route>${ev.route.label} →</button>` : ''}`);
  $all('[data-drawer-close]', el).forEach((b) => b.addEventListener('click', closeDrawer));
  const rb = $('[data-ev-route]', foot);
  if (rb && ev.route) rb.addEventListener('click', ev.route.run);
  show(ev.title + ' — how this is calculated');
}

export function initDrawer() {
  el = $('[data-drawer]'); scrim = $('[data-drawer-scrim]');
  hd = $('[data-drawer-hd]'); bd = $('[data-drawer-bd]'); foot = $('[data-drawer-foot]');
  if (scrim) scrim.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isOpen() && !hooks.isPaletteOpen()) closeDrawer();
    if (e.key === 'Tab' && isOpen() && !hooks.isPaletteOpen()) trapTab(el, e);
  });
  // Delegate: any element carrying data-sym opens the dossier. Controls inside
  // a trigger keep their own behaviour - except .sym-open, which exists to be
  // a table row's keyboard-operable equivalent.
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    const ctl = t.closest('a,button,input,select,label');
    if (ctl && !ctl.classList.contains('sym-open')) return;
    if (t.closest('[data-drawer]')) return;
    const kpiEl = t.closest('[data-kpi]');
    if (kpiEl) { openEvidence(kpiEl.getAttribute('data-kpi')); return; }
    const symEl = t.closest('[data-sym]');
    if (!symEl) return;
    const sym = symEl.getAttribute('data-sym');
    if (get(sym)) openDrawer(sym);
  });
  // Enter/Space on a focused div trigger (buttons activate natively).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const t = e.target;
    if (!t || !t.getAttribute || t.getAttribute('role') !== 'button') return;
    const kpiKey = t.getAttribute('data-kpi');
    if (kpiKey) { e.preventDefault(); openEvidence(kpiKey); return; }
    const sym = t.getAttribute('data-sym');
    if (!sym || !get(sym)) return;
    e.preventDefault();
    openDrawer(sym);
  });
}
