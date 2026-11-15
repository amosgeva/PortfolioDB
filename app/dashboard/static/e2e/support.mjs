// Shared by playwright.config.mjs and the specs: the server's throwaway
// password, the contract files, and helpers that find the dashboard frame and
// prove a step happened inside one Streamlit session (no page load).

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect } from '@playwright/test';

export const STATIC = join(dirname(fileURLToPath(import.meta.url)), '..');
export const DASHBOARD_PASSWORD = process.env.PDB_E2E_DASHBOARD_PASSWORD || 'e2e-not-a-secret';
export const NAV = JSON.parse(readFileSync(join(STATIC, 'nav.json'), 'utf8'));
const NATIVE_GROUPS = new Set(NAV.groups.filter((g) => g.renderer === 'native').map((g) => g.id));
export const NATIVE_VIEWS = NAV.views.filter((v) => NATIVE_GROUPS.has(v.group));
export const FRAME_VIEWS = NAV.views.filter((v) => !NATIVE_GROUPS.has(v.group));
/** Below this width both renderers swap the rail for the phone tab bar. */
export const RAIL_BREAKPOINT = 980;

export const isNarrow = (page) => page.viewportSize().width <= RAIL_BREAKPOINT;

/**
 * The live dashboard frame: the st.iframe whose document has the front end
 * mounted (not the first-paint skeleton, not a native page's theme shim).
 * Resolves once its rail has been drawn.
 */
export async function dashboardFrame(page) {
  let found = null;
  await expect.poll(async () => {
    for (const f of page.frames()) {
      if (f === page.mainFrame()) continue;
      try {
        if (await f.evaluate(() => document.querySelectorAll('[data-rail-nav] .nav a').length > 0 && !window.__e2eRetired)) {
          found = f;
          return true;
        }
      } catch { /* a frame being torn down */ }
    }
    return false;
  }, { message: 'the dashboard iframe never mounted (no frame with a drawn rail)', timeout: 60_000 }).toBe(true);
  return found;
}

/**
 * Open the dashboard and plant markers that only survive inside one page
 * load: a property on the top window, and a count of websocket connections
 * (a page load opens a new one). `session.check()` asserts both.
 */
export async function openSession(page, path = '/') {
  const sockets = [];
  const errors = [];
  page.on('websocket', (ws) => { if (ws.url().includes('/_stcore/stream')) sockets.push(ws.url()); });
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(path);
  const frame = await dashboardFrame(page);
  await page.evaluate(() => { window.__e2eSession = 'same page load'; });
  return {
    frame,
    sockets,
    async check(step) {
      expect(await page.evaluate(() => window.__e2eSession), `${step}: the page reloaded (top-window marker lost)`).toBe('same page load');
      expect(sockets.length, `${step}: a new Streamlit websocket was opened, so the session was not kept`).toBe(1);
      expect(errors, `${step}: uncaught errors in the page or the dashboard frame`).toEqual([]);
    },
  };
}

/** Mark a frame so a later lookup can tell a remount from the same document. */
export async function retire(frame) {
  await frame.evaluate(() => { window.__e2eRetired = true; });
}

/** Wait for the native page whose heading is nav.json's label for `id`. */
export async function expectNativePage(page, id) {
  const v = NAV.views.find((x) => x.id === id);
  await expect(page.locator('.m2-topbar h1'), `native page ${id} did not render`).toHaveText(v.label);
  await expect.poll(() => new URL(page.url()).searchParams.get('view'), `the address bar does not say ?view=${id}`).toBe(id);
  await expect(page.locator('[data-testid="stException"]'), `native page ${id} raised an exception`).toHaveCount(0);
}

/** Click a rail entry inside the dashboard frame (opening the drawer first on a narrow screen). */
export async function frameRailClick(page, frame, selector) {
  if (isNarrow(page)) {
    await frame.locator('.menu-btn').click();
    await expect(frame.locator('.rail'), 'the rail drawer did not open').toHaveClass(/\bopen\b/);
  }
  await frame.locator(selector).click();
}

/** Click the native page's rail entry, or its tab bar entry on a narrow screen. */
export async function nativeNavClick(page, id) {
  const inTabbar = NAV.tabbar.native.includes(id);
  const key = isNarrow(page) && inTabbar ? `m2_tab_${id}` : `m2_nav_${id}`;
  if (isNarrow(page) && !inTabbar) throw new Error(`${id} is not in the native tab bar; a phone cannot reach it from a native page`);
  await page.locator(`.st-key-${key} button`).click();
}
