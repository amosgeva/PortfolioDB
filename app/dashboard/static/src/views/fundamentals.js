// Fundamentals: company profile, valuation/quality metrics, quarterly trend,
// earnings, filings, insider activity and institutional holders. Rendered on
// first visit (it is the heaviest view and most sessions never open it).

import { $, setText } from '../core/dom.js';
import { html, setHTML, safeUrl } from '../core/html.js';
import { app } from '../core/state.js';
import * as F from '../core/format.js';
import { item } from '../core/dict.js';

function fpct(x, d) { return x == null ? '—' : F.num(Number(x) * 100, d == null ? 1 : d) + '%'; }
function fratio(x, d) { return x == null ? '—' : F.num(Number(x), d == null ? 2 : d); }
const LBL = 'font-size:var(--fs-micro);color:var(--muted);text-transform:uppercase;letter-spacing:.04em';

function statBox(label, val) {
  return html`<div><div style="${LBL}">${label}</div><div style="font-size:var(--fs-base);font-weight:600;margin-top:2px">${val || '—'}</div></div>`;
}

// Magnitude, not direction: positive quarters take a neutral ink and only a
// negative quarter earns a colour, because only it means something.
function barChart(items, key, title) {
  const vals = items.map((t) => (item(t, key) == null ? null : Number(item(t, key))));
  if (!vals.some((v) => v != null)) {
    return html`<div style="flex:1"><div style="font-size:var(--fs-meta);color:var(--muted);margin-bottom:6px">${title}</div><div class="empty" style="padding:14px">no data</div></div>`;
  }
  const nums = vals.map((v) => (v == null ? 0 : v));
  const max = Math.max.apply(null, nums.map(Math.abs)) || 1;
  const W = items.length * 26, H = 80;
  const bars = nums.map((v, i) => {
    const h = Math.abs(v) / max * 56, x = i * 26 + 4, y = v >= 0 ? (62 - h) : 62;
    const lbl = F.periodLabel((item(items, i) || {}).period) + ' · ' + (item(vals, i) == null ? 'no data' : (item(vals, i) < 0 ? '−' : '') + F.compact(Math.abs(item(vals, i))));
    return html`<rect x="${x}" y="${y.toFixed(1)}" width="16" height="${h.toFixed(1)}" rx="2" fill="${v < 0 ? 'var(--down)' : 'var(--muted)'}"><title>${lbl}</title></rect>`;
  });
  // The peak names the top of the scale, the ends name the span.
  const first = F.periodLabel((item(items, 0) || {}).period);
  const lastP = F.periodLabel((item(items, items.length - 1) || {}).period);
  const span = first === lastP ? first : first + ' – ' + lastP;
  return html`<div style="flex:1;min-width:160px"><div class="bc-hd"><span>${title}</span><span class="bc-peak num">peak ${F.compact(max)}</span></div><svg viewBox="0 0 ${W} ${H}" style="width:100%;height:80px" preserveAspectRatio="none" role="img" aria-label="${title + ', ' + span + ', peak ' + F.compact(max)}"><line x1="0" x2="${W}" y1="62" y2="62" stroke="var(--border)"/>${bars}</svg><div class="bc-x"><span>${first}</span><span>${lastP}</span></div></div>`;
}

function card(title, body, extra) {
  return html`<section class="card" style="margin-top:20px"><div class="card__hd"><h2>${title}</h2>${extra || ''}</div>${body}</section>`;
}
function emptyRow(cols, text) { return html`<tr><td colspan="${cols}"><div class="empty">${text}</div></td></tr>`; }

