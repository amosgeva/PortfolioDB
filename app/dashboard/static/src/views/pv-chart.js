// Portfolio value chart: snapshot history per range, drawdown shading,
// collection-gap markers and point inspection (mouse, touch, keyboard).

import { $, $all, activate, prefersReducedMotion } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app } from '../core/state.js';
import * as F from '../core/format.js';
import { niceTicks, suspectIdx, cleanExtent } from '../charts/common.js';
import { attachInspector } from '../charts/inspect.js';
import { CLOCK } from '../ui/icons.js';
import { item } from '../core/dict.js';

let pvRange = '1D';

function tipDate(ms) { return F.fmtDate(ms, pvRange === '1D' ? 'dayTime' : 'date'); }
// The tooltip names one point; the axis prints four side by side.
function axisDate(ms) {
  if (pvRange === '1D') return F.fmtDate(ms, 'time');
  // "Sep 25" is a day on every other range, so a year gets an apostrophe.
  if (pvRange === '1Y') return F.fmtDate(ms, 'monthYear');
  return F.fmtDate(ms, 'day');
}

// pvBasis says what window each range really covers. With less than a year of
// snapshots the server draws "1Y" from all history, and the chart says so
// rather than letting a few months pass for a year.
function fellBack(range) { return (item(app.D.pvBasis || {}, range)) === 'all_history'; }
function rangeLabel(range) { return fellBack(range) ? 'All history (less than 1Y collected)' : range; }

function gapHours(mins) {
  if (mins < 60) return mins + ' min';
  const h = Math.floor(mins / 60), m = mins % 60;
  return h + 'h' + (m ? ' ' + m + 'm' : '');
}
export function gapLabel(gap, fmt) {
  const f = fmt || tipDate;
  return 'No snapshot for ' + gapHours(gap.openMinutes) + ' of collecting time here — ' + f(gap.from) + ' to ' +
    f(gap.to) + '. The market being shut is not counted.';
}
function gapList() { return (item(app.D.pvGaps || {}, pvRange)) || []; }

