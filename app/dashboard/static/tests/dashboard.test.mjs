// The real front end, executed against a DOM (audit U-9): mount, every view,
// refresh (soft and fallback), theme toggle, CSV export, remounting, and the
// payload-driven labels (benchmark, currency, zone, cash entry date, alerts).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { mount, makeParent, click, key, pointer, buildHtmlPlaceholders, STATIC } from './harness.mjs';
import { demo, withConfig, w2b, SAMPLE_ALERTS } from './fixtures.mjs';

const $ = (page, s) => page.document.querySelector(s);
const $$ = (page, s) => Array.from(page.document.querySelectorAll(s));
const activeView = (page) => $(page, '.view.is-active').dataset.viewPane;

function withPage(payload, opts, fn) {
  const page = mount(payload, opts);
  try { fn(page); assert.deepEqual(page.errors, [], 'page errors'); } finally { page.close(); }
}

test('harness fills every placeholder build_html fills, and the shell uses no other', () => {
  const filled = new Set(buildHtmlPlaceholders());
  assert.ok(filled.has('__APP_JS__') && filled.has('__DATA_JSON__') && filled.has('__CSS__'));
  const shell = readFileSync(join(STATIC, 'shell.html'), 'utf8');
  for (const m of shell.matchAll(/__[A-Z_]+__/g)) assert.ok(filled.has(m[0]), m[0] + ' is in shell.html but build_html does not replace it');
});

test('mounts the real demo payload and renders the portfolio', () => withPage(demo(), {}, (page) => {
  assert.equal(activeView(page), 'portfolio');
  assert.match($(page, '#kpi-total').textContent, /^\$\d/);
  assert.equal($$(page, '#holdings tr[data-sym]').length, demo().holdings.length);
  assert.equal($$(page, '#holdings-cards .hcard').length, demo().holdings.length);
  assert.ok($(page, '#pv-chart svg'), 'value chart drawn');
  assert.ok($(page, '#alloc svg'), 'allocation drawn');
  assert.ok($(page, '#waterfall svg'), 'waterfall drawn');
  assert.ok($$(page, '#risk-tbl tr').length > 0, 'risk table drawn');
  assert.ok($(page, '#ph-chart svg'), 'price chart drawn');
  assert.ok($$(page, '#latest-prices tr').length > 0);
  assert.ok($$(page, '.tape__item').length > 0, 'ticker tape');
}));

test('every view opens from the rail and renders its content', () => withPage(demo(), {}, (page) => {
  const expect = {
    movers: '#gainers .mv, #losers .mv, #gainers .empty',
    stats: '#st-recs .rec',
    alerts: '#feed .fitem, #feed .empty',
    fundamentals: '#fd-body section.card, #fd-body .empty',
    history: '#history-body table',
    portfolio: '#holdings tr',
  };
  for (const [view, sel] of Object.entries(expect)) {
    click(page.window, $(page, '.nav a[data-view="' + view + '"]'));
    assert.equal(activeView(page), view);
    assert.ok($(page, sel), view + ' rendered');
    assert.equal($(page, '.nav a[data-view="' + view + '"]').getAttribute('aria-current'), 'page');
    // The parent URL follows the pane.
    assert.equal(page.parent.window.location.search, '?view=' + view);
  }
  // The phone tab bar drives the same switch.
  click(page.window, $(page, '.tabbar [data-tab="movers"]'));
  assert.equal(activeView(page), 'movers');
}));

test('soft refresh clicks the hidden Streamlit button and the next mount restores the pane', () => {
  const parent = makeParent();
  let page = mount(demo(), { parent });
  click(page.window, $(page, '.nav a[data-view="stats"]'));
  click(page.window, $(page, '[data-refresh]'));
  assert.equal(parent.clicks.refresh, 1);
  assert.equal(parent.window.sessionStorage.getItem('pdb_refresh_view'), 'stats');
  page.close();
  page = mount(demo(), { parent });
  try {
    assert.equal(activeView(page), 'stats', 'pane restored after the rerun');
    assert.equal(parent.window.sessionStorage.getItem('pdb_refresh_view'), null, 'consumed once');
    assert.equal($(page, '.toast span').textContent, 'Updated just now');
    assert.deepEqual(page.errors, []);
  } finally { page.close(); }
});

