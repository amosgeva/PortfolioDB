// Portfolio view: market overview, returns strip, allocation, holdings,
// attribution and the net P&L waterfall.

import { $, $all, activate, bySym, clickable, setText, prefersReducedMotion } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app, list, holdingRows, totals } from '../core/state.js';
import * as F from '../core/format.js';
import { sparkSVG, symColor, onFill, CAT, CAT_OTHER, CAT_CASH } from '../charts/common.js';
import { symBadge, symOpenBtn, symTrigger, subName } from '../ui/symbols.js';

// ---- market overview strip ----------------------------------------------
// DOM nodes, not markup: the labels come from a free-text setting. Index levels
// are plain numbers, not money - VIX is not 14.51 of any currency.
export function renderMarkets() {
  const card = $('#markets-card'), host = $('#markets-strip');
  if (!card || !host) return;
  const rows = Array.isArray(app.D.markets) ? app.D.markets : [];
  if (!rows.length) { card.hidden = true; return; }
  card.hidden = false;
  host.textContent = '';
  let freshest = null;
  const wrap = document.createElement('div');
  wrap.className = 'mkt-grid';
  rows.forEach((r) => {
    if (r.ts && (!freshest || r.ts > freshest)) freshest = r.ts;
    const up = r.pct != null && r.pct >= 0;
    const cell = document.createElement('div'); cell.className = 'mkt';
    const top = document.createElement('div'); top.className = 'mkt__top';
    const name = document.createElement('b'); name.textContent = r.label; top.appendChild(name);
    const p = document.createElement('span');
    p.className = 'mkt__pct num ' + (r.pct == null ? 'muted' : (up ? 'up' : 'down'));
    // No snapshot yet is said, not shown as 0.00%.
    p.textContent = r.pct == null ? 'no data yet' : (up ? '↗ ' : '↘ ') + F.pct(r.pct);
    top.appendChild(p);
    cell.appendChild(top);
    const bottom = document.createElement('div'); bottom.className = 'mkt__bot';
    const px = document.createElement('span'); px.className = 'num'; px.textContent = F.num(r.price, 2);
    bottom.appendChild(px);
    const chg = document.createElement('span');
    chg.className = 'num ' + (r.change == null ? 'muted' : (up ? 'up' : 'down'));
    chg.textContent = r.change == null ? '' : (r.change >= 0 ? '+' : '−') + F.num(Math.abs(r.change), 2);
    bottom.appendChild(chg);
    cell.appendChild(bottom);
    const spark = document.createElement('div'); spark.className = 'mkt__spark';
    if ((r.hist || []).length > 1) setHTML(spark, sparkSVG(r.hist, up, 240, 46));
    cell.appendChild(spark);
    wrap.appendChild(cell);
  });
  host.appendChild(wrap);
  const t = freshest ? Date.parse(String(freshest)) : NaN;
  setText($('#markets-sub'), 'context, not holdings' +
    (freshest ? ' · as of ' + (Number.isNaN(t) ? String(freshest) : F.fmtDate(t, 'fullTime')) : ' · awaiting first collection'));
}

// ---- returns strip (multi-period, vs the configured benchmark) -----------
export function renderReturns() {
  const R = app.D.returns || {};
  const host = $('#returns-strip');
  if (!host) return;
  const periods = R.periods || [];
  const label = app.cfg.benchmarkLabel;
  // benchmarkAvailable === false: the server has no history for the configured
  // benchmark, so its column is empty for a reason the reader should see.
  const missing = R.benchmarkAvailable === false;
  setText($('#returns-sub'), (R.basis ? R.basis + ' · ' : '') + 'benchmark ' + (R.benchmark || label) +
    (missing ? ' · no ' + label + ' price history collected yet' : ''));
  if (!periods.some((p) => p.portfolio != null)) {
    setHTML(host, html`<div class="empty">Not enough snapshot history for period returns yet.</div>`);
    return;
  }
  setHTML(host, html`<div style="display:flex;gap:10px;flex-wrap:wrap">${periods.map((p) => {
    const hasP = p.portfolio != null, dir = hasP ? p.portfolio : 0;
    const col = dir > 0 ? 'var(--up)' : dir < 0 ? 'var(--down)' : 'var(--muted)';
    return html`<div class="stat-mini stat-mini--well" style="min-width:96px"><div class="l">${p.period}</div><div class="v" style="color:${col}">${hasP ? F.pct(p.portfolio) : '—'}</div><div class="l" style="margin-top:4px">${label} ${p.benchmark != null ? F.pct(p.benchmark) : '—'}</div></div>`;
  })}</div>`);
}

