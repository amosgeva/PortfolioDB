// Display configuration, read from the payload's `config` block.
//
// Every field has a fallback because the block is new: a payload built before
// it existed (or by a server that has not been upgraded) must still render.
// The fallbacks are the honest defaults, not the operator's old ones - the
// reporting zone falls back to the payload's own `reportingTz` and then to
// UTC (the server default), never to a city; the logo CDN stays off unless the
// operator turned it on, because a request to it discloses every symbol.

function str(v) { return typeof v === 'string' && v.trim() ? v.trim() : null; }

export function validTimeZone(tz) {
  if (!str(tz)) return null;
  try { new Intl.DateTimeFormat('en-US', { timeZone: tz }); return tz; } catch (e) { return null; }
}

export function validCurrency(c) {
  if (!str(c) || !/^[A-Za-z]{3}$/.test(c)) return null;
  try { new Intl.NumberFormat('en-US', { style: 'currency', currency: c }); return c.toUpperCase(); }
  catch (e) { return null; }
}

function browserLocale() {
  try {
    const n = typeof navigator !== 'undefined' ? navigator : null;
    const l = n && ((n.languages && Array.from(n.languages).at(0)) || n.language);
    if (l) { new Intl.NumberFormat(l); return l; }
  } catch (e) { /* unsupported locale tag: fall through */ }
  return 'en-US';
}

export function readConfig(D) {
  const c = (D && typeof D.config === 'object' && D.config) || {};
  const benchmarkSymbol = str(c.benchmark_symbol) || 'SPY';
  return {
    benchmarkSymbol,
    benchmarkLabel: str(c.benchmark_label) || benchmarkSymbol,
    reportingTz: validTimeZone(c.reporting_tz) || validTimeZone(D && D.reportingTz) || 'UTC',
    currency: validCurrency(c.currency) || 'USD',
    logoCdn: c.logo_cdn === true,
    locale: browserLocale(),
  };
}