test('refresh falls back to a hard navigation when Streamlit’s button is absent', () => {
  const parent = makeParent({ refreshButton: false });
  withPage(demo(), { parent }, (page) => {
    click(page.window, $(page, '.nav a[data-view="movers"]'));
    click(page.window, $(page, '[data-refresh]'));
    assert.equal(parent.clicks.anchors.length, 1);
    assert.match(parent.clicks.anchors[0], /^\?view=movers&r=\d+$/);
  });
});

test('the page still mounts and works when the parent is unreachable (sandboxed)', () => {
  withPage(demo(), { sandboxed: true }, (p) => {
    assert.match($(p, '#kpi-total').textContent, /^\$/);
    click(p.window, $(p, '.nav a[data-view="movers"]'));
    assert.equal(activeView(p), 'movers');
    click(p.window, $(p, '[data-theme-toggle]'));
    click(p.window, $(p, '[data-refresh]'));
  });
});

test('remounting does not accumulate listeners on the parent window (U-5)', () => {
  const parent = makeParent();
  for (let i = 0; i < 6; i++) { const p = mount(demo(), { parent }); assert.deepEqual(p.errors, []); p.close(); }
  assert.equal(parent.listenerCount('resize'), 1, 'one resize listener after six mounts');
  assert.equal(parent.listenerCount('popstate'), 1, 'one popstate listener after six mounts');
  // And back/forward still drives the live frame.
  const page = mount(demo(), { parent });
  try {
    parent.window.dispatchEvent(new parent.window.PopStateEvent('popstate', { state: { pdbView: 'history' } }));
    assert.equal(activeView(page), 'history');
    assert.equal(parent.listenerCount('popstate'), 1);
  } finally { page.close(); }
});

test('theme toggle flips the document, remembers it and themes the parent', () => withPage(demo(), {}, (page) => {
  const root = page.document.documentElement;
  const before = root.getAttribute('data-theme');
  click(page.window, $(page, '[data-theme-toggle]'));
  const after = root.getAttribute('data-theme');
  assert.notEqual(after, before);
  assert.equal(page.window.localStorage.getItem('pdb_theme'), after);
  assert.equal(page.parent.window.document.querySelector('meta[name="theme-color"]').content, after === 'dark' ? '#16181d' : '#ffffff');
  assert.equal($(page, '[data-theme-toggle]').getAttribute('aria-pressed'), after === 'dark' ? 'true' : 'false');
  // Colours computed in JS follow the theme.
  assert.ok($(page, '#heat .heat__tile'), 'heatmap re-rendered');
}));

test('positions and lots export as CSV with formula-leading text neutralised (A-4)', () => {
  const p = demo();
  p.histLots[0].notes = '=HYPERLINK("http://evil.example","x")';
  p.histLots[1].notes = '@SUM(A1)';
  p.histLots[2].notes = '-2+3';
  p.histLots[3].notes = '\tcmd';
  withPage(p, {}, (page) => {
    click(page.window, $(page, '#pos-csv'));
    const pos = page.blobText(0).split('\r\n');
    assert.equal(pos[0], '"symbol","name","sector","qty","avgCost","price","mktVal","cost","gl","glPct"');
    assert.equal(pos.length - 2, p.holdings.length);
    assert.match($(page, '.toast span').textContent, /^Exported \d+ positions$/);
    click(page.window, $(page, '.nav a[data-view="history"]'));
    click(page.window, $(page, '#lots-csv'));
    const lots = page.blobText(1);
    assert.ok(lots.includes('"\'=HYPERLINK(""http://evil.example"",""x"")"'), 'leading = neutralised');
    assert.ok(lots.includes('"\'@SUM(A1)"'), 'leading @ neutralised');
    assert.ok(lots.includes('"\'-2+3"'), 'leading - in text neutralised');
    assert.ok(lots.includes('"\'\tcmd"'), 'leading tab neutralised');
    assert.equal(page.blobs[1].type, 'text/csv;charset=utf-8');
  });
});