// ---- allocation donut / treemap -----------------------------------------
let allocDim = 'position', allocMode = 'donut';
const ALLOC_PALETTE = CAT.slice(0, 9);

function treemapHTML(slices, total) {
  // weight-proportional treemap (two greedy strips): tile area = weight
  const rows = [[], []], sums = [0, 0];
  slices.forEach((s) => { const i = sums[0] <= sums[1] ? 0 : 1; rows[i].push(s); sums[i] += s.val; });
  if (!rows[1].length) { rows.pop(); sums.pop(); }
  return html`<div style="display:flex;flex-direction:column;gap:4px;width:100%" role="img" aria-label="Allocation treemap, tile size is portfolio weight">${rows.map((row, ri) => {
    const h = Math.max(64, Math.round(176 * sums[ri] / total));
    return html`<div style="display:flex;gap:4px;height:${h}px">${row.map((s) => {
      const p = s.val / total * 100;
      return html`<div title="${s.label + ' · ' + F.money(s.val) + ' · ' + p.toFixed(1) + '%'}" style="flex:${s.val.toFixed(2)} 1 0;min-width:0;background:${s.color};border-radius:var(--r-sm);color:${onFill(s.color)};padding:8px 9px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden"><b style="font-size:var(--fs-meta);white-space:nowrap;text-overflow:ellipsis;overflow:hidden">${s.label}</b><span class="num" style="font-size:var(--fs-micro)">${p.toFixed(1)}%</span></div>`;
    })}</div>`;
  })}</div>`;
}

export function renderAlloc() {
  const host = $('#alloc');
  if (!host) return;
  let slices, total;
  if (allocDim === 'position') {
    const rs = holdingRows().slice().sort((a, b) => b.mktVal - a.mktVal);
    total = totals(rs).mktVal + app.cash;
    if (total <= 0) { setHTML(host, html`<div class="empty">No positions to allocate.</div>`); return; }
    const top = rs.slice(0, 6);
    const otherVal = rs.slice(6).reduce((a, r) => a + r.mktVal, 0);
    // symColor keeps a symbol's colour stable across views, but six symbols in
    // nine buckets collide often: walk to the next free step when they do.
    const taken = {};
    slices = top.map((r) => {
      let c = symColor(r.sym);
      for (let g = 0; taken[c] && g < 9; g++) c = CAT[(CAT.indexOf(c) + 1) % 9];
      taken[c] = 1;
      return { label: r.sym, val: r.mktVal, color: c };
    });
    if (otherVal > 0) slices.push({ label: 'Other', val: otherVal, color: CAT_OTHER });
    if (app.cash > 0) slices.push({ label: 'Cash', val: app.cash, color: CAT_CASH });
  } else {
    const rows = ((app.D.alloc || {})[allocDim]) || [];
    const otherR = rows.slice(7).reduce((a, r) => a + r.value, 0);
    slices = rows.slice(0, 7).map((r, i) => ({ label: r.key, val: r.value, color: ALLOC_PALETTE[i % ALLOC_PALETTE.length] }));
    if (otherR > 0) slices.push({ label: 'Other', val: otherR, color: CAT_OTHER });
    total = slices.reduce((a, s) => a + s.val, 0);
    if (total <= 0) { setHTML(host, html`<div class="empty">No allocation data for this view.</div>`); return; }
  }
  if (allocMode === 'map') { setHTML(host, treemapHTML(slices, total)); return; }
  const C = 2 * Math.PI * 54;
  // Every step of the ramp sits at one lightness, so the boundary between two
  // arcs is a gap rather than a hue change.
  const GAP = 3;
  let off = 0;
  const ring = slices.map((s) => {
    const frac = s.val / total, dash = Math.max(1.5, frac * C - GAP);
    const seg = html`<circle cx="80" cy="80" r="54" fill="none" stroke="${s.color}" stroke-width="20" stroke-dasharray="${dash.toFixed(2)} ${(C - dash).toFixed(2)}" stroke-dashoffset="${(-off * C).toFixed(2)}" transform="rotate(-90 80 80)"/>`;
    off += frac;
    return seg;
  });
  const legend = slices.map((s) => html`<div style="display:flex;align-items:center;gap:8px;font-size:var(--fs-sm);padding:3px 0"><span style="width:9px;height:9px;border-radius:2px;background:${s.color}"></span><span style="flex:1">${s.label}</span><span class="num" style="color:var(--muted)">${(s.val / total * 100).toFixed(1)}%</span></div>`);
  setHTML(host, html`<div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap"><div style="position:relative;flex:0 0 auto"><svg width="160" height="160" viewBox="0 0 160 160" role="img" aria-label="${'Allocation donut, total ' + F.compact(total)}">${ring}</svg><div style="position:absolute;inset:0;display:grid;place-items:center;text-align:center"><div><div class="num" style="font-size:var(--fs-xl);font-weight:600">${F.compact(total)}</div><div style="font-size:var(--fs-micro);color:var(--muted);letter-spacing:.04em">TOTAL</div></div></div></div><div style="flex:1;min-width:150px">${legend}</div></div>`);
}

