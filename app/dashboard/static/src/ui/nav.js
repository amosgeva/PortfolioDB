// Client-side navigation between the read-only views, and the jump out to the
// native Streamlit pages (Manage, Advisor, Data Health).
//
// What the rail and the phone tab bar contain - ids, labels, groups, glyphs -
// is nav.json, the file the native pages' rail is drawn from as well
// (app/dashboard/nav.py, audit R-3). This module draws both from it; the
// contract tests (tests/nav.test.mjs, app/tests/test_nav_contract.py) fail if
// either half stops following it.

import { $, $all, clickable, setText } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { syncUrl, nativeNav, onParent, parentQueryView, session } from '../core/host.js';
import { app } from '../core/state.js';
import { openRail } from './shell.js';
import { icon } from './icons.js';
import { item } from '../core/dict.js';
import NAV from '../../nav.json';

const RENDERER = new Map(NAV.groups.map((g) => [g.id, g.renderer]));

/** Every view in rail order, each with `native`: drawn by Streamlit, not here. */
export const VIEWS = NAV.views.map((v) => ({ ...v, native: RENDERER.get(v.group) === 'native' }));

/** The panes this front end draws: id -> [title, subtitle]. */
export const TITLES = Object.fromEntries(VIEWS.filter((v) => !v.native).map((v) => [v.id, [v.label, v.subtitle]]));
const FIRST = VIEWS.find((v) => !v.native).id;

const BADGE = html` <span class="badge">0</span>`;

function railEntry(v) {
  if (v.native) return html`<a data-nav="${v.id}" role="link" tabindex="0">${icon(v.icon)} ${v.label}</a>`;
  return html`<a data-view="${v.id}" role="link" tabindex="0">${icon(v.icon)} ${v.label}${v.badge ? BADGE : ''}</a>`;
}

function railGroup(g) {
  const entries = VIEWS.filter((v) => v.group === g.id).map(railEntry);
  return html`<div class="rail__sect" data-rail-group="${g.id}"><div class="rail__label">${g.label}</div><nav class="nav" aria-label="${g.label}">${entries}</nav></div>`;
}

function tab(v) {
  return html`<button type="button" data-tab="${v.id}">${icon(v.icon)}<span>${v.short}</span></button>`;
}

/** Draw the rail's groups and the tab bar from nav.json. */
export function renderNav() {
  const rail = $('[data-rail-nav]');
  if (rail) setHTML(rail, html`${NAV.groups.map(railGroup)}`);
  const bar = $('[data-tabbar]');
  if (bar) {
    const byId = new Map(VIEWS.map((v) => [v.id, v]));
    setHTML(bar, html`${NAV.tabbar.frame.map((id) => tab(byId.get(id)))}`);
  }
}

const firstShow = new Map();
/** Run `fn` the first time `view` is opened (lazy, expensive renders). */
export function onFirstShow(view, fn) { firstShow.set(view, fn); }

export function currentView() {
  const p = $('.view.is-active');
  return p ? p.dataset.viewPane : FIRST;
}

export function switchView(name, skipHistory) {
  const view = item(TITLES, name) ? name : FIRST;
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
  const t = item(TITLES, view);
  setText($('[data-title]'), item(t, 0));
  setText($('[data-subtitle]'), item(t, 1));
  const lazy = firstShow.get(view);
  if (lazy) { firstShow.delete(view); lazy(); }
  openRail(false);
  try { window.scrollTo(0, 0); } catch (e) { /* not implemented in every host */ }
  if (!skipHistory) syncUrl(view);
}

/** Open one of the native pages (Manage, Advisor, Data Health). */
export function goNative(view) { nativeNav(view); }

/** Go to any nav.json view, whichever half draws it. */
export function goTo(id) {
  const v = VIEWS.find((x) => x.id === id);
  if (v?.native) goNative(id); else switchView(id);
}

export function initNav() {
  renderNav();
  // The pane the first paint shows, before restoreView() picks the real one.
  switchView(FIRST, true);
  $all('.nav a[data-view]').forEach((a) => clickable(a, () => switchView(a.dataset.view)));
  $all('.nav a[data-nav]').forEach((a) => clickable(a, () => goNative(a.dataset.nav)));
  $all('.tabbar [data-tab]').forEach((b) => b.addEventListener('click', () => switchView(b.dataset.tab)));
}

/** Open the pane the URL or a soft refresh asked for, and follow back/forward. */
export function restoreView() {
  // A soft refresh stashes the pane it was on (consumed once).
  const initial = session.take('pdb_refresh_view') || app.D.initialView;
  if (initial && initial !== FIRST && item(TITLES, initial)) switchView(initial, true);
  onParent('popstate', (ev) => {
    const v = ev.state?.pdbView || parentQueryView() || FIRST;
    if (item(TITLES, v)) switchView(v, true);
  });
}
