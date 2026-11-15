// Client-side navigation between the six read-only views, and the jump out to
// the native Streamlit pages (Manage, Advisor, Data Health).

import { $, $all, clickable, setText } from '../core/dom.js';
import { syncUrl, nativeNav, onParent, parentQueryView, session } from '../core/host.js';
import { app } from '../core/state.js';
import { openRail } from './shell.js';

export const TITLES = {
  portfolio: ['Portfolio', 'Real-time overview of your holdings'],
  movers: ['Market Movers', 'Biggest moves across your tracked symbols'],
  stats: ['Statistics', 'Best and worst periods, consistency and streaks'],
  alerts: ['Alerts & News', 'Price alerts and market headlines'],
  fundamentals: ['Fundamentals', 'Company profile, financials, filings and ownership'],
  history: ['History', 'Every recorded lot and snapshot run'],
};

const firstShow = {};
const shown = {};
/** Run `fn` the first time `view` is opened (lazy, expensive renders). */
export function onFirstShow(view, fn) { firstShow[view] = fn; }

export function currentView() {
  const p = $('.view.is-active');
  return p ? p.dataset.viewPane : 'portfolio';
}

export function switchView(name, skipHistory) {
  const view = TITLES[name] ? name : 'portfolio';
  $all('.nav a[data-view]').forEach((a) => {
    const on = a.dataset.view === view;
    a.classList.toggle('is-active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  $all('.tabbar [data-tab]').forEach((b) => {
    const on = b.dataset.tab === view;
    b.classList.toggle('is-active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  $all('.view[data-view-pane]').forEach((v) => v.classList.toggle('is-active', v.dataset.viewPane === view));
  const t = TITLES[view];
  setText($('[data-title]'), t[0]);
  setText($('[data-subtitle]'), t[1]);
  if (firstShow[view] && !shown[view]) { shown[view] = true; firstShow[view](); }
  openRail(false);
  try { window.scrollTo(0, 0); } catch (e) { /* not implemented in every host */ }
  if (!skipHistory) syncUrl(view);
}

/** Open one of the native pages (Manage, Advisor, Data Health). */
export function goNative(view) { nativeNav(view); }

export function initNav() {
  $all('.nav a[data-view]').forEach((a) => clickable(a, () => switchView(a.dataset.view)));
  $all('.nav a[data-nav]').forEach((a) => clickable(a, () => goNative(a.dataset.nav)));
  $all('.tabbar [data-tab]').forEach((b) => b.addEventListener('click', () => switchView(b.dataset.tab)));
}

/** Open the pane the URL or a soft refresh asked for, and follow back/forward. */
export function restoreView() {
  // A soft refresh stashes the pane it was on (consumed once).
  const initial = session.take('pdb_refresh_view') || app.D.initialView;
  if (initial && initial !== 'portfolio' && TITLES[initial]) switchView(initial, true);
  onParent('popstate', (ev) => {
    const v = (ev.state && ev.state.pdbView) || parentQueryView() || 'portfolio';
    if (TITLES[v]) switchView(v, true);
  });
}