// ---- holdings table + phone cards ----------------------------------------
const sortState = { key: 'mktVal', dir: -1 };

function rowHTML(r) {
  // null dayPct → neutral tag; spark colour falls back to the hist trend
  const dUp = r.dayPct != null ? r.dayPct >= 0 : r.hist[r.hist.length - 1] >= r.hist[0];
  const dTag = r.dayPct == null ? '' : (r.dayPct >= 0 ? 'tag--up' : 'tag--down');
  const gUp = r.gl >= 0;
  return html`<tr data-sym="${r.sym}"><td>${symOpenBtn(r.sym, html`<span class="sym-cell">${symBadge(r.sym)}<span class="nm"><b>${r.sym}</b>${subName(r.sym, r.name)}</span></span>`)}</td><td class="price">${F.money(r.price)}</td><td><span class="tag ${dTag}">${F.pct(r.dayPct)}</span></td><td class="num">${F.qty(r.qty)}</td><td class="num">${F.money(r.mktVal)}</td><td class="num" style="color:var(--muted)">${F.money(r.avgCost)}</td><td class="num ${gUp ? 'up' : 'down'}">${(gUp ? '+' : '−') + F.money(Math.abs(r.gl))} <span style="font-size:var(--fs-micro)">${F.pct(r.glPct)}</span></td><td class="spark-cell">${sparkSVG(r.hist, dUp)}</td></tr>`;
}
function hcardHTML(r) {
  const dTag = r.dayPct == null ? '' : (r.dayPct >= 0 ? 'tag--up' : 'tag--down');
  const gUp = r.gl >= 0;
  return html`<div class="hcard" ${symTrigger(r.sym)}><div class="hcard__top">${symBadge(r.sym)}<span class="nm" style="display:flex;flex-direction:column;line-height:1.25"><b>${r.sym}</b>${subName(r.sym, r.name, 'muted')}</span><span class="tag ${dTag}">${F.pct(r.dayPct)}</span></div><div class="hcard__row"><span class="mv">${F.money(r.mktVal)}</span><span class="num ${gUp ? 'up' : 'down'}">${(gUp ? '+' : '−') + F.money(Math.abs(r.gl))} (${F.pct(r.glPct)})</span></div><div class="hcard__meta">${F.qty(r.qty) + ' sh · avg ' + F.money(r.avgCost) + ' · now ' + F.money(r.price)}</div></div>`;
}

export function renderTable() {
  const rs = holdingRows();
  const k = sortState.key, dir = sortState.dir;
  rs.sort((a, b) => {
    const x = a[k], y = b[k];
    if (typeof x === 'string') return String(x).localeCompare(String(y)) * dir;
    return ((x == null ? -Infinity : x) - (y == null ? -Infinity : y)) * dir || 0;
  });
  setHTML($('#holdings'), rs.length ? html`${rs.map(rowHTML)}` : html`<tr><td colspan="8"><div class="empty">No open positions.</div></td></tr>`);
  setHTML($('#holdings-cards'), rs.length ? html`${rs.map(hcardHTML)}` : html`<div class="empty">No open positions.</div>`);
  // The engine is named on the table it produced.
  setText($('#holdings-sub'), rs.length + ' position' + (rs.length === 1 ? '' : 's') + ' · ' +
    (app.D.engine || 'FIFO') + ' cost basis · click a column to sort');
  $all('#holdings-tbl thead th').forEach((th) => {
    const sorted = th.dataset.key === k;
    th.classList.toggle('sorted', sorted);
    if (th.dataset.key) th.setAttribute('aria-sort', sorted ? (dir < 0 ? 'descending' : 'ascending') : 'none');
    const ar = th.querySelector('.arrow');
    if (ar) ar.textContent = sorted ? (dir < 0 ? '▼' : '▲') : '';
  });
}
function defaultDir(key) { return (key === 'sym' || key === 'name') ? 1 : -1; }

