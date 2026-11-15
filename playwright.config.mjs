// Real-browser integration tests for the dashboard (audit U-9): Chromium
// against a real `streamlit run app/streamlit_app.py` on a real Postgres.
// The jsdom suite (npm test) proves the front end's logic against a fake
// parent; this proves the embedding itself - the iframe Streamlit builds, the
// private selectors core/host.js reaches for, the native pages - at a phone,
// a tablet and a desktop width. Run by the "Browser integration" job in
// .github/workflows/frontend.yml; locally:
//
//   PORTFOLIODB_* pointing at a demo-seeded database (never production)
//   PDB_E2E_PYTHON=<python with app/requirements.txt>   (default: python)
//   npx playwright install chromium   (once)
//   npm run e2e
//
// The server is started here (webServer) with its own dashboard password, on
// 127.0.0.1:PDB_E2E_PORT (default 18521), and stopped afterwards.

import { defineConfig } from '@playwright/test';
// A throwaway for the server this config starts; the write-gate test types it.
import { DASHBOARD_PASSWORD } from './app/dashboard/static/e2e/support.mjs';

const PORT = Number(process.env.PDB_E2E_PORT || 18521);
const PYTHON = process.env.PDB_E2E_PYTHON || 'python';

const WIDTHS = [
  { name: 'phone-360', viewport: { width: 360, height: 740 }, isMobile: true, hasTouch: true },
  { name: 'tablet-768', viewport: { width: 768, height: 1024 }, hasTouch: true },
  { name: 'desktop-1440', viewport: { width: 1440, height: 900 } },
];

export default defineConfig({
  testDir: 'app/dashboard/static/e2e',
  testMatch: '*.spec.mjs',
  // One Streamlit process serves every test; each test gets its own browser
  // context and so its own Streamlit session.
  workers: 1,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  // A flaky embedding is a finding, not noise: no retries.
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]] : 'list',
  outputDir: 'test-results',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    browserName: 'chromium',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: WIDTHS.map(({ name, ...use }) => ({ name, use })),
  webServer: {
    command: `"${PYTHON}" -m streamlit run app/streamlit_app.py --server.port ${PORT} --server.address 127.0.0.1 ` +
      '--server.headless true --server.fileWatcherType none --browser.gatherUsageStats false',
    url: `http://127.0.0.1:${PORT}/_stcore/health`,
    timeout: 120_000,
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
    env: { ...process.env, PORTFOLIODB_DASHBOARD_PASSWORD: DASHBOARD_PASSWORD },
  },
});
