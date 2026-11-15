// The payload and the few derived values every view reads.
//
// One mount per frame: Streamlit builds a fresh iframe on every rerun, and the
// bundle runs once in it, so this module's state is set once by initState()
// and never mutated by a later payload.

import { readConfig } from './config.js';

export const app = {
  D: {},
  cfg: readConfig({}),
  stocks: {},
  cash: 0,
};

export function initState(D) {
  app.D = D && typeof D === 'object' ? D : {};
  app.cfg = readConfig(app.D);
  // Without a config block, the symbol the server actually computed against
  // (returns.benchmarkSymbol / risk.benchmark) beats the SPY default.
  const c = app.D.config && typeof app.D.config === 'object' ? app.D.config : {};
  if (!c.benchmark_symbol) {
    const used = (app.D.returns || {}).benchmarkSymbol || (app.D.risk || {}).benchmark;
    if (typeof used === 'string' && used.trim()) {
      app.cfg.benchmarkSymbol = used.trim();
      if (!c.benchmark_label) app.cfg.benchmarkLabel = used.trim();
    }
  }
  app.stocks = app.D.stocks || {};
  app.cash = Number(app.D.cash) || 0;
}

export function get(sym) {
  return Object.prototype.hasOwnProperty.call(app.stocks, sym) ? app.stocks[sym] : undefined;
}
export function list() { return Object.keys(app.stocks).map((k) => app.stocks[k]); }

/** Open positions joined to their latest quote. */
export function holdingRows() {
  return (app.D.holdings || []).map((h) => {
    const s = get(h.sym);
    if (!s) return null;
    const mktVal = s.price * h.qty, cost = h.avgCost * h.qty, gl = mktVal - cost;
    return { sym: s.sym, name: s.name, sector: s.sector, hist: s.hist || [], qty: h.qty, avgCost: h.avgCost,
      price: s.price, day: s.day, dayPct: s.dayPct, mktVal, cost, gl, glPct: cost ? (gl / cost) * 100 : 0 };
  }).filter(Boolean);
}

export function totals(rs) {
  const t = { mktVal: 0, day: 0, gl: 0, cost: 0 };
  rs.forEach((r) => { t.mktVal += r.mktVal; t.day += (r.day || 0) * r.qty; t.gl += r.gl; t.cost += r.cost; });
  return t;
}

// symLots carries every lot per symbol (payload builds it from the same rows
// the P&L engine uses), so a truncated list can state what it is truncating.
export function lotTotal(sym) { return ((app.D.symLots || {})[sym] || []).length; }
export function lotTotalAll() {
  const m = app.D.symLots || {};
  let n = 0;
  Object.keys(m).forEach((k) => { n += m[k].length; });
  return n;
}

/** The price series used for the benchmark overlay, or null. */
export function benchmarkSeries() {
  const ph = app.D.priceHist || {};
  const s = ph[app.cfg.benchmarkSymbol];
  return Array.isArray(s) && s.length > 1 ? s : null;
}
