// The KPI tiles and the hero card's one-line story.

import { $, setText, prefersReducedMotion } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app, holdingRows } from '../core/state.js';
import * as F from '../core/format.js';
import { sparkSVG } from '../charts/common.js';
import { ARROW_UP, ARROW_DN } from '../ui/icons.js';

function deltaHTML(val, p, isMoney) {
  const up = val >= 0;
  const txt = (isMoney ? (up ? '+' : '−') + F.money(Math.abs(val)) : '') + (p != null ? (isMoney ? '  ' : '') + F.pct(p) : '');
  return html`<span class="delta ${up ? 'up' : 'down'}">${up ? ARROW_UP : ARROW_DN}${txt}</span>`;
}
function setKpi(id, txt, dir) {
  const e = $('#' + id);
  if (!e) return;
  e.textContent = txt;
  e.className = 'kpi__val' + (dir > 0 ? ' up' : dir < 0 ? ' down' : '');
}
function muted(text) { return html`<span style="color:var(--muted)">${text}</span>`; }

function countUp(el, target, fmt) {
  if (!el) return;
  if (prefersReducedMotion() || !window.requestAnimationFrame) { el.textContent = fmt(target); return; }
  const from = target * 0.985, dur = 700;
  let t0 = null;
  function step(t) {
    if (t0 == null) t0 = t;
    let f = Math.min(1, (t - t0) / dur); f = 1 - Math.pow(1 - f, 3);
    el.textContent = fmt(from + (target - from) * f);
    if (f < 1) window.requestAnimationFrame(step);
  }
  el.textContent = fmt(target);
  window.requestAnimationFrame(step);
}

/** The cash entries with their entry date, whichever key the server used. */
export function cashEntries() {
  const k = app.D.kpi || {};
  const rows = Array.isArray(k.cashAccounts) ? k.cashAccounts : (Array.isArray(app.D.cashAccounts) ? app.D.cashAccounts : []);
  return rows.map((a) => ({ account: a.account, cash: Number(a.cash) || 0, asOf: a.as_of || a.asOf || null }));
}
/** "Nov 2, 2026" for an ISO timestamp, in the reporting zone. */
export function entryDate(iso) {
  const t = Date.parse(String(iso || ''));
  return Number.isNaN(t) ? (F.isoDay(iso) || null) : F.fmtDate(t, 'date');
}
// The figure is the latest balance typed in per account - not a broker's
// settled cash and not buying power - so the tile says when it was entered
// (audit U-4). With several accounts the oldest entry is the one that bounds
// how current the total is.
function cashSub() {
  const rows = cashEntries().filter((a) => a.asOf);
  if (!rows.length) return cashEntries().length ? 'entry date not recorded' : 'no balance entered';
  const oldest = rows.reduce((a, b) => (Date.parse(a.asOf) <= Date.parse(b.asOf) ? a : b));
  return (rows.length > 1 ? 'oldest entry ' : 'entered ') + entryDate(oldest.asOf);
}

// One-line story for the hero card: day move, top contributor, MTD vs benchmark.
export function narrative() {
  const k = app.D.kpi || {}, dc = k.dayChange || 0, parts = [];
  if (Math.abs(dc) < 0.005) parts.push('Flat today');
  else parts.push((dc > 0 ? 'Up ' : 'Down ') + F.money(Math.abs(dc)) + ' (' + F.pct(k.dayChangePct || 0) + ') today');
  const rs = holdingRows();
  if (rs.length && Math.abs(dc) >= 0.005) {
    const top = rs.slice().sort((a, b) => Math.abs(b.day * b.qty) - Math.abs(a.day * a.qty))[0];
    if (top && Math.abs(top.day * top.qty) > 0.005) {
      parts.push((top.day * top.qty >= 0 ? 'led by ' : 'dragged by ') + top.sym + ' ' + F.pct(top.dayPct));
    }
  }
  let mtd = null;
  ((app.D.returns || {}).periods || []).forEach((p) => { if (p.period === 'MTD') mtd = p; });
  let bench = '';
  if (mtd && mtd.portfolio != null && mtd.benchmark != null) {
    const d = mtd.portfolio - mtd.benchmark;
    bench = 'MTD ' + F.pct(mtd.portfolio) + ', ' + (d >= 0 ? 'beating' : 'trailing') + ' ' +
      app.cfg.benchmarkLabel + ' by ' + F.num(Math.abs(d), 2) + ' pts';
  }
  return parts.join(', ') + (bench ? ' — ' + bench : '') + '.';
}

// Built with DOM nodes: the label never becomes text-to-parse.
function sparkWindowLabel(series, up) {
  const p = series[0] ? ((series[series.length - 1] - series[0]) / series[0]) * 100 : 0;
  const el = document.createElement('div');
  el.className = 'kpi__spark-label';
  el.appendChild(document.createTextNode('past month '));
  const val = document.createElement('span');
  val.className = up ? 'up' : 'down';
  val.textContent = F.pct(p);
  el.appendChild(val);
  return el;
}

export function renderKPIs() {
  const k = app.D.kpi || {};
  countUp($('#kpi-total'), k.totalValue || 0, F.money);
  setHTML($('#kpi-total-sub'), html`${deltaHTML(k.dayChange || 0, k.dayChangePct, true)}${muted('today')}`);
  setText($('#kpi-story'), narrative());
  const spark = $('#kpi-hero-spark');
  if (spark) {
    // The number above is *today*; the line is the past month and can fall
    // while today rises, so it is labelled.
    const pvm = ((app.D.pv || {})['1M'] || []).map((p) => p[1]);
    const up = pvm.length > 1 && pvm[pvm.length - 1] >= pvm[0];
    setHTML(spark, pvm.length > 1 ? sparkSVG(pvm, up, 600, 54) : html``);
    if (pvm.length > 1) spark.insertBefore(sparkWindowLabel(pvm, up), spark.firstChild);
  }
  setText($('#kpi-mktval'), F.money(k.marketValue || 0));
  setKpi('kpi-gl', F.signed(k.unrealized), k.unrealized);
  setHTML($('#kpi-gl-sub'), html`${deltaHTML(k.unrealized || 0, k.unrealizedPct, false)}${muted('open positions')}`);
  setText($('#kpi-cash'), F.money(k.cash || 0));
  setText($('#kpi-cash-sub'), cashSub());
  setKpi('kpi-realized', F.signed(k.realized), k.realized);
  setKpi('kpi-return', F.pct(k.totalReturnPct || 0), k.totalReturnPct);
  setText($('#kpi-cost'), F.money(k.costBasis || 0));
  setHTML($('#kpi-cost-sub'), muted((k.activeCount || 0) + ' active · ' + (k.watchlistCount || 0) + ' watching · ' + F.money(k.totalFees || 0) + ' fees'));
  setKpi('kpi-delta', F.signed(k.deltaLast), k.deltaLast);
  setHTML($('#kpi-delta-sub'), muted('since previous snapshot'));
  setText($('#kpi-div'), F.money(k.dividends || 0));
  setHTML($('#kpi-div-sub'), muted('TTM ' + F.money(k.dividendsTtm || 0) + ' · YoC ' + F.pct(k.yieldOnCostPct || 0) +
    ' · w/ div ' + F.pct(k.totalReturnWithIncomePct || 0)));
}
