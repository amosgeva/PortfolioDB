// The dashboard as a browser sees it: the custom front end mounted by a real
// Streamlit server in its st.iframe, and the native pages around it (audit
// U-9). Every test runs at 360, 768 and 1440 px (playwright.config.mjs).
//
// What the jsdom suite cannot show, and this does:
//  * the private Streamlit selectors core/host.js depends on still match the
//    page Streamlit renders - checked by name, so an upgrade that renames one
//    fails here with the selector and the feature it breaks;
//  * a soft refresh is a rerun that remounts the frame, without a page load
//    and without the parent collecting listeners from every dead frame;
//  * the frame sizes itself to the viewport, and it - not the page - scrolls;
//  * the theme reaches the page behind the frame and the native pages;
//  * rail and tab-bar navigation crosses between the two renderers inside one
//    Streamlit session, a typed draft and the unlock survive the trip, and the
//    write gate is locked until the password is given.

import { test, expect } from '@playwright/test';
import { STREAMLIT } from '../src/core/host.js';
import {
  DASHBOARD_PASSWORD, NAV, NATIVE_VIEWS, FRAME_VIEWS, RAIL_BREAKPOINT, isNarrow,
  openSession, dashboardFrame, retire, expectNativePage, frameRailClick, nativeNavClick,
} from './support.mjs';

const parentListeners = (page) => page.evaluate(() => (window.__pdbHostListeners || []).map((l) => l.type).sort());

/** Go from the dashboard frame to a native page through the frame's rail. */
async function frameToNative(page, frame, id) {
  await frameRailClick(page, frame, `.nav a[data-nav="${id}"]`);
  await expectNativePage(page, id);
}

/** Go from a native page back to the dashboard frame through the native rail/tab bar. */
async function nativeToFrame(page) {
  await nativeNavClick(page, FRAME_VIEWS[0].id);
  return dashboardFrame(page);
}

test('the private Streamlit selectors core/host.js depends on are all present', async ({ page }) => {
  const { frame } = await openSession(page);

  expect(await frame.evaluate(() => { try { return Boolean(window.parent.document.body); } catch { return false; } }),
    'the dashboard frame cannot reach its parent page (did st.iframe lose allow-same-origin?): every core/host.js feature degrades').toBe(true);

  // Soft assertions: one run names every selector that has gone, not just the first.
  await expect.soft(page.locator(STREAMLIT.refreshButton),
    `Streamlit private selector STREAMLIT.refreshButton "${STREAMLIT.refreshButton}" matches nothing: ` +
    'the soft refresh would fall back to a full page load (core/host.js softRefresh)').toHaveCount(1);

  for (const v of NATIVE_VIEWS) {
    const sel = STREAMLIT.navButton(v.id);
    await expect.soft(page.locator(sel),
      `Streamlit private selector STREAMLIT.navButton('${v.id}') "${sel}" matches nothing: ` +
      `the rail's ${v.label} entry would reload the page and lose the session (core/host.js nativeNav)`).toHaveCount(1);
  }

  const inContainer = await frame.evaluate((sel) => Boolean(window.frameElement && window.frameElement.closest(sel)), STREAMLIT.elementContainer);
  expect.soft(inContainer,
    `Streamlit private selector STREAMLIT.elementContainer "${STREAMLIT.elementContainer}" is not an ancestor of the dashboard iframe: ` +
    "fitFrame() cannot release Streamlit's reserved height, so the frame cannot own the viewport").toBe(true);
});

test('the frame sizes itself to the viewport and scrolls inside, not the page', async ({ page }) => {
  const { frame } = await openSession(page);
  const vp = page.viewportSize();
  const iframe = await frame.frameElement();

  const box = await iframe.boundingBox();
  expect(Math.abs(box.height - vp.height), `frame height ${box.height} is not the viewport's ${vp.height}`).toBeLessThanOrEqual(1);
  expect(Math.abs(box.width - vp.width), `frame width ${box.width} is not the viewport's ${vp.width}`).toBeLessThanOrEqual(1);
  expect(box.y, 'the frame does not start at the top of the page').toBeLessThanOrEqual(1);

  // fitFrame(): the parent no longer scrolls, and nothing in it does either.
  const parent = await page.evaluate(() => ({
    html: document.documentElement.style.overflow, body: document.body.style.overflow,
    scrollers: Array.from(document.querySelectorAll('*')).filter((e) => e.scrollHeight > e.clientHeight + 1 &&
      /auto|scroll/.test(getComputedStyle(e).overflowY)).map((e) => e.tagName + (e.dataset.testid ? '[' + e.dataset.testid + ']' : '')),
  }));
  expect(parent.html, 'parent <html> still scrolls (fitFrame did not run)').toBe('hidden');
  expect(parent.body, 'parent <body> still scrolls').toBe('hidden');
  expect(parent.scrollers, 'an element of the Streamlit page scrolls behind the frame').toEqual([]);

  // The frame's own document is the scroller, with no sideways overflow.
  const inner = await frame.evaluate(() => {
    const s = document.scrollingElement;
    window.scrollTo(0, 300);
    return { tall: s.scrollHeight > s.clientHeight, y: window.scrollY, sw: s.scrollWidth, cw: s.clientWidth };
  });
  expect(inner.tall, 'the dashboard is not taller than one screen, so the scroll check proves nothing').toBe(true);
  expect(inner.y, 'the dashboard frame does not scroll').toBeGreaterThan(0);
  expect(inner.sw, `the dashboard overflows sideways at ${vp.width}px`).toBeLessThanOrEqual(inner.cw);

  // A resize re-fits the frame (the parent resize listener).
  const shorter = { width: vp.width, height: vp.height - 120 };
  await page.setViewportSize(shorter);
  await expect.poll(async () => Math.round((await iframe.boundingBox()).height), 'the frame did not follow a viewport resize').toBe(shorter.height);
});