export function initHoldings() {
  $all('#holdings-tbl thead th[data-key]').forEach((th) => clickable(th, () => {
    const key = th.dataset.key;
    if (sortState.key === key) sortState.dir *= -1; else { sortState.key = key; sortState.dir = defaultDir(key); }
    renderTable(); syncHcSort();
  }));
  // Below 640px the table becomes cards, which took the sort controls with it.
  const hcSort = $('#hc-sort'), hcDir = $('[data-hc-dir]');
  function syncHcSort() {
    if (hcSort) hcSort.value = sortState.key;
    if (hcDir) {
      hcDir.textContent = sortState.dir === 1 ? '↑' : '↓';
      hcDir.title = sortState.dir === 1 ? 'Ascending — tap for descending' : 'Descending — tap for ascending';
    }
  }
  if (hcSort) hcSort.addEventListener('change', () => { sortState.key = hcSort.value; sortState.dir = defaultDir(sortState.key); renderTable(); syncHcSort(); });
  if (hcDir) hcDir.addEventListener('click', () => { sortState.dir *= -1; renderTable(); syncHcSort(); });
  syncHcSort();
}

// Terminal-style price flash on cells whose price moved since the last visit
// (previous prices kept per viewer in localStorage).
export function flashChangedPrices() {
  try {
    const prev = JSON.parse(localStorage.getItem('pdb_prices') || '{}');
    const cur = {};
    list().forEach((s) => { cur[s.sym] = s.price; });
    if (!prefersReducedMotion()) {
      Object.keys(cur).forEach((sym) => {
        if (prev[sym] == null || prev[sym] === cur[sym]) return;
        const row = bySym($('#holdings'), 'tr[data-sym]', sym);
        const cell = row && row.querySelector('td.price');
        if (cell) cell.classList.add(cur[sym] > prev[sym] ? 'flash-up' : 'flash-down');
      });
    }
    localStorage.setItem('pdb_prices', JSON.stringify(cur));
  } catch (e) { /* storage unavailable: no flash on the next load */ }
}

// ---- P&L attribution + waterfall ----------------------------------------
let attrMode = 'day';
export function renderAttribution() {
  const host = $('#attr-chart');
  if (!host) return;
  const rs = holdingRows().map((r) => ({ sym: r.sym, val: attrMode === 'day' ? (r.dayPct == null ? null : r.day * r.qty) : r.gl }))
    .filter((r) => r.val != null);
  if (!rs.length) { setHTML(host, html`<div class="empty">No P&amp;L data yet.</div>`); return; }
  rs.sort((a, b) => b.val - a.val);
  const max = Math.max.apply(null, rs.map((r) => Math.abs(r.val))) || 1;
  setHTML(host, html`${rs.map((r) => {
    const w = Math.max(1.5, Math.abs(r.val) / max * 50);
    const up = r.val >= 0;
    return html`<div class="attr-row" ${symTrigger(r.sym)} title="${'Open ' + r.sym}"><span class="attr-sym">${r.sym}</span><span class="attr-track"><span class="attr-bar ${up ? 'up' : 'down'}" style="${(up ? 'left:50%;' : 'right:50%;') + 'width:' + w.toFixed(1) + '%'}"></span></span><span class="attr-val num ${up ? 'up' : 'down'}">${(up ? '+' : '−') + F.money(Math.abs(r.val))}</span></div>`;
  })}`);
}