// The x axis is ordinal (one step per snapshot), so a missing hour has no
// width: the mark sits on the junction rather than spanning it.
function gapMarkers(pairs, gaps, pts, top, bot) {
  if (!gaps || !gaps.length || pairs.length < 2) return html``;
  return html`${gaps.map((gap) => {
    let left = -1;
    for (let i = 0; i < pairs.length; i++) { if (item(item(pairs, i), 0) <= gap.from) left = i; else break; }
    if (left < 0 || left >= pairs.length - 1) return '';
    const x = (item(item(pts, left), 0) + item(item(pts, left + 1), 0)) / 2;
    return html`<rect x="${(x - 3).toFixed(1)}" y="${top}" width="6" height="${bot - top}" fill="url(#pv-gap)"><title>${gapLabel(gap)}</title></rect><line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${top}" y2="${bot}" stroke="var(--warn)" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/>`;
  })}`;
}

const TIP_STYLE = 'position:absolute;pointer-events:none;opacity:0;transform:translate(-50%,-115%);background:var(--surface);' +
  'border:1px solid var(--border);border-radius:var(--r-sm);padding:6px 9px;box-shadow:var(--shadow-tip);' +
  'font-size:var(--fs-meta);white-space:nowrap;z-index:5';
export { TIP_STYLE };

export function renderPV() {
  const host = $('#pv-chart');
  if (!host) return;
  const pairs = (item(app.D.pv || {}, pvRange)) || [];
  if (pairs.length < 2) { setHTML(host, html`<div class="empty">No portfolio-value history for this range yet.</div>`); return; }
  const data = pairs.map((p) => item(p, 1));
  const W = 920, H = 240, pad = 6;
  // Points the arithmetic cannot support are excluded from the domain, the
  // stroke and the drawdown scan alike.
  const bad = suspectIdx(data);
  const ext = cleanExtent(data, bad);
  const min = ext.min, range = ext.range;
  const up = item(data, data.length - 1) >= item(data, 0), color = up ? 'var(--up)' : 'var(--down)';
  const pts = data.map((v, i) => [pad + (i / (data.length - 1)) * (W - pad * 2), 14 + (1 - (v - min) / range) * (H - 28)]);
  let line = '', pen = 'M';
  pts.forEach((p, i) => {
    if (item(bad, i)) { pen = 'M'; return; }
    line += (line ? ' ' : '') + pen + item(p, 0).toFixed(1) + ',' + item(p, 1).toFixed(1); pen = 'L';
  });
  let areaLine = '', holdY = null;
  pts.forEach((p, i) => {
    const y = item(bad, i) ? (holdY == null ? item(p, 1) : holdY) : (holdY = item(p, 1));
    areaLine += (areaLine ? ' L' : 'M') + item(p, 0).toFixed(1) + ',' + y.toFixed(1);
  });
  const area = areaLine + ' L' + item(item(pts, pts.length - 1), 0).toFixed(1) + ',' + (H - 2) + ' L' + item(item(pts, 0), 0).toFixed(1) + ',' + (H - 2) + ' Z';
  const last = item(pts, pts.length - 1);
  // A change from zero is not a percentage: say nothing rather than Infinity.
  const chg = item(data, 0) ? (item(data, data.length - 1) - item(data, 0)) / item(data, 0) * 100 : null;
  const chgTxt = chg == null ? '—' : F.pct(chg);
  let peakI = 0, ddS = 0, ddE = 0, maxDD = 0;
  for (let di = 1; di < data.length; di++) {
    if (item(bad, di)) continue;
    if (item(bad, peakI) || item(data, di) > item(data, peakI)) peakI = di;
    const dd = (item(data, peakI) - item(data, di)) / item(data, peakI);
    if (dd > maxDD) { maxDD = dd; ddS = peakI; ddE = di; }
  }
  const ddRect = (maxDD >= 0.005 && ddE > ddS)
    ? html`<rect x="${item(item(pts, ddS), 0).toFixed(1)}" y="14" width="${(item(item(pts, ddE), 0) - item(item(pts, ddS), 0)).toFixed(1)}" height="${H - 28}" fill="var(--down)" opacity=".055"><title>${'Max drawdown −' + (maxDD * 100).toFixed(1) + '%'}</title></rect>`
    : '';
  const tickY = (v) => 14 + (1 - (v - min) / range) * (H - 28);
  const ticks = niceTicks(min, min + range, 4).filter((t) => { const y = tickY(t); return y >= 12 && y <= H - 12; });
  const tStep = ticks.length > 1 ? item(ticks, 1) - item(ticks, 0) : 0;
  const yLabels = ticks.map((t) => html`<span style="top:${((tickY(t) / H) * 100).toFixed(2)}%">${F.axisMoney(t, tStep)}</span>`);
  const nX = Math.min(4, pairs.length);
  const xLabels = [];
  for (let xi = 0; xi < nX; xi++) {
    const idx = nX === 1 ? 0 : Math.round(xi * (pairs.length - 1) / (nX - 1));
    const place = xi === 0 ? 'left:0' : xi === nX - 1 ? 'right:0' : 'left:' + ((item(item(pts, idx), 0) / W) * 100).toFixed(2) + '%;transform:translateX(-50%)';
    xLabels.push(html`<span style="${place}">${axisDate(item(item(pairs, idx), 0))}</span>`);
  }
  const gaps = gapList();
  const ariaLabel = 'Portfolio value, ' + rangeLabel(pvRange) + ' range, ' + chgTxt +
    (bad.count ? ', ' + bad.count + ' incomplete snapshot' + (bad.count > 1 ? 's' : '') + ' omitted' : '') +
    (gaps.length ? ', ' + gaps.length + ' collection gap' + (gaps.length > 1 ? 's' : '') + ' marked' : '');
  setHTML(host, html`<div class="pv-wrap"><div class="pv-y">${yLabels}</div><div class="pv-plot inspectable"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:240px;display:block" role="img" aria-label="${ariaLabel}"><defs><pattern id="pv-gap" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="5" stroke="var(--warn)" stroke-width="2.4" opacity=".5"/></pattern><linearGradient id="pvg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".20"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>${ticks.map((t) => { const y = tickY(t).toFixed(1); return html`<line x1="0" x2="${W}" y1="${y}" y2="${y}" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 4"/>`; })}${ddRect}${gapMarkers(pairs, gaps, pts, 14, H - 14)}<path d="${area}" fill="url(#pvg)"/><path class="pv-line" d="${line}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round"/><line class="pv-cross" x1="0" x2="0" y1="0" y2="${H}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/><circle class="pv-cursor" cx="0" cy="0" r="4" fill="${color}" stroke="var(--surface)" stroke-width="1.5" opacity="0"/><circle cx="${item(last, 0).toFixed(1)}" cy="${item(last, 1).toFixed(1)}" r="4" fill="${color}"/><circle cx="${item(last, 0).toFixed(1)}" cy="${item(last, 1).toFixed(1)}" r="8" fill="${color}" opacity=".18"/></svg><div class="pv-tip" style="${TIP_STYLE}"></div><div class="sr-only pv-live" aria-live="polite" aria-atomic="true"></div><div class="num" style="position:absolute;top:6px;left:10px;font-size:var(--fs-meta);font-weight:600;pointer-events:none"><span style="color:${color}">${rangeLabel(pvRange) + ' ' + chgTxt}</span>${maxDD >= 0.005 ? html`<span style="color:var(--muted);font-weight:500">  ·  max DD −${(maxDD * 100).toFixed(1)}%</span>` : ''}</div>${bad.count ? html`<div style="position:absolute;bottom:26px;right:10px;font-size:var(--fs-meta);color:var(--muted);pointer-events:none">${CLOCK} ${bad.count + ' incomplete snapshot' + (bad.count > 1 ? 's' : '') + ' omitted · see Data Health'}</div>` : ''}<div class="pv-x">${xLabels}</div></div></div>`);

  // draw-in: reveal the line along its own length on (re)render
  const lineEl = host.querySelector('.pv-line');
  if (lineEl && !prefersReducedMotion() && lineEl.getTotalLength) {
    try {
      const L = lineEl.getTotalLength();
      lineEl.style.strokeDasharray = L + ' ' + L;
      lineEl.style.strokeDashoffset = L;
      lineEl.getBoundingClientRect();
      lineEl.style.transition = 'stroke-dashoffset .7s cubic-bezier(.25,1,.5,1)';
      lineEl.style.strokeDashoffset = '0';
      setTimeout(() => { lineEl.style.strokeDasharray = 'none'; }, 750);
    } catch (e) { /* getTotalLength unsupported: draw without the sweep */ }
  }
  const plot = host.querySelector('.pv-plot');
  // Measure the svg itself: the wrapper includes the axis gutters.
  const svg = plot.querySelector('svg');
  const label = (i) => tipDate(item(item(pairs, i), 0)) + ': ' + F.money(item(item(pairs, i), 1));
  attachInspector({
    plot, svg, W, H, ariaLabel,
    points: pts.map((p) => ({ x: item(p, 0), y: item(p, 1) })),
    // The x axis is ordinal, so the nearest point is a rounding, not a search.
    nearest: (svgX) => Math.max(0, Math.min(data.length - 1, Math.round(((svgX - pad) / (W - pad * 2)) * (data.length - 1)))),
    label,
    tipHTML: (i) => html`<div style="font-weight:600">${F.money(item(item(pairs, i), 1))}</div><div style="color:var(--muted);font-size:var(--fs-micro)">${tipDate(item(item(pairs, i), 0))}</div>`,
    cross: host.querySelector('.pv-cross'), cursor: host.querySelector('.pv-cursor'),
    tip: host.querySelector('.pv-tip'), live: host.querySelector('.pv-live'),
  });
}

// Ranges with no snapshots are disabled rather than silently drawn from a wider
// window, and the narrowest one that has data starts active.
export function initPVRange() {
  const chips = $all('#pv-chips [data-range]');
  let firstWithData = null;
  chips.forEach((c) => {
    const has = ((item(app.D.pv || {}, c.dataset.range)) || []).length > 1;
    c.disabled = !has;
    c.title = !has ? 'No snapshots in this range yet'
      : fellBack(c.dataset.range) ? 'Less than a year of snapshots: this shows all history' : '';
    if (has && !firstWithData) firstWithData = c;
  });
  if (firstWithData) { pvRange = firstWithData.dataset.range; activate(chips, firstWithData); }
  chips.forEach((b) => b.addEventListener('click', () => { pvRange = b.dataset.range; activate(chips, b); renderPV(); }));
}