test('the configured benchmark labels every comparison; SPY is only the fallback (U-3)', () => {
  const p = withConfig(demo(), { benchmark_symbol: 'VOO', benchmark_label: 'S&P 500 (VOO)', reporting_tz: 'America/New_York', currency: 'USD', logo_cdn: false });
  p.returns.benchmark = 'VOO (price return, excl. its dividends)';
  withPage(p, {}, (page) => {
    assert.ok($(page, '#returns-strip').textContent.includes('S&P 500 (VOO)'));
    assert.ok($(page, '#risk-sub').textContent.includes('S&P 500 (VOO) benchmark'));
    assert.ok($(page, '#risk-stats').textContent.includes('Beta vs S&P 500 (VOO)'));
    assert.equal($(page, '[data-bench-label]').textContent, 'S&P 500 (VOO)');
    assert.equal($(page, '#ph-spy').disabled, false, 'VOO history exists, so the overlay is offered');
    const shown = page.document.querySelector('.content').textContent;
    // SPY is a holding in the demo, so check labels rather than the whole page.
    assert.ok(!$(page, '#returns-strip').textContent.includes('SPY'));
    assert.ok(!$(page, '#risk-sub').textContent.includes('SPY'));
    assert.ok(shown.length > 0);
  });
  // No config block: the fallback is SPY.
  withPage(demo(), {}, (page) => {
    assert.ok($(page, '#returns-strip').textContent.includes('SPY'));
    assert.equal($(page, '[data-bench-label]').textContent, 'SPY');
  });
});

test('reporting zone and currency come from config, never a hard-coded city (U-6)', () => {
  const bundle = readFileSync(join(STATIC, 'app.js'), 'utf8');
  assert.ok(!bundle.includes('Asia/Jerusalem'), 'no Jerusalem fallback in the bundle');
  withPage(withConfig(demo(), { reporting_tz: 'Asia/Tokyo', currency: 'EUR' }), {}, (page) => {
    assert.match($(page, '[data-clock]').textContent, /GMT\+9|JST/);
    assert.match($(page, '#kpi-mktval').textContent, /€/);
  });
  const noTz = demo(); delete noTz.reportingTz;
  withPage(noTz, {}, (page) => assert.match($(page, '[data-clock]').textContent, /UTC/));
});

test('logos: self-hosted data URIs render; the CDN only when config.logo_cdn is true (A-5)', () => {
  const p = demo();
  p.logos = { AAPL: 'data:image/png;base64,iVBORw0KGgo=' };
  withPage(p, {}, (page) => {
    const srcs = $$(page, 'img.sym-logo').map((i) => i.getAttribute('src'));
    assert.ok(srcs.length > 0 && srcs.every((s) => s.startsWith('data:image/png')), 'only the cached logo');
  });
  withPage(withConfig(p, { logo_cdn: true }), {}, (page) => {
    const srcs = $$(page, 'img.sym-logo').map((i) => i.getAttribute('src'));
    assert.ok(srcs.some((s) => s.startsWith('https://assets.parqet.com/logos/symbol/')), 'CDN used when opted in');
    assert.ok(srcs.some((s) => s.startsWith('data:image/png')), 'cached logo still preferred');
  });
  // A failed logo removes itself (no inline onerror attribute anywhere).
  withPage(withConfig(p, { logo_cdn: true }), {}, (page) => {
    const img = $(page, 'img.sym-logo[src^="https://"]');
    img.dispatchEvent(new page.window.Event('error'));
    assert.equal(img.isConnected, false);
    assert.equal($$(page, '[onerror]').length, 0);
  });
});

