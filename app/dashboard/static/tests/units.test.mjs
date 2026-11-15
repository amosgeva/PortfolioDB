// Pure-module tests, imported straight from src/ (no DOM needed).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { html, attrs, esc, join, isSafe, setHTML, safeUrl } from '../src/core/html.js';
import { csvCell, toCsv } from '../src/core/csv.js';
import { nyseHolidays, nyseEarlyCloses, marketState } from '../src/core/market.js';
import { readConfig, validTimeZone, validCurrency } from '../src/core/config.js';
import * as F from '../src/core/format.js';
import { splitMovers } from '../src/views/movers.js';

test('html escapes every interpolation and keeps nested templates', () => {
  const bad = '<img src=x onerror=alert(1)>"\'&`';
  assert.equal(html`<b title="${bad}">${bad}</b>`.s,
    '<b title="&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;&#96;">&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;&#96;</b>');
  assert.equal(html`<i>${html`<b>${bad}</b>`}</i>`.s, '<i><b>&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;&#96;</b></i>');
  assert.equal(html`${['<a>', html`<b>`]}`.s, '&lt;a&gt;<b>');
  assert.equal(html`${null}${undefined}${false}${0}`.s, '0');
  // Numeric entities in data stay text: the & is escaped.
  assert.equal(html`${'&#60;script&#62;'}`.s, '&amp;#60;script&amp;#62;');
  assert.equal(join(['<', html`<br>`], html`|`).s, '&lt;|<br>');
  assert.equal(esc(null), '');
});

test('html cannot be fed a concatenated string, and setHTML takes only html output', () => {
  assert.throws(() => html(['<b>' + 'x' + '</b>']), /tagged template/);
  const fake = Object.freeze(['<b>x</b>']);
  assert.throws(() => html(fake), /tagged template/, 'a frozen array without a frozen raw is not a template');
  assert.equal(isSafe({ s: '<b>' }), false, 'a look-alike object is not safe');
  const el = { innerHTML: '' };
  assert.throws(() => setHTML(el, '<b>x</b>'), /html`` output only/);
  assert.throws(() => setHTML(el, { s: '<b>x</b>' }), /html`` output only/);
  setHTML(el, html`<b>${'<i>'}</b>`);
  assert.equal(el.innerHTML, '<b>&lt;i&gt;</b>');
});

test('safeUrl admits http(s), and data images only when asked', () => {
  assert.equal(String(safeUrl('https://example.com/a?b=1')), 'https://example.com/a?b=1');
  assert.equal(String(safeUrl(' http://x.test ')), 'http://x.test');
  for (const u of ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', ' javascript:x', 'vbscript:x', 'data:text/html,<b>', '/relative', '//host/x', null, 5]) {
    assert.equal(safeUrl(u), null, String(u));
  }
  assert.equal(safeUrl('data:image/png;base64,AAAA'), null);
  assert.equal(String(safeUrl('data:image/png;base64,AAAA', { allowDataImage: true })), 'data:image/png;base64,AAAA');
  assert.equal(String(safeUrl('data:image/svg+xml;base64,PHN2Zz4=', { allowDataImage: true })), 'data:image/svg+xml;base64,PHN2Zz4=');
  assert.equal(safeUrl('data:image/png;base64,AA" onerror="x', { allowDataImage: true }), null);
});

