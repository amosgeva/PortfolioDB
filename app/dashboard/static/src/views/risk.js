// Risk & analytics: payload-computed beta/vol/Sharpe/drawdown, per-symbol
// table and the correlation grid (re-coloured on a theme change).

import { $, setText } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app } from '../core/state.js';
import { symOpenBtn } from '../ui/symbols.js';
import { isDark } from '../ui/theme.js';
import { item } from '../core/dict.js';

function tile(l, v, cls) {
  return html`<div class="stat-mini stat-mini--well"><div class="v num${cls ? ' ' + cls : ''}">${v}</div><div class="l">${l}</div></div>`;
}
function fx(v, dp, suffix) { return v != null ? v.toFixed(dp) + (suffix || '') : '—'; }

function corrColor(v) {
  const a = Math.min(Math.abs(v), 1);
  if (a < 0.05) return isDark() ? 'oklch(38% 0.012 260)' : 'oklch(88% 0.008 260)';
  return 'oklch(' + (isDark() ? (36 + a * 20) : (90 - a * 28)).toFixed(1) + '% ' + (0.03 + a * 0.1).toFixed(3) + ' ' + (v >= 0 ? 152 : 26) + ')';
}

export function renderRisk() {
  const body = $('#risk-body');
  if (!body) return;
  const R = app.D.risk;
  const stats = $('#risk-stats'), tbl = $('#risk-tbl'), corrHost = $('#risk-corr');
  if (!R || !R.ok) {
    setHTML(stats, html`<div class="empty">Not enough snapshot history for risk stats yet (needs ~20 trading days).</div>`);
    setHTML(tbl, html``);
    setHTML(corrHost, html``);
    return;
  }
  const bench = app.cfg.benchmarkLabel;
  setText($('#risk-sub'), R.days + ' trading days · ' + bench + ' benchmark · current positions over historical closes');
  const p = R.portfolio || {}, conc = R.concentration || {};
  setHTML(stats, html`${tile('Beta vs ' + bench, fx(p.beta, 2))}${tile('Volatility (ann.)', fx(p.vol, 1, '%'))}${tile('Sharpe (1Y)', fx(p.sharpe, 2), p.sharpe != null ? (p.sharpe >= 1 ? 'up' : p.sharpe < 0 ? 'down' : '') : '')}${tile('Max drawdown', p.maxDD != null ? '−' + p.maxDD.toFixed(1) + '%' : '—', 'down')}${tile('Top position', conc.top1 ? conc.top1.sym + ' ' + conc.top1.pct.toFixed(1) + '%' : '—')}${tile('Top-3 weight', fx(conc.top3Pct, 1, '%'))}`);
  setHTML(tbl, html`${(R.perSymbol || []).map((r) => html`<tr data-sym="${r.sym}"><td>${symOpenBtn(r.sym, html`<b>${r.sym}</b>`)}</td><td class="num">${fx(r.weight, 1, '%')}</td><td class="num">${fx(r.beta, 2)}</td><td class="num">${fx(r.vol, 1, '%')}</td><td class="num">${fx(r.sharpe, 2)}</td></tr>`)}`);
  const C = R.corr || {}, syms = C.syms || [], m = C.m || [];
  if (syms.length < 2) { setHTML(corrHost, html`<div class="empty">Need at least two holdings for correlations.</div>`); return; }
  const rows = syms.map((si, i) => html`<span class="corr__lbl" style="text-align:right;padding-right:6px">${si}</span>${syms.map((sj, j) => {
    const v = item(m, i) ? item(item(m, i), j) : null;
    if (v == null) return html`<span class="corr__cell" style="background:var(--surface-2);color:var(--faint)">—</span>`;
    const strong = Math.abs(v) > 0.65 && !isDark();
    return html`<span class="corr__cell" title="${si + ' × ' + sj + ' = ' + v.toFixed(2)}" style="${'background:' + corrColor(v) + (strong ? ';color:#fff' : '')}">${v.toFixed(2)}</span>`;
  })}`);
  setHTML(corrHost, html`<div class="corr" style="grid-template-columns:48px repeat(${syms.length},1fr)" role="img" aria-label="Holdings correlation matrix"><span></span>${syms.map((s) => html`<span class="corr__lbl">${s}</span>`)}${rows}</div><div style="font-size:var(--fs-micro);color:var(--muted);margin-top:8px">Pairwise correlation of daily returns — green moves together, red moves opposite.</div>`);
}