test('the app shell makes no third-party request: fonts are inline, no Google Fonts', () => {
  const css = readFileSync(join(STATIC, 'app.css'), 'utf8');
  assert.ok(!/fonts\.googleapis|fonts\.gstatic|@import/.test(css));
  assert.match(css, /@font-face\{font-family:'Space Grotesk'[^}]*src:url\(data:font\/woff2;base64,/);
  assert.match(css, /@font-face\{font-family:'JetBrains Mono'[^}]*src:url\(data:font\/woff2;base64,/);
  const urls = Array.from(css.matchAll(/url\(([^)]*)\)/g), (m) => m[1]).filter((u) => !u.startsWith('data:') && !u.startsWith('#'));
  assert.deepEqual(urls, [], 'no external url() in app.css');
});

test('cash is labelled as entered, with its entry date (U-4)', () => {
  withPage(demo(), {}, (page) => {
    const tile = $(page, '[data-kpi="cash"]');
    assert.equal(tile.querySelector('.kpi__label').textContent, 'Cash (as entered)');
    assert.ok(!tile.textContent.includes('Buying power') && !tile.textContent.includes('Settled'));
    assert.equal($(page, '#kpi-cash-sub').textContent, 'oldest entry Nov 15, 2026');
    click(page.window, tile);
    assert.ok($(page, '[data-drawer-hd]').textContent.includes('Cash (as entered)'));
    assert.ok($(page, '[data-drawer-bd]').textContent.includes('not settled cash or buying power'));
  });
  // The W2b key name (as_of) is read too, and one account says "entered".
  const p = demo();
  p.kpi.cashAccounts = [{ account: 'Broker A', cash: 10, as_of: '2026-10-02T12:00:00+00:00' }];
  withPage(p, {}, (page) => assert.equal($(page, '#kpi-cash-sub').textContent, 'entered Oct 2, 2026'));
});

test('stored alerts are listed, counted and routed to Manage; no in-memory form (U-1)', () => {
  const parent = makeParent();
  withPage({ ...demo(), alerts: SAMPLE_ALERTS }, { parent }, (page) => {
    assert.equal($(page, '#alert-form'), null, 'the ephemeral form is gone');
    click(page.window, $(page, '.nav a[data-view="alerts"]'));
    assert.equal($$(page, '#alert-list .alist__row').length, 2);
    assert.equal($(page, '#triggered-count').textContent, '1');
    assert.equal($(page, '#armed-count').textContent, '1');
    assert.equal($(page, '#unread-count').textContent, '1');
    assert.ok($(page, '#feed').textContent.includes('AAPL is above $150.00'));
    assert.ok($(page, '#feed').textContent.includes('MSFT falls below $100.00'));
    // Mark read persists per viewer, keyed on the stored alert id.
    click(page.window, $(page, '#mark-all'));
    assert.equal($(page, '#unread-count').textContent, '0');
    assert.deepEqual(JSON.parse(page.window.localStorage.getItem('pdb_alerts_read')), ['11']);
    click(page.window, $(page, '[data-manage-alerts]'));
    assert.equal(parent.clicks.anchors.at(-1), '?view=manage');
  });
  // A server without stored alerts: an empty list and the same route.
  withPage(demo(), {}, (page) => {
    assert.equal($(page, '#alert-list').textContent.trim(), 'No price alerts set.');
    assert.equal($(page, '#armed-count').textContent, '0');
  });
});

test('toasts are announced: role=status and aria-live (U-7)', () => withPage(demo(), {}, (page) => {
  const t = $(page, '.toast');
  assert.equal(t.getAttribute('role'), 'status');
  assert.equal(t.getAttribute('aria-live'), 'polite');
}));

test('no symbol input is capped below the 32-character symbol limit (U-8)', () => {
  const shell = readFileSync(join(STATIC, 'shell.html'), 'utf8');
  for (const m of shell.matchAll(/maxlength="(\d+)"/g)) assert.ok(Number(m[1]) >= 32, 'maxlength ' + m[1]);
  assert.ok(!/id="a-sym"/.test(shell), 'the 6-character alert symbol field is gone');
});

test('value chart: keyboard steps the cursor and announces each point (U-2)', () => withPage(demo(), {}, (page) => {
  const plot = $(page, '#pv-chart .pv-plot');
  assert.equal(plot.tabIndex, 0, 'focusable');
  assert.match(plot.getAttribute('aria-label'), /arrow keys/);
  const live = $(page, '#pv-chart .pv-live');
  plot.focus();
  key(page.window, plot, 'ArrowLeft');
  const last = live.textContent;
  assert.match(last, /: \$[\d,]+\.\d\d$/, 'announces date and value');
  assert.equal($(page, '#pv-chart .pv-cursor').getAttribute('opacity'), '1');
  key(page.window, plot, 'ArrowLeft');
  assert.notEqual(live.textContent, last, 'moved one point');
  key(page.window, plot, 'Home');
  const first = live.textContent;
  key(page.window, plot, 'End');
  assert.equal(live.textContent, last, 'End is the latest point');
  key(page.window, plot, 'Home');
  assert.equal(live.textContent, first);
  key(page.window, plot, 'Escape');
  assert.equal($(page, '#pv-chart .pv-cursor').getAttribute('opacity'), '0');
}));

test('price chart: touch scrubs and the reading outlasts the finger (U-2)', () => withPage(demo(), {}, (page) => {
  const plot = $(page, '#ph-chart .ph-plot');
  const svg = plot.querySelector('svg');
  svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 920, height: 280, right: 920, bottom: 280 });
  const tip = $(page, '#ph-chart .ph-tip');
  pointer(page.window, plot, 'pointerdown', { clientX: 460, pointerType: 'touch' });
  assert.equal(tip.style.opacity, '1');
  assert.ok($(page, '#ph-chart .ph-live').textContent.length > 0, 'touch reading announced');
  pointer(page.window, plot, 'pointerleave', { pointerType: 'touch' });
  assert.equal(tip.style.opacity, '1', 'still shown after lifting');
  pointer(page.window, page.document.body, 'pointerdown', { clientX: 5, pointerType: 'touch' });
  assert.equal(tip.style.opacity, '0', 'a tap elsewhere hides it');
  // Mouse: hover shows, leaving hides.
  pointer(page.window, plot, 'pointermove', { clientX: 100, pointerType: 'mouse' });
  assert.equal(tip.style.opacity, '1');
  pointer(page.window, plot, 'pointerleave', { pointerType: 'mouse' });
  assert.equal(tip.style.opacity, '0');
}));