test('csvCell neutralises formula triggers in text and leaves numbers numeric (A-4)', () => {
  for (const t of ['=1+1', '+1', '-1+2', '@SUM(A1)', '\tx', '\rx']) {
    assert.equal(csvCell(t), '"\'' + t.replace(/"/g, '""') + '"', JSON.stringify(t));
  }
  assert.equal(csvCell('=HYPERLINK("u")'), '"\'=HYPERLINK(""u"")"');
  assert.equal(csvCell(-12.5), '-12.5', 'a negative number is data');
  assert.equal(csvCell(0), '0');
  assert.equal(csvCell(Number.NaN), '""');
  assert.equal(csvCell(null), '""');
  assert.equal(csvCell('NVDA'), '"NVDA"');
  assert.equal(csvCell('a "b"'), '"a ""b"""');
  assert.equal(toCsv(['a', 'b'], [{ a: '=x', b: -1 }]), '"a","b"\r\n"\'=x",-1\r\n');
});

test('NYSE holidays derived from the rules match the table they replaced (2026-2030)', () => {
  const OLD = [
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24',
    '2028-01-17', '2028-02-21', '2028-04-14', '2028-05-29', '2028-06-19', '2028-07-04', '2028-09-04', '2028-11-23', '2028-12-25',
    '2029-01-01', '2029-01-15', '2029-02-19', '2029-03-30', '2029-05-28', '2029-06-19', '2029-07-04', '2029-09-03', '2029-11-22', '2029-12-25',
    '2030-01-01', '2030-01-21', '2030-02-18', '2030-04-19', '2030-05-27', '2030-06-19', '2030-07-04', '2030-09-02', '2030-11-28', '2030-12-25',
  ];
  const derived = [];
  for (let y = 2026; y <= 2030; y++) derived.push(...Object.keys(nyseHolidays(y)));
  assert.deepEqual(derived.sort(), OLD.slice().sort());
  // And it keeps working past the old table's end.
  assert.ok(nyseHolidays(2031)['2031-01-01']);
  assert.ok(nyseHolidays(2033)['2033-04-15'], 'Good Friday 2033');
});

test('early closes: July 3, the day after Thanksgiving, Christmas Eve', () => {
  assert.deepEqual(Object.keys(nyseEarlyCloses(2026)).sort(), ['2026-11-27', '2026-12-24']); // Jul 3 2026 is the observed holiday
  assert.deepEqual(Object.keys(nyseEarlyCloses(2025)).sort(), ['2025-07-03', '2025-11-28', '2025-12-24']);
  assert.deepEqual(Object.keys(nyseEarlyCloses(2027)).sort(), ['2027-11-26']); // Dec 24 2027 is the observed Christmas
});

test('marketState honours holidays and the 13:00 early close', () => {
  // 2026-11-27 (day after Thanksgiving) 14:00 ET = 19:00 UTC: after the early close.
  assert.equal(marketState(new Date('2026-11-27T19:00:00Z')).level, 'after');
  assert.equal(marketState(new Date('2026-11-27T15:00:00Z')).level, 'open');
  assert.match(marketState(new Date('2026-11-27T15:00:00Z')).label, /early close/);
  // 2026-11-26 Thanksgiving: closed all day.
  assert.equal(marketState(new Date('2026-11-26T16:00:00Z')).label, 'Holiday');
  // An ordinary Wednesday, 2026-11-18: 10:00 ET open, 17:00 ET after-hours, 21:00 ET closed.
  assert.equal(marketState(new Date('2026-11-18T15:00:00Z')).level, 'open');
  assert.equal(marketState(new Date('2026-11-18T22:00:00Z')).level, 'after');
  assert.equal(marketState(new Date('2026-11-19T02:00:00Z')).level, 'closed');
  assert.equal(marketState(new Date('2026-11-21T15:00:00Z')).level, 'closed', 'Saturday');
});

test('config: fallbacks are UTC, USD, SPY, CDN off; invalid values are rejected', () => {
  const none = readConfig({});
  assert.deepEqual([none.reportingTz, none.currency, none.benchmarkSymbol, none.benchmarkLabel, none.logoCdn], ['UTC', 'USD', 'SPY', 'SPY', false]);
  assert.equal(readConfig({ reportingTz: 'Europe/London' }).reportingTz, 'Europe/London');
  const c = readConfig({ reportingTz: 'Europe/London', config: { reporting_tz: 'Asia/Tokyo', currency: 'eur', benchmark_symbol: 'VT', logo_cdn: 'true' } });
  assert.deepEqual([c.reportingTz, c.currency, c.benchmarkSymbol, c.benchmarkLabel, c.logoCdn], ['Asia/Tokyo', 'EUR', 'VT', 'VT', false]);
  assert.equal(validTimeZone('Not/AZone'), null);
  assert.equal(validCurrency('<b>'), null);
  assert.equal(validCurrency('ZZZZ'), null);
});

test('format: currency and locale follow configuration', () => {
  F.configureFormat({ locale: 'en-US', currency: 'USD', reportingTz: 'UTC' });
  assert.equal(F.money(1234.5), '$1,234.50');
  assert.equal(F.compact(1234), '$1.2K');
  assert.equal(F.compact(2500000), '$2.50M');
  assert.equal(F.pct(-1.234), '−1.23%');
  assert.equal(F.pct(null), 'n/a');
  assert.equal(F.printedMoney(0.125), Number('0.13'));
  F.configureFormat({ locale: 'de-DE', currency: 'EUR' });
  assert.match(F.money(1234.5), /1\.234,50\s€/);
  assert.equal(F.printedMoney(1234.5), 1234.5, 'printed value parses back in any locale');
  F.configureFormat({ locale: 'en-US', currency: 'USD', reportingTz: 'UTC' });
  assert.equal(F.fmtDate(Date.UTC(2026, 10, 15, 23, 30), 'dayTime'), 'Nov 15, 23:30');
  F.configureFormat({ reportingTz: 'Asia/Tokyo' });
  assert.equal(F.fmtDate(Date.UTC(2026, 10, 15, 23, 30), 'dayTime'), 'Nov 16, 08:30', 'reporting zone, not the browser’s');
  F.configureFormat({ reportingTz: 'UTC' });
});

test('splitMovers filters by sign before slicing (X-7)', () => {
  const up = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => ({ sym: 'U' + n, dayPct: n, hist: [1, 2] }));
  const { gainers, losers } = splitMovers(up, 6);
  assert.equal(gainers.length, 6);
  assert.equal(losers.length, 0, 'an all-up day has no losers, however short the list');
  assert.deepEqual(gainers.map((s) => s.sym), ['U8', 'U7', 'U6', 'U5', 'U4', 'U3']);
  const mixed = [{ sym: 'A', dayPct: 2 }, { sym: 'B', dayPct: -1 }, { sym: 'C', dayPct: null }, { sym: 'D', dayPct: 0, hist: [1] }];
  const r = splitMovers(mixed, 6);
  assert.deepEqual(r.gainers.map((s) => s.sym), ['A']);
  assert.deepEqual(r.losers.map((s) => s.sym), ['B']);
});

test('topNavigate refuses anything but this page’s own ?view= query (no open redirect)', async () => {
  const { topNavigate } = await import('../src/core/host.js');
  // These return before touching window: the guard runs first.
  for (const bad of ['javascript:alert(1)', 'https://evil.example/', '//evil.example', '/path', '?view=x&next=https://e', '?view=<b>']) {
    assert.equal(topNavigate(bad), undefined, bad);
  }
});
test('QA B2: a frozen array with a frozen raw built from runtime text cannot become markup', () => {
  // The exact reproduction from qa-pr-122.md. The shape check alone accepts
  // it (it is indistinguishable at run time), so the literal-text policy has
  // to refuse the handler; the lint rules forbid the call shape outright.
  const text = '<img src="x" onerror="window.__qaPwned=1">';
  const raw = Object.freeze([text]);
  const forged = Object.freeze(Object.assign([text], { raw }));
  assert.throws(() => html(forged), /template literal text is not allowed/);
  for (const t of ['<script>alert(1)</script>', '<svg onload=alert(1)>', '<iframe src=x>', '<a href="javascript:alert(1)">x</a>',
    '<div ONMOUSEOVER = "x">', '<iframe srcdoc="x">', '<form><button formaction="x">', '<math><mi xlink:href="x">', '<!-- x -->']) {
    const r = Object.freeze([t]);
    assert.throws(() => html(Object.freeze(Object.assign([t], { raw: r }))), /not allowed/, t);
  }
});

test('html enforces slot contexts; nothing is promoted by nesting (QA B2 rounds 1 and 2)', () => {
  const evil = 'x onmouseover=alert(1)';
  assert.throws(() => html`<div data-x=${evil}>safe</div>`, /unquoted attribute value/);
  assert.throws(() => html`<div data-x= ${evil}>`, /unquoted attribute value/);
  assert.throws(() => html`<img src="x" onerror="${evil}">`, /not allowed/);
  assert.throws(() => html`<div data-${evil}="1">`, /attribute name/);
  assert.throws(() => html`<${'img'} src="x">`, /tag name/);
  // Tag (attribute-list) slots take attrs() only - not strings, not Markup.
  assert.throws(() => html`<div ${'onclick=alert(1)'}>`, /only an attrs\(\) list/);
  assert.throws(() => html`<div ${html`data-a="1"`}>`, /only an attrs\(\) list/);
  assert.equal(html`<div ${attrs({ 'data-a': '1"x', role: 'button' })}>`.s, '<div  data-a="1&quot;x" role="button">');
  assert.equal(html`<div ${''} class="a">`.s, '<div  class="a">');
  // QA round 2: an inner template built from runtime name/value, spliced into
  // a parent's attribute list, URL or attribute slot, is refused in each.
  const attributeList = (name, value) => html`${name}="${value}"`;
  assert.throws(() => html`<a ${attributeList('href', 'javascript:void(0)')}>x</a>`, /only an attrs\(\) list/);
  assert.throws(() => html`<a ${attributeList('onclick', 'void(0)')}>x</a>`, /only an attrs\(\) list/);
  assert.throws(() => html`<a href="${html`https://e.test/${'x'}`}">x</a>`, /needs a safeUrl/);
  assert.throws(() => html`<a title="${html`<b>x</b>`}">x</a>`, /markup may not be interpolated into the title/);
  assert.throws(() => html`<a title="${[html`<b>x</b>`]}">x</a>`, /markup may not be interpolated/, 'arrays are checked per element');
  assert.throws(() => html`<p>${attrs({ role: 'x' })}</p>`, /belongs in a tag/);
  // attrs() names are allow-listed: no handlers, no URL attributes.
  for (const name of ['onclick', 'OnLoad', 'href', 'src', 'srcdoc', 'style', 'formaction', 'xlink:href', 'data-x"y']) {
    assert.throws(() => attrs({ [name]: 'v' }), /not allowed/, name);
  }
  assert.throws(() => attrs({ title: html`<b>x</b>` }), /takes text/);
  // A template must end outside any tag, so it cannot leave its parent open.
  assert.throws(() => html`<a `, /must end outside any tag/);
  assert.throws(() => html`<a title="${'x'}`, /must end outside any tag/);
  // URL attributes take safeUrl() values only.
  assert.throws(() => html`<a href="${'https://example.com'}">x</a>`, /needs a safeUrl\(\) value/);
  assert.throws(() => html`<img src="${'data:image/png;base64,AAAA'}">`, /needs a safeUrl/);
  assert.equal(html`<a href="${safeUrl('https://e.test/?a=1&b="2"')}">x</a>`.s, '<a href="https://e.test/?a=1&amp;b=&quot;2&quot;">x</a>');
  assert.equal(html`<a href="${null}">x</a>`.s, '<a href="">x</a>');
  // Text and quoted values are escaped; Markup nests in text slots.
  assert.equal(html`<p title='${evil}"'>${evil}</p>`.s, '<p title=\'x onmouseover=alert(1)"\'>x onmouseover=alert(1)</p>');
  assert.equal(html`<p>${html`<b>${'<i>'}</b>`}</p>`.s, '<p><b>&lt;i&gt;</b></p>');
});

test('literal-text policy also decodes character references (QA round 2 java&#115;cript:)', () => {
  const forge = (t) => Object.freeze(Object.assign([t], { raw: Object.freeze([t]) }));
  for (const t of ['<a href="java&#115;cript:void(0)">x</a>', '<a href="java&#x73;cript:x">', '<a href="javascript&colon;x">',
    '<a href="jav&#9;ascript:x">', '<div on&#99;lick="x">', '<a href=" j a v a s c r i p t : x">']) {
    assert.throws(() => html(forge(t)), /not allowed/, t);
  }
});
