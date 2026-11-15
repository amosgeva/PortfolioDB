// US equity session state, computed in New York time.
//
// The holiday list used to be a literal table for 2026-2030 that would have
// gone silently wrong on 2031-01-01, and it did not know about early closes.
// It is now derived from the NYSE rules (Rule 7.2 and the published calendar):
//
//   closed  New Year's Day, MLK Day (3rd Mon Jan), Washington's Birthday
//           (3rd Mon Feb), Good Friday, Memorial Day (last Mon May),
//           Juneteenth (from 2022), Independence Day, Labor Day (1st Mon Sep),
//           Thanksgiving (4th Thu Nov), Christmas Day.
//   observe a Saturday holiday on the Friday before and a Sunday holiday on
//           the Monday after - except New Year's Day on a Saturday, which the
//           NYSE does not observe at all (it would fall in the prior year).
//   13:00   early close on July 3 when it is Mon-Thu, on the day after
//           Thanksgiving, and on December 24 when it is Mon-Thu.
//
// What no rule can know is an unscheduled closure (a national day of
// mourning, a weather event). The pill then says "Market open" on a day the
// exchange is shut; it is a clock, and it is labelled as one.

function ymd(y, m, d) {
  return y + '-' + String(m).padStart(2, '0') + '-' + String(d).padStart(2, '0');
}
function dow(y, m, d) { return new Date(Date.UTC(y, m - 1, d)).getUTCDay(); }
function nthWeekday(y, m, weekday, n) {
  const first = dow(y, m, 1);
  return 1 + ((weekday - first + 7) % 7) + (n - 1) * 7;
}
function lastWeekday(y, m, weekday) {
  const days = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const last = dow(y, m, days);
  return days - ((last - weekday + 7) % 7);
}
// Anonymous Gregorian computus (Meeus/Jones/Butcher).
function easter(y) {
  const a = y % 19, b = Math.floor(y / 100), c = y % 100, d = Math.floor(b / 4), e = b % 4;
  const f = Math.floor((b + 8) / 25), g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30, i = Math.floor(c / 4), k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7, m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31), day = ((h + l - 7 * m + 114) % 31) + 1;
  return [month, day];
}
function shift(y, m, d, by) {
  const t = new Date(Date.UTC(y, m - 1, d + by));
  return ymd(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
}
function observed(y, m, d) {
  const w = dow(y, m, d);
  if (w === 6) return shift(y, m, d, -1);
  if (w === 0) return shift(y, m, d, 1);
  return ymd(y, m, d);
}

/** Full-day NYSE closures in `year`, as { 'YYYY-MM-DD': name }. */
export function nyseHolidays(year) {
  const out = {};
  const y = year;
  if (dow(y, 1, 1) !== 6) out[observed(y, 1, 1)] = 'New Year’s Day';
  out[ymd(y, 1, nthWeekday(y, 1, 1, 3))] = 'Martin Luther King Jr. Day';
  out[ymd(y, 2, nthWeekday(y, 2, 1, 3))] = 'Washington’s Birthday';
  const [em, ed] = easter(y);
  out[shift(y, em, ed, -2)] = 'Good Friday';
  out[ymd(y, 5, lastWeekday(y, 5, 1))] = 'Memorial Day';
  if (y >= 2022) out[observed(y, 6, 19)] = 'Juneteenth';
  out[observed(y, 7, 4)] = 'Independence Day';
  out[ymd(y, 9, nthWeekday(y, 9, 1, 1))] = 'Labor Day';
  out[ymd(y, 11, nthWeekday(y, 11, 4, 4))] = 'Thanksgiving Day';
  out[observed(y, 12, 25)] = 'Christmas Day';
  return out;
}

/** 13:00 ET early closes in `year`, as { 'YYYY-MM-DD': reason }. */
export function nyseEarlyCloses(year) {
  const out = {};
  const y = year;
  const j3 = dow(y, 7, 3);
  if (j3 >= 1 && j3 <= 4) out[ymd(y, 7, 3)] = 'Independence Day eve';
  out[ymd(y, 11, nthWeekday(y, 11, 4, 4) + 1)] = 'Day after Thanksgiving';
  const x24 = dow(y, 12, 24);
  if (x24 >= 1 && x24 <= 4) out[ymd(y, 12, 24)] = 'Christmas Eve';
  return out;
}

const MIN = { pre: 4 * 60, open: 9 * 60 + 30, close: 16 * 60, early: 13 * 60, after: 20 * 60, earlyAfter: 17 * 60 };

/** New York wall-clock parts for `date`. */
export function etParts(date) {
  const p = {};
  new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
    .formatToParts(date).forEach((x) => { p[x.type] = x.value; });
  return p;
}

/** { level: open|pre|after|closed, label } for the session at `date`. */
export function marketState(date) {
  const p = etParts(date || new Date());
  const wd = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }[p.weekday];
  const day = p.year + '-' + p.month + '-' + p.day;
  const mins = (Number.parseInt(p.hour, 10) % 24) * 60 + Number.parseInt(p.minute, 10);
  if (wd === 0 || wd === 6) return { level: 'closed', label: 'Closed' };
  const year = Number(p.year);
  const hol = nyseHolidays(year)[day];
  if (hol) return { level: 'closed', label: 'Holiday', detail: hol };
  const early = nyseEarlyCloses(year)[day];
  const close = early ? MIN.early : MIN.close;
  const afterEnd = early ? MIN.earlyAfter : MIN.after;
  if (mins >= MIN.open && mins < close) return { level: 'open', label: early ? 'Open · early close 13:00' : 'Market open' };
  if (mins >= MIN.pre && mins < MIN.open) return { level: 'pre', label: 'Pre-market' };
  if (mins >= close && mins < afterEnd) return { level: 'after', label: 'After-hours' };
  return { level: 'closed', label: 'Closed' };
}