test('gainers and losers stay sign-filtered on the page (X-7 regression guard)', () => {
  const p = demo();
  Object.values(p.stocks).forEach((s, i) => { s.dayPct = 0.5 + i; s.hist = [1, 2]; });
  withPage(p, {}, (page) => {
    assert.equal($$(page, '#losers .mv').length, 0, 'no losers on an all-up day');
    assert.equal($(page, '#losers .empty').textContent, 'No symbol declined today.');
    const g = $$(page, '#gainers .mv__chg').map((e) => e.textContent);
    assert.ok(g.length > 0 && g.every((t) => t.startsWith('+')));
  });
});

test('drawer and palette: open, trap focus, close with Escape', () => withPage(demo(), {}, (page) => {
  const { window: w } = page;
  click(w, $(page, '#holdings .sym-open'));
  const drawer = $(page, '[data-drawer]');
  assert.ok(drawer.classList.contains('open'));
  assert.match(drawer.getAttribute('aria-label'), / details$/);
  key(w, page.document, 'Escape');
  assert.ok(!drawer.classList.contains('open'));
  key(w, page.document, 'k', { ctrlKey: true });
  const cmdk = $(page, '[data-cmdk]');
  assert.ok(cmdk.classList.contains('open'));
  const input = $(page, '[data-cmdk-input]');
  input.value = 'hist'; input.dispatchEvent(new w.Event('input'));
  key(w, input, 'Enter');
  assert.equal(activeView(page), 'history');
  assert.ok(!cmdk.classList.contains('open'));
}));

test('a payload missing whole sections still mounts', () => withPage({}, {}, (page) => {
  assert.equal($(page, '#kpi-total').textContent, '$0.00');
  for (const view of ['movers', 'stats', 'alerts', 'fundamentals', 'history']) click(page.window, $(page, '.nav a[data-view="' + view + '"]'));
}));

test('W2b contract: stored alerts show the split-restated level and "no quote" for triggered:null', () => withPage(w2b(), {}, (page) => {
  click(page.window, $(page, '.nav a[data-view="alerts"]'));
  const feed = $(page, '#feed').textContent;
  assert.ok(feed.includes('NVDA is above $100.00'), 'effective_level is the one shown');
  assert.ok(feed.includes('entered as $400.00, restated for a split'));
  assert.ok(feed.includes('NO QUOTE'), 'triggered null is not "watching"');
  assert.equal($(page, '#triggered-count').textContent, '1');
  assert.ok($(page, '#alert-list').textContent.includes('NO QUOTE'));
}));

test('W2b contract: currency warnings are shown, and cash outside USD is listed but not summed', () => withPage(w2b(), {}, (page) => {
  const warn = $(page, '#currency-warn');
  assert.equal(warn.hidden, false);
  assert.match(warn.textContent, /2 amounts are not in USD/);
  assert.ok(warn.textContent.includes('SAP is registered in EUR'));
  assert.equal($$(page, '#currency-warn li').length, 2);
  assert.match($(page, '#kpi-cash-sub').textContent, /1 not in USD, left out$/);
  click(page.window, $(page, '[data-kpi="cash"]'));
  const bd = $(page, '[data-drawer-bd]').textContent;
  assert.ok(bd.includes('900.00 EUR'), 'foreign balance shown in its own currency');
  assert.ok(bd.includes('Only USD balances are added up'));
}));

test('no currency warnings: the notice stays hidden', () => withPage(demo(), {}, (page) => {
  assert.equal($(page, '#currency-warn').hidden, true);
}));

