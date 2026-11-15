// Statistics: records, monthly grid, consistency and streaks.
//
// Everything here reads DATA.stats, which the server computes from the same
// TWR growth curve as the returns strip - so "best month" and MTD cannot
// disagree. Partial periods are excluded from records by the server and are
// hatched, not hidden, in the grid.

import { $ } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app } from '../core/state.js';
import { pctTxt } from '../core/format.js';
import { item } from '../core/dict.js';

function recCard(label, rec, meta) {
  if (!rec) return html`<div class="rec"><div class="rec__lbl">${label}</div><div class="rec__val">—</div><div class="rec__meta">${meta || 'not enough history'}</div></div>`;
  const dir = rec.return_pct >= 0 ? 'up' : 'down';
  const when = rec.start === rec.end ? rec.start : rec.start + ' → ' + rec.end;
  return html`<div class="rec rec--${dir}"><div class="rec__lbl">${label}</div><div class="rec__val">${pctTxt(rec.return_pct)}</div><div class="rec__when">${rec.label}</div><div class="rec__meta">${when}</div></div>`;
}

function streakRow(lbl, r) {
  if (!r) return html`<div class="stk"><div class="stk__lbl">${lbl}</div><div class="stk__days">—</div></div>`;
  return html`<div class="stk stk--${r.direction === 'up' ? 'up' : 'down'}"><div class="stk__lbl">${lbl}</div><div class="stk__days">${r.days}d</div><div class="stk__meta">${pctTxt(r.return_pct) + ' · ' + r.start + ' → ' + r.end}</div></div>`;
}

// Shade a month cell by size relative to the largest absolute move on the grid.
// background-color, not the shorthand: the shorthand resets background-image
// and defeated the partial-period hatch.
function monthTint(v, peak) {
  if (v == null || !peak) return '';
  const a = Math.min(Math.abs(v) / peak, 1) * 0.42 + 0.06;
  return 'background-color:' + (v >= 0 ? 'oklch(62% .17 152/' + a.toFixed(3) + ')' : 'oklch(58% .19 26/' + a.toFixed(3) + ')') + ';';
}

export function renderStats() {
  const st = app.D.stats;
  const recs = $('#st-recs'), months = $('#st-months'), hit = $('#st-hit'), stk = $('#st-streaks');
  if (!recs) return;
  if (!st || !st.ok) {
    const why = st && st.null_reason === 'insufficient_history'
      ? 'Not enough price history yet — statistics need at least two snapshot days.'
      : 'No statistics available.';
    setHTML(recs, html`<div class="empty">${why}</div>`);
    setHTML(months, html``); setHTML(hit, html``); setHTML(stk, html``);
    return;
  }
  const cov = $('#st-coverage');
  if (cov && st.coverage) cov.textContent = st.coverage.days + ' trading days · ' + st.coverage.start + ' → ' + st.coverage.end;

  const none = (b, what) => (b && b.null_reason ? 'no complete ' + what + ' yet' : null);
  setHTML(recs, html`${recCard('Best day', st.day.best)}${recCard('Worst day', st.day.worst)}${recCard('Best week', st.week.best, none(st.week, 'weeks'))}${recCard('Worst week', st.week.worst, none(st.week, 'weeks'))}${recCard('Best month', st.month.best, none(st.month, 'months'))}${recCard('Worst month', st.month.worst, none(st.month, 'months'))}`);

  if (months) {
    const tbl = st.monthly_table || { labels: [], rows: [] };
    let peak = 0;
    tbl.rows.forEach((r) => r.months.forEach((m) => { if (m != null) peak = Math.max(peak, Math.abs(m)); }));
    const head = html`<tr><th class="stbl__yr">Year</th>${tbl.labels.map((l) => html`<th>${l}</th>`)}<th>Year</th></tr>`;
    const body = tbl.rows.map((r) => html`<tr><td class="stbl__yr">${r.year}</td>${r.months.map((m, i) => {
      const partial = (r.partial_months || []).indexOf(item(tbl.labels, i)) >= 0;
      const cls = 'stbl__m' + (m == null ? '' : ' on') + (partial ? ' part' : '');
      const title = m == null ? 'no observations' : item(tbl.labels, i) + ' ' + r.year + ': ' + pctTxt(m) + (partial ? ' (partial period)' : '');
      return html`<td class="${cls}" style="${monthTint(m, peak)}" title="${title}">${m == null ? '·' : pctTxt(m, 1)}</td>`;
    })}<td class="tot${r.year_pct == null ? '' : ' on'}" title="${r.months_observed + ' month(s) observed'}">${pctTxt(r.year_pct, 1)}</td></tr>`);
    setHTML(months, html`${head}${body}`);
  }

  if (hit) {
    setHTML(hit, html`${['day', 'week', 'month'].map((k) => {
      const b = item(st, k), decided = b.positive + b.negative;
      const upPct = decided ? (b.positive / decided) * 100 : 0;
      const note = b.partial_excluded ? b.complete + ' complete (' + b.partial_excluded + ' partial excluded)' : b.complete + ' ' + k + 's';
      return html`<div class="hitrow"><div class="hitrow__lbl">${k}s</div><div class="hitrow__track"><div class="hitrow__up" style="width:${upPct.toFixed(1)}%"></div><div class="hitrow__down" style="width:${(100 - upPct).toFixed(1)}%"></div></div><div class="hitrow__val" title="${note}">${b.hit_rate_pct == null ? '—' : b.hit_rate_pct.toFixed(0) + '% up'} <span style="color:var(--faint)">· ${b.complete}</span></div></div>`;
    })}<div style="margin-top:12px;font-size:var(--fs-meta);color:var(--muted)">${'Average up month ' + pctTxt(st.month.best_average_pct) + ' · average down month ' + pctTxt(st.month.worst_average_pct)}</div>`);
  }

  if (stk) {
    const s = st.streaks || {};
    setHTML(stk, html`${streakRow('Longest up', s.longest_up)}${streakRow('Longest down', s.longest_down)}${streakRow('Current', s.current && s.current.direction !== 'flat' ? s.current : null)}`);
  }
}