test('a soft refresh remounts the frame in the same session without collecting listeners', async ({ page }) => {
  const session = await openSession(page);
  let frame = session.frame;
  expect(await parentListeners(page), 'listeners on the parent after the first mount').toEqual(['popstate', 'resize']);

  // Leave the default pane, so the refresh has a pane to restore.
  const target = NAV.tabbar.frame[1];
  const tab = isNarrow(page) ? `.tabbar [data-tab="${target}"]` : `.nav a[data-view="${target}"]`;
  await frame.locator(tab).click();
  await expect(frame.locator('.view.is-active')).toHaveAttribute('data-view-pane', target);

  for (let i = 1; i <= 3; i++) {
    await retire(frame);
    await frame.locator('[data-refresh]').click();
    frame = await dashboardFrame(page);   // a new document, not the retired one
    await expect(frame.locator('.toast'), `refresh ${i}: no "Updated just now" from the new mount`).toContainText('Updated just now');
    await expect(frame.locator('.view.is-active'), `refresh ${i}: the pane was not restored`).toHaveAttribute('data-view-pane', target);
    expect(await parentListeners(page), `refresh ${i}: the parent collected listeners from a dead frame`).toEqual(['popstate', 'resize']);
    await session.check(`refresh ${i}`);
  }
});

test('the theme toggle themes the frame, the page behind it and the native pages', async ({ page }) => {
  const { frame, check } = await openSession(page);
  const before = await frame.evaluate(() => document.documentElement.dataset.theme);
  const after = before === 'dark' ? 'light' : 'dark';
  const bg = { dark: 'rgb(22, 24, 29)', light: 'rgb(255, 255, 255)' };

  await frame.locator('[data-theme-toggle]').click();
  await expect(frame.locator('html')).toHaveAttribute('data-theme', after);
  await expect(frame.locator('[data-theme-toggle]')).toHaveAttribute('aria-pressed', after === 'dark' ? 'true' : 'false');
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).backgroundColor),
    'the page behind the frame was not themed (over-scroll would flash)').toBe(bg[after]);
  await expect(page.locator('meta[name="theme-color"]')).toHaveAttribute('content', after === 'dark' ? '#16181d' : '#ffffff');

  await frameToNative(page, frame, NATIVE_VIEWS[0].id);
  await expect(page.locator('html'), 'the native page did not pick up the saved theme').toHaveAttribute('data-theme', after);
  await check('theme, then native page');
});

test('rail and tab bar cross between the two renderers in one Streamlit session', async ({ page }) => {
  const session = await openSession(page);
  const narrow = isNarrow(page);

  // Each half shows the navigation its width calls for, switching at the same
  // breakpoint (both stylesheets use RAIL_BREAKPOINT).
  await expect(session.frame.locator('.tabbar'), `frame tab bar visibility at ${page.viewportSize().width}px (breakpoint ${RAIL_BREAKPOINT}px)`).toBeVisible({ visible: narrow });
  if (!narrow) await expect(session.frame.locator('.rail')).toBeVisible();

  // Every native page, reached from the frame's rail; back to the frame each time.
  for (const v of NATIVE_VIEWS) {
    await frameToNative(page, session.frame, v.id);
    await expect(page.locator('.st-key-m2_rail'), `native rail visibility at ${page.viewportSize().width}px`).toBeVisible({ visible: !narrow });
    await expect(page.locator('.st-key-m2_tabbar'), `native tab bar visibility at ${page.viewportSize().width}px`).toBeVisible({ visible: narrow });
    await session.check(`frame rail -> ${v.id}`);
    session.frame = await nativeToFrame(page);
    expect(await parentListeners(page), `${v.id} round trip: the parent collected listeners`).toEqual(['popstate', 'resize']);
    await session.check(`${v.id} -> frame`);
  }

  // Native to native, through the native rail or (narrow) the tab bar.
  const natives = narrow ? NATIVE_VIEWS.filter((v) => NAV.tabbar.native.includes(v.id)) : NATIVE_VIEWS;
  await frameToNative(page, session.frame, natives[0].id);
  for (const v of natives.slice(1)) {
    await nativeNavClick(page, v.id);
    await expectNativePage(page, v.id);
    await session.check(`native -> ${v.id}`);
  }
});