test('W2b contract: unrealized uses priced cost basis; total return is over invested capital; null ratios read n/a', () => withPage(w2b(), {}, (page) => {
  const p = w2b();
  assert.equal($(page, '#kpi-return').textContent, 'n/a');
  assert.match($(page, '#kpi-return-sub').textContent, /priced positions only/);
  assert.match($(page, '#kpi-gl-sub').textContent, /1 unpriced left out/);
  assert.match($(page, '#kpi-div-sub').textContent, /YoC n\/a/);
  click(page.window, $(page, '[data-kpi="unrealized"]'));
  let bd = $(page, '[data-drawer-bd]').textContent;
  assert.ok(bd.includes('Cost basis of priced positions'));
  assert.ok(bd.includes('$' + p.kpi.pricedCostBasis.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })));
  assert.ok(bd.includes('ZZZZ') && bd.includes('an unknown price is not a loss'));
  key(page.window, page.document, 'Escape');
  click(page.window, $(page, '[data-kpi="totalReturn"]'));
  bd = $(page, '[data-drawer-bd]').textContent;
  assert.ok(bd.includes('Invested capital (peak)') && bd.includes('$60,000.00'), 'the divisor shown is invested capital');
  assert.ok(!bd.includes('Cost basis'), 'no longer "÷ cost basis"');
  assert.ok(bd.includes('peak net invested capital'));
  key(page.window, page.document, 'Escape');
  click(page.window, $(page, '[data-kpi="costBasis"]'));
  assert.ok($(page, '[data-drawer-bd]').textContent.includes('of invested capital'));
}));

test('W2b contract: the 1Y value chart says when it is all history', () => withPage(w2b(), {}, (page) => {
  const chip = $(page, '#pv-chips [data-range="1Y"]');
  click(page.window, chip);
  assert.match($(page, '#pv-chart .pv-plot').getAttribute('aria-label'), /All history \(less than 1Y collected\)/);
  assert.ok($(page, '#pv-chart').textContent.includes('All history (less than 1Y collected)'));
  assert.match(chip.title, /all history/);
}));

test('W2b contract: without a config block the benchmark the server used labels the page', () => {
  const p = demo();
  p.returns = { ...p.returns, benchmarkSymbol: 'VOO', benchmarkAvailable: false };
  withPage(p, {}, (page) => {
    assert.ok($(page, '#returns-strip').textContent.includes('VOO'));
    assert.match($(page, '#returns-sub').textContent, /no VOO price history collected yet/);
  });
});

test('native pages open through Streamlit’s hidden nav buttons when present (W5), else a page load', () => {
  const parent = makeParent({ navButtons: ['manage', 'advisor', 'health'] });
  withPage(demo(), { parent }, (page) => {
    click(page.window, $(page, '.nav a[data-nav="health"]'));
    click(page.window, $(page, '.nav a[data-nav="manage"]'));
    assert.deepEqual(parent.clicks.nav, ['health', 'manage']);
    assert.equal(parent.clicks.anchors.length, 0, 'no page load');
  });
  const bare = makeParent();
  withPage(demo(), { parent: bare }, (page) => {
    click(page.window, $(page, '.nav a[data-nav="advisor"]'));
    assert.deepEqual(bare.clicks.anchors, ['?view=advisor']);
  });
});
test('dayChangePct null (no previous close) renders n/a, neutral, in the title, the total line and the story', () => {
  const p = demo();
  p.kpi.dayChangePct = null;
  p.kpi.dayChange = 0;
  withPage(p, {}, (page) => {
    const title = page.parent.window.document.title;
    assert.equal(title, '● n/a · PortfolioDB', 'tab title says n/a, not +0.00%');
    const sub = $(page, '#kpi-total-sub');
    assert.match(sub.textContent, /^n\/a/);
    assert.equal(sub.querySelector('svg'), null, 'no up/down arrow');
    assert.equal(sub.querySelectorAll('.up, .down').length, 0, 'no gain/loss colour');
    assert.ok(!sub.textContent.includes('+$0.00'));
    const story = $(page, '#kpi-story').textContent;
    assert.ok(!story.includes('Flat today'), 'unknown is not flat');
    assert.match(story, /^No day change yet/);
  });
  // A known day move still gets its sign, arrow and colour.
  withPage(demo(), {}, (page) => {
    assert.match(page.parent.window.document.title, /^[▲▼●] [+-]\d+\.\d\d% · PortfolioDB$/);
    assert.ok($(page, '#kpi-total-sub .delta.up, #kpi-total-sub .delta.down'));
  });
});