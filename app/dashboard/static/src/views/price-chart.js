// Per-symbol price history (with trade markers, benchmark overlay and point
// inspection) and the latest-prices table.

import { $, $all, activate, setText } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app, benchmarkSeries } from '../core/state.js';
import * as F from '../core/format.js';
import { niceTicks } from '../charts/common.js';
import { attachInspector } from '../charts/inspect.js';
import { CLOCK } from '../ui/icons.js';
import { gapLabel, TIP_STYLE } from './pv-chart.js';

let phRange = '3M', phBench = false;
export function spanMs(r) {
  const d = F.DAY_MS;
  return r === '1M' ? 30 * d : r === '3M' ? 90 * d : r === '1Y' ? 365 * d : null;
}
function axisDate(ms) { return (phRange === '1M' || phRange === '3M') ? F.fmtDate(ms, 'day') : F.fmtDate(ms, 'monthYear'); }

/** Trade markers: an up triangle per BUY, a down triangle per SELL. */
export function lotMarkers(lots, X, Y, xmin, xmax, size) {
  const a = size || 7, b = a - 2;
  return html`${lots.map((l) => {
    if (l.ts == null || l.ts < xmin || l.ts > xmax) return '';
    const mx = X(l.ts), my = Y(l.price), c = l.side === 'BUY' ? 'var(--up)' : 'var(--down)';
    const tri = l.side === 'BUY'
      ? mx + ',' + (my - a) + ' ' + (mx - (a - 1)) + ',' + (my + b) + ' ' + (mx + (a - 1)) + ',' + (my + b)
      : mx + ',' + (my + a) + ' ' + (mx - (a - 1)) + ',' + (my - b) + ' ' + (mx + (a - 1)) + ',' + (my - b);
    return html`<polygon points="${tri}" fill="${c}" stroke="var(--surface)" stroke-width="1"><title>${l.side + ' ' + l.qty + ' @ ' + F.money(l.price)}</title></polygon>`;
  })}`;
}

