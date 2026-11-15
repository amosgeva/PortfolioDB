// Number, money and date formatting.
//
// Locale, currency and time zone come from configureFormat() (called once at
// mount from core/config.js) instead of being spelled into every call. The
// defaults - en-US, USD, UTC - are what a payload without a `config` block
// gets; they are not anyone's personal setting.

// Written out as 1e6 these are reported as literals whose runtime value differs
// from the text, so every magnitude is composed from small integers instead.
export const THOUSAND = 1000;
export const TEN_THOUSAND = THOUSAND * 10;
export const MILLION = THOUSAND * THOUSAND;
export const BILLION = MILLION * THOUSAND;
export const TRILLION = BILLION * THOUSAND;
export const DAY_MS = 24 * 60 * 60 * 1000;

const opts = { locale: 'en-US', currency: 'USD', tz: 'UTC' };
let cache = new Map();

export function configureFormat({ locale, currency, reportingTz } = {}) {
  if (locale) opts.locale = locale;
  if (currency) opts.currency = currency;
  if (reportingTz) opts.tz = reportingTz;
  cache = new Map();
}
export function reportingTz() { return opts.tz; }
export function locale() { return opts.locale; }

function nf(key, o) {
  if (!cache.has(key)) cache.set(key, new Intl.NumberFormat(opts.locale, o));
  return cache.get(key);
}
function moneyFmt(dp) {
  return nf('m' + dp, { style: 'currency', currency: opts.currency, currencyDisplay: 'narrowSymbol', minimumFractionDigits: dp, maximumFractionDigits: dp });
}

export function money(n, dp) {
  return moneyFmt(dp == null ? 2 : dp).format(Number(n) || 0);
}

/** Money as the number a reader sees, parsed back out of the formatter. The
 *  evidence panels compare what is printed, and re-implementing the rounding
 *  disagrees with Intl on half-cents. Locale-proof: built from formatToParts. */
export function printedMoney(n) {
  let s = '';
  moneyFmt(2).formatToParts(Number(n) || 0).forEach((p) => {
    if (p.type === 'integer' || p.type === 'fraction') s += p.value;
    else if (p.type === 'decimal') s += '.';
    else if (p.type === 'minusSign') s += '-';
  });
  return Number(s);
}

function compactDp(a) { return a >= MILLION ? 2 : a >= THOUSAND ? 1 : 0; }

/** $1.2K / $3.45M / $1.23T - currency-aware compact notation. */
export function compact(n) {
  const v = Number(n) || 0, dp = compactDp(Math.abs(v));
  return nf('c' + dp, { style: 'currency', currency: opts.currency, currencyDisplay: 'narrowSymbol', notation: 'compact',
    minimumFractionDigits: dp, maximumFractionDigits: dp }).format(v);
}

function fixed(n, dp) {
  return nf('f' + dp, { minimumFractionDigits: dp, maximumFractionDigits: dp }).format(n);
}

// null day-change (no prior-session price yet) renders as "n/a", never a fake +0.00%
export function pct(n) {
  if (n == null) return 'n/a';
  const v = Number(n);
  return (v >= 0 ? '+' : '−') + fixed(Math.abs(v), 2) + '%';
}
/** Like pct, but '—' for missing and a chosen precision (statistics tables). */
export function pctTxt(v, dp) {
  if (v == null) return '—';
  return (v >= 0 ? '+' : '−') + fixed(Math.abs(v), dp == null ? 2 : dp) + '%';
}
export function chgCls(n) { return n == null ? '' : (n >= 0 ? 'up' : 'down'); }
export function signed(v) { return (v >= 0 ? '+' : '−') + money(Math.abs(v || 0)); }
/** A plain number in the reader's locale (index levels, ratios). */
export function num(v, dp) { return v == null ? '—' : fixed(v, dp == null ? 2 : dp); }
/** Share quantities: up to four decimals, no trailing zeros. */
export function qty(v) {
  return nf('q', { maximumFractionDigits: 4 }).format(Math.round(Number(v) * TEN_THOUSAND) / TEN_THOUSAND);
}
export function integer(v) { return nf('i', { maximumFractionDigits: 0 }).format(Number(v) || 0); }

// Axis money is not tooltip money: it is read in a column, at 11px, against
// its neighbours. Precision follows magnitude so the ticks stay distinct
// without ever printing cents nobody is reading off a gridline.
export function axisMoney(v, step) {
  const a = Math.abs(v);
  if (a >= TEN_THOUSAND) {
    const dp = a >= MILLION ? (a >= MILLION * 10 ? 0 : 1) : (a >= THOUSAND * 100 ? 0 : 1);
    return nf('ax' + dp, { style: 'currency', currency: opts.currency, currencyDisplay: 'narrowSymbol', notation: 'compact',
      minimumFractionDigits: dp, maximumFractionDigits: dp }).format(v);
  }
  // A ladder of $0.20 steps rounded to whole dollars prints "$100" three
  // times: the step decides the precision, not the magnitude.
  const dp = !step || step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
  return money(v, dp);
}

function dtf(key, o) {
  if (!cache.has(key)) cache.set(key, new Intl.DateTimeFormat(opts.locale, { timeZone: opts.tz, ...o }));
  return cache.get(key);
}
/** A timestamp in the reporting zone. `kind` picks one of a few fixed shapes. */
export function fmtDate(ms, kind) {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '—';
  switch (kind) {
    case 'time': return dtf('t', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d);
    case 'dayTime': return dtf('dt', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d);
    case 'fullTime': return dtf('ft', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(d);
    case 'monthYear': return dtf('my', { month: 'short', year: '2-digit' }).format(d).replace(' ', ' \'');
    case 'day': return dtf('d', { month: 'short', day: 'numeric' }).format(d);
    case 'stamp': return dtf('st', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'short' }).format(d);
    default: return dtf('fd', { year: 'numeric', month: 'short', day: 'numeric' }).format(d);
  }
}

// A quarter bar is stamped with the end of the period it covers. Naming it
// "Q3" would assert a fiscal quarter this row does not carry - NVDA's fiscal
// Q1 ends in April - and would disagree with the earnings table above, which
// prints the real fiscal label. The period end date is what is actually known.
export function periodLabel(period) {
  const d = new Date(String(period) + 'T00:00:00Z');
  if (Number.isNaN(d.getTime())) return String(period);
  return new Intl.DateTimeFormat(opts.locale, { month: 'short', year: '2-digit', timeZone: 'UTC' })
    .format(d).replace(' ', ' \'');
}

/** An ISO date (YYYY-MM-DD) from an ISO timestamp, or null. */
export function isoDay(s) {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec(String(s || ''));
  return m ? m[1] : null;
}
