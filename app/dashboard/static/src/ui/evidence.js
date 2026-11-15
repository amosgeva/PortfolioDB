// KPI evidence: every tile states a conclusion, and this is the route from the
// claim to its terms and to the rows they were summed from.

import { html } from '../core/html.js';
import { app, holdingRows, totals } from '../core/state.js';
import * as F from '../core/format.js';
import { cashEntries, entryDate } from '../views/kpis.js';

function evEq(rows, result) {
  return html`<table class="ev-eq">${rows.map((r) => html`<tr><td class="ev-op">${r[2] || ''}</td><td>${r[0]}</td><td class="num${r[3] ? ' ' + r[3] : ''}">${r[1]}</td></tr>`)}<tr class="ev-sum"><td class="ev-op"></td><td>${result[0]}</td><td class="num${result[2] ? ' ' + result[2] : ''}">${result[1]}</td></tr></table>`;
}
// The server rounds each term to the cent and the total from unrounded inputs,
// so the printed terms can miss the printed total by a penny. The gap is named
// where it happens. Compared as printed (read back out of the formatter), not
// as held: re-implementing the rounding disagrees with Intl on half-cents.
export function evRounding(terms, value) {
  let sum = 0;
  terms.forEach((t) => { sum += F.printedMoney(t); });
  if (!(Math.abs(sum - F.printedMoney(value)) >= 0.005)) return '';
  return html`<p class="ev-round">Terms are rounded to the cent; the total is taken before rounding, so the column can differ from it by a penny.</p>`;
}
function evTable(head, body) {
  if (!body.length) return '';
  return html`<div class="tbl-wrap"><table class="tbl ev-tbl"><thead><tr>${head.map((h, i) => (i ? html`<th>${h}</th>` : html`<th style="text-align:left">${h}</th>`))}</tr></thead><tbody>${body.map((r) => html`<tr>${r.map((c, i) => (i ? html`<td class="num">${c}</td>` : html`<td>${c}</td>`))}</tr>`)}</tbody></table></div>`;
}
const bold = (s) => html`<b>${s}</b>`;
const tone = (v, text) => html`<span class="${v >= 0 ? 'up' : 'down'}">${text}</span>`;
const CASH_LABEL = 'Cash (as entered)';