export function renderPriceChart() {
  const sel = $('#ph-symbol'), host = $('#ph-chart');
  if (!sel || !host) return;
  const sym = sel.value;
  const series = ((app.D.priceHist || {})[sym]) || [];
  if (series.length < 2) { setHTML(host, html`<div class="empty">No price history for ${sym} yet.</div>`); return; }
  const now = series[series.length - 1][0], span = spanMs(phRange);
  let data = span ? series.filter((p) => p[0] >= now - span) : series.slice();
  if (data.length < 2) data = series.slice();
  const W = 920, H = 280, padL = 8, padR = 8, padT = 14, padB = 18;
  const xs = data.map((p) => p[0]), ys = data.map((p) => p[1]);
  const xmin = xs[0], xmax = xs[xs.length - 1], xr = (xmax - xmin) || 1;
  const ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys), yr = (ymax - ymin) || 1;
  const X = (t) => padL + (t - xmin) / xr * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - ymin) / yr) * (H - padT - padB);
  const up = ys[ys.length - 1] >= ys[0], color = up ? 'var(--up)' : 'var(--down)';
  const line = data.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join(' ');
  const area = line + ' L' + X(xmax).toFixed(1) + ',' + (H - padB) + ' L' + X(xmin).toFixed(1) + ',' + (H - padB) + ' Z';
  const ticks = niceTicks(ymin, ymax, 4).filter((t) => { const y = Y(t); return y >= padT - 1 && y <= H - padB + 1; });
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 0;
  const yLabels = ticks.map((t) => html`<span style="top:${((Y(t) / H) * 100).toFixed(2)}%">${F.axisMoney(t, step)}</span>`);
  const nX = Math.min(4, data.length), xLabels = [];
  for (let i = 0; i < nX; i++) {
    const idx = nX === 1 ? 0 : Math.round(i * (data.length - 1) / (nX - 1));
    const place = i === 0 ? 'left:0' : i === nX - 1 ? 'right:0' : 'left:' + ((X(data[idx][0]) / W) * 100).toFixed(2) + '%;transform:translateX(-50%)';
    xLabels.push(html`<span style="${place}">${axisDate(data[idx][0])}</span>`);
  }
  // This x axis IS time, so a collection gap is drawn at its own width. The
  // list is the collector's, filtered to what this range shows.
  const gaps = (app.D.gapsAll || []).filter((g) => g.to > xmin && g.from < xmax);
  const gapRects = gaps.map((g) => {
    const x0 = X(Math.max(g.from, xmin)), x1 = X(Math.min(g.to, xmax));
    return html`<rect x="${x0.toFixed(1)}" y="${padT}" width="${Math.max(2, x1 - x0).toFixed(1)}" height="${H - padT - padB}" fill="url(#ph-gap)"><title>${gapLabel(g, (ms) => F.fmtDate(ms, 'dayTime'))}</title></rect>`;
  });
  const chg = ys[0] ? ((ys[ys.length - 1] - ys[0]) / ys[0]) * 100 : 0;
  const bLabel = app.cfg.benchmarkLabel;
  let overlay = '';
  const bs = phBench ? benchmarkSeries() : null;
  if (bs) {
    const sp = bs.filter((p) => p[0] >= xmin && p[0] <= xmax);
    if (sp.length > 1) {
      // Rebased to the symbol's starting price: same % move = same slope.
      const base = sp[0][1], symBase = data[0][1];
      const sline = sp.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(symBase * p[1] / base).toFixed(1)).join(' ');
      overlay = html`<path d="${sline}" fill="none" stroke="var(--warn)" stroke-width="1.6" stroke-dasharray="3 3" opacity=".85"/>`;
    }
  }
  const ariaLabel = sym + ' price, ' + phRange + ' range, ' + F.pct(chg) + ', from ' + F.money(ys[0]) + ' to ' + F.money(ys[ys.length - 1]) +
    (gaps.length ? ', ' + gaps.length + ' collection gap' + (gaps.length > 1 ? 's' : '') + ' marked' : '');
  const lots = ((app.D.symLots || {})[sym]) || [];
  setHTML(host, html`<div class="ph-wrap"><div class="ph-y">${yLabels}</div><div class="ph-plot inspectable"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:300px" role="img" aria-label="${ariaLabel}"><defs><pattern id="ph-gap" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="5" stroke="var(--warn)" stroke-width="2.4" opacity=".45"/></pattern><linearGradient id="phg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".18"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>${ticks.map((t) => { const y = Y(t).toFixed(1); return html`<line x1="0" x2="${W}" y1="${y}" y2="${y}" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 4"/>`; })}${gapRects}<path d="${area}" fill="url(#phg)"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round"/>${overlay}${lotMarkers(lots, X, Y, xmin, xmax, 7)}<line class="ph-cross" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/><circle class="ph-cursor" cx="0" cy="0" r="4" fill="${color}" stroke="var(--surface)" stroke-width="1.5" opacity="0"/></svg><div class="ph-tip" style="${TIP_STYLE}"></div><div class="sr-only ph-live" aria-live="polite" aria-atomic="true"></div><div class="ph-x">${xLabels}</div></div></div><div style="font-size:var(--fs-micro);color:var(--muted);margin-top:6px"><span class="up">▲ BUY</span> &nbsp; <span class="down">▼ SELL</span>${overlay ? html` &nbsp; · &nbsp; <span style="color:var(--warn)">┄ ${bLabel}, rebased to ${sym}’s start (same % scale)</span>` : ''}</div>`);
  const plot = host.querySelector('.ph-plot');
  attachInspector({
    plot, svg: plot.querySelector('svg'), W, H, ariaLabel,
    points: data.map((p) => ({ x: X(p[0]), y: Y(p[1]) })),
    label: (i) => sym + ', ' + F.fmtDate(data[i][0], 'fullTime') + ': ' + F.money(data[i][1]),
    tipHTML: (i) => html`<div style="font-weight:600">${sym}  ${F.money(data[i][1])}</div><div style="color:var(--muted);font-size:var(--fs-micro)">${F.fmtDate(data[i][0], 'fullTime')}</div>`,
    cross: host.querySelector('.ph-cross'), cursor: host.querySelector('.ph-cursor'),
    tip: host.querySelector('.ph-tip'), live: host.querySelector('.ph-live'),
  });
}

export function initPriceChart() {
  const sel = $('#ph-symbol');
  if (!sel) return;
  let syms = (app.D.chartSyms || []).filter((s) => app.D.priceHist && app.D.priceHist[s]);
  if (!syms.length) syms = Object.keys(app.D.priceHist || {});
  // DOM nodes, not markup: new Option() sets text as text.
  sel.replaceChildren();
  syms.forEach((s) => sel.appendChild(new Option(s, s)));
  sel.addEventListener('change', renderPriceChart);
  // The overlay is the configured benchmark, and its control says which.
  const bench = $('#ph-spy');
  setText($('[data-bench-label]'), app.cfg.benchmarkLabel);
  if (bench) {
    bench.disabled = !benchmarkSeries();
    if (bench.disabled) bench.closest('label').title = 'No ' + app.cfg.benchmarkLabel + ' price history collected yet';
    bench.addEventListener('change', () => { phBench = bench.checked; renderPriceChart(); });
  }
  const chips = $all('#ph-chips [data-phr]');
  chips.forEach((b) => b.addEventListener('click', () => { phRange = b.dataset.phr; activate(chips, b); renderPriceChart(); }));
  renderPriceChart();
}

// ---- latest prices table -------------------------------------------------
export function renderLatestPrices() {
  const rows = app.D.latestPrices || [], tb = $('#latest-prices');
  if (!tb) return;
  // 0.0 is what yfinance hands back for bid/ask outside regular hours. It is an
  // absent quote, not a price of nothing: never $0.00, never a spread.
  const px = (v) => (v == null || v === 0)
    ? html`<span style="color:var(--faint)" title="Not quoted at this snapshot">—</span>` : F.money(v);
  // Freshness against the newest row - the collector's own last run - never a
  // wall clock: the weekend gap alone is 64.2h.
  const parse = (r) => Date.parse(String(r.ts || '').replace(' ', 'T'));
  const stamps = rows.map(parse).filter((t) => !Number.isNaN(t));
  const newest = stamps.length ? Math.max.apply(null, stamps) : null;
  const STALE_MS = 4 * F.DAY_MS; // clears a long weekend plus a public holiday
  let staleCount = 0;
  setHTML(tb, html`${rows.map((r) => {
    const t = parse(r);
    const age = (newest != null && !Number.isNaN(t)) ? newest - t : 0;
    const stale = age > STALE_MS;
    if (stale) staleCount++;
    const days = Math.round(age / F.DAY_MS);
    return html`<tr${stale ? html` class="lp-stale" title="${'Last collected ' + days + ' days before the most recent snapshot'}"` : ''}><td><b>${r.symbol}</b></td><td class="price">${px(r.last)}</td><td class="num" style="color:var(--muted)">${px(r.bid)}</td><td class="num" style="color:var(--muted)">${px(r.ask)}</td><td class="num" style="color:var(--muted)">${r.source}</td><td class="num" style="color:var(--muted)">${stale ? html`${CLOCK} ` : ''}${r.ts}</td></tr>`;
  })}`);
  setText($('#lp-sub'), rows.length + ' symbols' + (staleCount ? ' · ' + staleCount + ' not refreshed in the last 4 days' : ''));
}