export function renderFundamentals() {
  const sel = $('#fd-symbol'), host = $('#fd-body');
  if (!sel || !host) return;
  if (!sel.options.length) {
    // DOM nodes, not markup - new Option() sets text as text.
    sel.replaceChildren();
    (app.D.fdUniverse || []).forEach((s) => sel.appendChild(new Option(s, s)));
    if (app.D.fdDefault) sel.value = app.D.fdDefault;
    sel.addEventListener('change', renderFundamentals);
  }
  const sym = sel.value, fd = item(app.D.fundamentals || {}, sym);
  setText($('#fd-asof'), app.D.asOf ? 'As of ' + app.D.asOf : '');
  if (!fd) { setHTML(host, html`<div class="empty">No fundamentals on file for ${sym}.</div>`); return; }

  const m = fd.metrics;
  const out = [html`<section class="card"><div class="card__bd" style="display:flex;gap:24px;flex-wrap:wrap;align-items:center"><div><div style="font-size:var(--fs-xl);font-weight:700">${fd.name}</div><div style="font-size:var(--fs-meta);color:var(--muted)">${sym + ' · ' + (fd.exchange || '—')}</div></div><div style="flex:1"></div>${statBox('Sector', fd.sector)}${statBox('Industry', fd.industry)}${statBox('Market cap', m && m.market_cap != null ? F.compact(m.market_cap) : '—')}</div></section>`];

  if (fd.isEtf) {
    out.push(html`<section class="card" style="margin-top:20px"><div class="card__bd"><div class="empty">${sym} is an ETF / fund — Financial Datasets carries news only (see Alerts &amp; News), no fundamentals.</div></div></section>`);
    setHTML(host, html`${out}`);
    return;
  }

  if (m) {
    // grouped by question being answered: price vs. quality vs. trajectory
    const groups = [
      ['Valuation', [['P/E', fratio(m.pe_ratio)], ['P/S', fratio(m.ps_ratio)], ['EV/EBITDA', fratio(m.ev_ebitda)], ['FCF yield', fpct(m.free_cash_flow_yield, 2)]]],
      ['Profitability', [['ROE', fpct(m.return_on_equity)], ['Gross', fpct(m.gross_margin)], ['Op margin', fpct(m.operating_margin)], ['Net margin', fpct(m.net_margin)]]],
      ['Growth & balance', [['Rev growth', fpct(m.revenue_growth)], ['EPS growth', fpct(m.earnings_growth)], ['D/E', fratio(m.debt_to_equity)], ['Current', fratio(m.current_ratio)]]],
    ];
    out.push(card('Valuation & quality', html`<div class="card__bd fd-groups">${groups.map((g) => html`<div class="fd-group"><div class="fd-group__t">${item(g, 0)}</div><div class="fd-group__grid">${item(g, 1).map((c) => html`<div><div style="${LBL}">${item(c, 0)}</div><div class="num" style="font-size:var(--fs-xl);font-weight:600;margin-top:3px">${item(c, 1)}</div></div>`)}</div></div>`)}</div>`));
  }

  if (fd.trend && fd.trend.length) {
    out.push(card('Quarterly trend', html`<div class="card__bd" style="display:flex;gap:20px;flex-wrap:wrap">${barChart(fd.trend, 'revenue', 'Revenue')}${barChart(fd.trend, 'net_income', 'Net income')}${barChart(fd.trend, 'fcf', 'Free cash flow')}</div>`,
      html`<span class="sub">last ${fd.trend.length} periods</span>`));
  }

  if (fd.earnings && fd.earnings.length) {
    out.push(card('Recent earnings', html`<div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Period</th><th>EPS actual</th><th>EPS est.</th><th>Surprise</th><th>Revenue</th></tr></thead><tbody>${fd.earnings.map((e) => {
      const sc = e.eps_surprise === 'BEAT' ? 'tag--up' : e.eps_surprise === 'MISS' ? 'tag--down' : 'tag--neu';
      return html`<tr><td style="text-align:left">${e.period || '—'}</td><td class="num">${e.eps_actual != null ? F.num(Number(e.eps_actual), 2) : '—'}</td><td class="num" style="color:var(--muted)">${e.eps_estimate != null ? F.num(Number(e.eps_estimate), 2) : '—'}</td><td>${e.eps_surprise ? html`<span class="tag ${sc}">${e.eps_surprise}</span>` : '—'}</td><td class="num">${e.revenue_actual != null ? F.compact(e.revenue_actual) : '—'}</td></tr>`;
    })}</tbody></table></div>`));
  }

  const filings = (fd.filings || []).map((f) => {
    const url = safeUrl(f.url);
    return html`<tr><td style="text-align:left" class="num">${f.date || '—'}</td><td style="text-align:left">${f.type || '—'}</td><td>${url ? html`<a href="${url}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">open</a>` : '—'}</td></tr>`;
  });
  const insiders = (fd.insiders || []).map((t) => {
    const low = String(t.type || '').toLowerCase();
    const col = /sale|sold/.test(low) ? 'var(--down)' : /purchase|buy/.test(low) ? 'var(--up)' : 'inherit';
    return html`<tr><td style="text-align:left" class="num">${t.date || '—'}</td><td style="text-align:left">${t.name || '—'}</td><td style="text-align:left;color:${col}">${t.type || '—'}</td><td class="num">${t.value != null ? F.compact(t.value) : '—'}</td></tr>`;
  });
  out.push(html`<div class="row2" style="margin-top:20px;grid-template-columns:1fr 1fr"><section class="card"><div class="card__hd"><h2>Recent filings</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Date</th><th style="text-align:left">Type</th><th>Link</th></tr></thead><tbody>${filings.length ? filings : emptyRow(3, 'No filings.')}</tbody></table></div></section><section class="card"><div class="card__hd"><h2>Insider activity</h2></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Date</th><th style="text-align:left">Name</th><th style="text-align:left">Type</th><th>Value</th></tr></thead><tbody>${insiders.length ? insiders : emptyRow(4, 'No insider activity.')}</tbody></table></div></section></div>`);

  const holders = (fd.holders || []).map((h) => html`<tr><td style="text-align:left">${h.investor || '—'}</td><td class="num" style="color:var(--muted)">${h.period || '—'}</td><td class="num">${h.shares != null ? F.integer(h.shares) : '—'}</td><td class="num">${h.value != null ? F.compact(h.value) : '—'}</td></tr>`);
  out.push(card('Top institutional holders', html`<div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Investor</th><th>Period</th><th>Shares</th><th>Value</th></tr></thead><tbody>${holders.length ? holders : emptyRow(4, 'No 13F holdings.')}</tbody></table></div>`));

  setHTML(host, html`${out}`);
}