export function kpiEvidence(key, routes) {
  const k = app.D.kpi || {};
  const rs = holdingRows().slice().sort((a, b) => b.mktVal - a.mktVal);
  const t = totals(rs);
  const cash = app.cash;
  const holdings = { label: 'See holdings', run: routes.goHoldings };
  switch (key) {
    case 'totalValue': return {
      title: 'Total value', value: F.money(k.totalValue || 0),
      what: 'Everything this ledger can put a price on: your open positions at their last recorded price, plus the cash balance you entered.',
      eq: html`${evEq([['Market value', F.money(k.marketValue || 0), ''], [CASH_LABEL, F.money(k.cash || 0), '+']], ['Total value', F.money(k.totalValue || 0)])}${evRounding([k.marketValue || 0, k.cash || 0], k.totalValue || 0)}`,
      table: evTable(['Symbol', 'Value', 'Weight'], rs.map((r) => [bold(r.sym), F.money(r.mktVal), ((k.totalValue ? r.mktVal / k.totalValue : 0) * 100).toFixed(1) + '%'])
        .concat(cash > 0 ? [['Cash', F.money(cash), ((k.totalValue ? cash / k.totalValue : 0) * 100).toFixed(1) + '%']] : [])),
      tableNote: evRounding(rs.map((r) => r.mktVal).concat(cash > 0 ? [cash] : []), k.totalValue || 0),
      route: holdings,
    };
    case 'marketValue': {
      const rest = rs.slice(3).reduce((a, r) => a + r.mktVal, 0);
      return {
        title: 'Market value', value: F.money(k.marketValue || 0),
        what: 'Each open position’s quantity multiplied by the most recent price recorded for it. Cash is not included.',
        eq: html`${evEq(rs.slice(0, 3).map((r, i) => [r.sym + '  ' + F.qty(r.qty) + ' × ' + F.money(r.price), F.money(r.mktVal), i ? '+' : ''])
          .concat(rs.length > 3 ? [[(rs.length - 3) + ' more positions', F.money(rest), '+']] : []), ['Market value', F.money(t.mktVal)])}${evRounding(rs.slice(0, 3).map((r) => r.mktVal).concat(rs.length > 3 ? [rest] : []), t.mktVal)}`,
        table: evTable(['Symbol', 'Qty', 'Price', 'Value'], rs.map((r) => [bold(r.sym), F.qty(r.qty), F.money(r.price), F.money(r.mktVal)])),
        tableNote: evRounding(rs.map((r) => r.mktVal), t.mktVal),
        route: holdings,
      };
    }
    case 'unrealized': return {
      title: 'Unrealized P&L', value: F.signed(k.unrealized),
      what: 'What your open positions are worth now, less what they cost you. Nothing here has been sold, so none of it is booked.',
      eq: html`${evEq([['Market value', F.money(k.marketValue || 0), ''], ['Cost basis', F.money(k.costBasis || 0), '−']], ['Unrealized P&L', F.signed(k.unrealized), (k.unrealized || 0) >= 0 ? 'up' : 'down'])}${evRounding([k.marketValue || 0, -(k.costBasis || 0)], k.unrealized || 0)}`,
      table: evTable(['Symbol', 'Cost', 'Value', 'Gain'], rs.slice().sort((a, b) => b.gl - a.gl).map((r) => [bold(r.sym), F.money(r.cost), F.money(r.mktVal), tone(r.gl, F.signed(r.gl))])),
      tableNote: evRounding(rs.map((r) => r.gl), k.unrealized || 0),
      route: holdings,
    };
    case 'cash': {
      const accts = cashEntries();
      return {
        title: CASH_LABEL, value: F.money(k.cash || 0),
        what: 'The balance you last typed in for each account. PortfolioDB never contacts a broker, so this is not settled cash or buying power: it is exactly as current as your last entry and no more.',
        eq: accts.length > 1 ? html`${evEq(accts.map((a, i) => [a.account, F.money(a.cash), i ? '+' : '']), [CASH_LABEL, F.money(k.cash || 0)])}${evRounding(accts.map((a) => a.cash), k.cash || 0)}` : '',
        table: evTable(['Account', 'Balance', 'Entered'], accts.map((a) => [bold(a.account), F.money(a.cash), a.asOf ? entryDate(a.asOf) : '—'])),
        note: 'Update it from Manage → Cash, or with make set-cash.',
      };
    }
    case 'realized': return {
      title: 'Realized P&L', value: F.signed(k.realized),
      what: 'Profit and loss already booked by selling, matched first-in-first-out against the lots you bought. Fees paid on a sale reduce it.',
      eq: '',
      table: evTable(['Symbol', 'Realized'], (k.realizedBySymbol || []).map((r) => [bold(r.sym), tone(r.realized, F.signed(r.realized))])),
      tableNote: evRounding((k.realizedBySymbol || []).map((r) => r.realized), k.realized || 0),
      note: 'Every match is recomputed from the lots on each read — there is no stored total to drift.',
      route: { label: 'See every lot', run: () => routes.goView('history') },
    };
    case 'totalReturn': return {
      title: 'Total return', value: F.pct(k.totalReturnPct || 0),
      what: 'Everything you have made, booked or not, against what you put in.',
      eq: evEq([['Realized P&L', F.signed(k.realized), ''], ['Unrealized P&L', F.signed(k.unrealized), '+'], ['Cost basis', F.money(k.costBasis || 0), '÷']],
        ['Total return', F.pct(k.totalReturnPct || 0), (k.totalReturnPct || 0) >= 0 ? 'up' : 'down']),
      table: '',
      note: 'Dividends are excluded here. With them it is ' + F.pct(k.totalReturnWithIncomePct || 0) + '.',
    };
    case 'costBasis': return {
      title: 'Cost basis', value: F.money(k.costBasis || 0),
      what: 'What your open lots cost, including the fees you paid to buy them. Sold lots have left this number.',
      eq: '',
      table: evTable(['Symbol', 'Qty', 'Avg cost', 'Cost'], rs.slice().sort((a, b) => b.cost - a.cost).map((r) => [bold(r.sym), F.qty(r.qty), F.money(r.avgCost), F.money(r.cost)])),
      tableNote: evRounding(rs.map((r) => r.cost), k.costBasis || 0),
      note: 'Fees paid so far: ' + F.money(k.totalFees || 0) + ', which is ' + F.pct(k.feeDragPct || 0) + ' of cost.',
      route: holdings,
    };
    case 'deltaLast': {
      const pv = (app.D.pv && (app.D.pv['1D'] || app.D.pv['1W'])) || [];
      const prev = pv.length > 1 ? pv[pv.length - 2][1] : null;
      const last = pv.length ? pv[pv.length - 1][1] : null;
      return {
        title: 'Change since last snapshot', value: F.signed(k.deltaLast),
        what: 'How the portfolio moved between the two most recent price collections. It is a step, not a day: if the collector missed a run, this spans the gap.',
        eq: (prev != null && last != null)
          ? html`${evEq([['These holdings, latest prices', F.money(last), ''], ['These holdings, previous prices', F.money(prev), '−']], ['Change', F.signed(k.deltaLast), (k.deltaLast || 0) >= 0 ? 'up' : 'down'])}${evRounding([last, -prev], k.deltaLast || 0)}`
          : '',
        table: '',
        note: 'Counted over the ' + (k.deltaLastSyms || 0) + ' holding' + (k.deltaLastSyms === 1 ? '' : 's') +
          ' priced in both snapshots. Snapshot times and gaps are on the Data Health page.',
      };
    }
    case 'dividends': return {
      title: 'Dividends', value: F.money(k.dividends || 0),
      what: 'Income recorded against your holdings. Nothing is estimated or projected here — these are payments entered into the ledger.',
      eq: evEq([['All time', F.money(k.dividends || 0), ''], ['Last twelve months', F.money(k.dividendsTtm || 0), ''], ['Cost basis', F.money(k.costBasis || 0), '÷']],
        ['Yield on cost (TTM)', F.pct(k.yieldOnCostPct || 0)]),
      table: '',
    };
    default: return null;
  }
}