export function renderWaterfall() {
  const host = $('#waterfall');
  if (!host) return;
  const k = app.D.kpi || {};
  const steps = [
    { label: 'Unrealized', val: k.unrealized || 0 },
    { label: 'Realized', val: k.realized || 0 },
    { label: 'Dividends', val: k.dividends || 0 },
    { label: 'Fees', val: -(k.totalFees || 0) },
  ];
  let cum = 0;
  const nodes = steps.map((s) => { const y0 = cum; cum += s.val; return { label: s.label, val: s.val, y0, y1: cum, net: false }; });
  nodes.push({ label: 'Net P&L', val: cum, y0: 0, y1: cum, net: true });
  let lo = 0, hi = 0;
  nodes.forEach((nd) => { lo = Math.min(lo, nd.y0, nd.y1); hi = Math.max(hi, nd.y0, nd.y1); });
  if (hi - lo < 0.01) { setHTML(host, html`<div class="empty">No P&amp;L recorded yet.</div>`); return; }
  const W = 440, H = 220, padT = 20, padB = 34, span = hi - lo;
  const Y = (v) => padT + (hi - v) / span * (H - padT - padB);
  const slot = W / nodes.length, bw = slot * 0.54;
  // Name the values, not just the chart.
  const wfLabel = 'Net P&L waterfall. ' + nodes.map((nd) => nd.label + ' ' + (nd.val >= 0 ? 'plus ' : 'minus ') + F.money(Math.abs(nd.val))).join(', ') + '.';
  const parts = nodes.map((nd, i) => {
    const x = i * slot + (slot - bw) / 2;
    const yTop = Y(Math.max(nd.y0, nd.y1)), yBot = Y(Math.min(nd.y0, nd.y1));
    const hgt = Math.max(1.5, yBot - yTop);
    // Sign-coloured like the rest; the total is marked by being anchored to
    // zero, drawn at full strength, behind a separating rule.
    const color = nd.val >= 0 ? 'var(--up)' : 'var(--down)';
    let vy = yTop - 5; if (vy < 12) vy = yBot + 12;
    const dx = (x - (slot - bw) / 2 + 1).toFixed(1);
    const cy = Y(nd.y1).toFixed(1);
    return html`<rect x="${x.toFixed(1)}" y="${yTop.toFixed(1)}" width="${bw.toFixed(1)}" height="${hgt.toFixed(1)}" rx="3" fill="${color}"${nd.net ? '' : html` opacity=".85"`}><title>${nd.label + ' ' + F.money(nd.val)}</title></rect>${nd.net ? html`<line x1="${dx}" x2="${dx}" y1="${padT}" y2="${H - padB + 6}" stroke="var(--border-2)" stroke-width="1"/>` : ''}${!nd.net && i < nodes.length - 1 ? html`<line x1="${(x + bw).toFixed(1)}" x2="${((i + 1) * slot + (slot - bw) / 2).toFixed(1)}" y1="${cy}" y2="${cy}" stroke="var(--faint)" stroke-width="1" stroke-dasharray="3 3"/>` : ''}<text x="${(x + bw / 2).toFixed(1)}" y="${vy.toFixed(1)}" text-anchor="middle" font-size="10" class="num" fill="var(--fg-soft)">${(nd.val >= 0 ? '+' : '−') + F.compact(Math.abs(nd.val))}</text><text x="${(x + bw / 2).toFixed(1)}" y="${H - 12}" text-anchor="middle" font-size="10.5" fill="var(--muted)">${nd.label}</text>`;
  });
  setHTML(host, html`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:220px;display:block" role="img" aria-label="${wfLabel}"><line x1="0" x2="${W}" y1="${Y(0).toFixed(1)}" y2="${Y(0).toFixed(1)}" stroke="var(--border-2)" stroke-width="1"/>${parts}</svg>`);
}

function chipGroup(sel, attr, onPick) {
  const chips = $all(sel);
  chips.forEach((b) => b.addEventListener('click', () => { activate(chips, b); onPick(b.dataset[attr]); }));
}

export function initPortfolioControls() {
  chipGroup('#alloc-chips [data-dim]', 'dim', (v) => { allocDim = v; renderAlloc(); });
  chipGroup('#alloc-mode [data-mode]', 'mode', (v) => { allocMode = v; renderAlloc(); });
  chipGroup('#attr-chips [data-attr]', 'attr', (v) => { attrMode = v; renderAttribution(); });
  initHoldings();
}