test('both rails list the same entries with the same glyphs', async ({ page }) => {
  const { frame } = await openSession(page);
  const frameRail = await frame.evaluate(() => Array.from(document.querySelectorAll('[data-rail-nav] .nav a'), (a) => ({
    id: a.dataset.view || a.dataset.nav,
    label: Array.from(a.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim(),
    glyph: a.querySelector('svg').innerHTML.replace(/\s+/g, ' '),
  })));
  expect(frameRail.map((e) => e.id), 'frame rail order').toEqual(NAV.views.map((v) => v.id));

  await frameToNative(page, frame, NATIVE_VIEWS[0].id);
  const nativeRail = await page.evaluate(() => Array.from(document.querySelectorAll('.st-key-m2_rail .stButton'), (b) => {
    const key = Array.from(b.closest('[class*="st-key-m2_nav_"]').classList).find((c) => c.startsWith('st-key-m2_nav_'));
    const mask = getComputedStyle(b.querySelector('button'), '::before').getPropertyValue('mask-image') ||
      getComputedStyle(b.querySelector('button'), '::before').getPropertyValue('-webkit-mask-image');
    const svg = decodeURIComponent((mask.match(/data:image\/svg\+xml,([^"')]+)/) || [])[1] || '');
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml');
    return { id: key.replace('st-key-m2_nav_', ''), label: b.innerText.trim(), glyph: doc.documentElement.innerHTML.replace(/\s+/g, ' ') };
  }));
  const norm = (g) => g.replace(/ xmlns="[^"]*"/g, '').replace(/<(\w+)([^>]*?)\s*\/>/g, '<$1$2></$1>');
  expect(nativeRail.map((e) => [e.id, e.label]), 'native rail entries differ from the frame rail').toEqual(frameRail.map((e) => [e.id, e.label]));
  nativeRail.forEach((n, i) => expect(norm(n.glyph), `${n.id}: the native rail glyph differs from the frame rail glyph`).toBe(norm(frameRail[i].glyph)));
});

test('a typed Manage draft survives a round trip through the dashboard frame', async ({ page }) => {
  const session = await openSession(page);
  const manage = NATIVE_VIEWS.find((v) => v.id === 'manage');
  const symbol = page.locator('.st-key-m2_lot_sym input');
  const notes = page.locator('.st-key-m2_lot_notes input');

  await frameToNative(page, session.frame, manage.id);
  await symbol.fill('ZZE2E');
  await notes.fill('typed before leaving');

  session.frame = await nativeToFrame(page);        // to the iframe and back
  await frameToNative(page, session.frame, manage.id);
  await expect(symbol, 'the Symbol draft was lost on the frame round trip').toHaveValue('ZZE2E');
  await expect(notes, 'the Notes draft was lost on the frame round trip').toHaveValue('typed before leaving');

  const other = NATIVE_VIEWS.find((v) => v.id !== manage.id && NAV.tabbar.native.includes(v.id));
  await nativeNavClick(page, other.id);             // to another native page and back
  await expectNativePage(page, other.id);
  await nativeNavClick(page, manage.id);
  await expectNativePage(page, manage.id);
  await expect(symbol, `the Symbol draft was lost on the ${other.id} round trip`).toHaveValue('ZZE2E');
  await session.check('draft round trips');
});

test('the write gate: locked until the password, unlocked across navigation, then locked again', async ({ page }) => {
  const session = await openSession(page);
  const gate = page.locator('.m2-gate .m2-tag');
  const addLot = page.locator('.st-key-m2_lot_submit button');

  await frameToNative(page, session.frame, 'manage');
  await expect(gate, 'Manage does not open locked').toHaveText('locked');
  await expect(addLot, 'a write control is enabled while locked').toBeDisabled();

  await page.locator('.st-key-m2_unlock_pw input').fill(DASHBOARD_PASSWORD + '-wrong');
  await page.locator('.st-key-m2_unlock_submit button').click();
  await expect(page.locator('[data-testid="stAlertContentError"]'), 'a wrong password was not refused').toBeVisible();
  await expect(gate).toHaveText('locked');
  await expect(addLot).toBeDisabled();

  await page.locator('.st-key-m2_unlock_pw input').fill(DASHBOARD_PASSWORD);
  await page.locator('.st-key-m2_unlock_submit button').click();
  await expect(gate, 'the right password did not unlock').toHaveText('unlocked');
  await expect(addLot).toBeEnabled();

  // The unlock lives in the Streamlit session, so it survives the frame.
  session.frame = await nativeToFrame(page);
  await frameToNative(page, session.frame, 'manage');
  await expect(gate, 'the unlock did not survive a round trip through the frame').toHaveText('unlocked');
  await expect(addLot).toBeEnabled();
  await session.check('unlock round trip');

  await page.locator('.st-key-m2_lock button').click();
  await expect(gate, 'Lock did not lock').toHaveText('locked');
  await expect(addLot).toBeDisabled();
});
