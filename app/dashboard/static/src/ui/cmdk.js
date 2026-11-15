// Command palette (Ctrl+K): symbols, views and actions.

import { $, $all, trapTab } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { list } from '../core/state.js';
import * as F from '../core/format.js';

let cmdk, input, listEl, sel = 0, items = [], prevFocus = null;
let commandsFor = () => [];

export function isPaletteOpen() { return Boolean(cmdk && cmdk.classList.contains('open')); }

function score(c, q) {
  if (!q) return 1;
  const hay = (c.label + ' ' + (c.detail || '')).toLowerCase();
  if (c.label.toLowerCase().indexOf(q) === 0) return 3;
  return hay.indexOf(q) >= 0 ? 2 : 0;
}

function render() {
  const q = (input.value || '').trim().toLowerCase();
  items = commandsFor().map((c) => ({ c, s: score(c, q) })).filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s).slice(0, 24).map((x) => x.c);
  if (sel >= items.length) sel = 0;
  let lastSect = null;
  const rows = items.map((c, i) => {
    const head = c.sect !== lastSect ? html`<div class="cmdk__sect" role="presentation">${c.sect}</div>` : '';
    lastSect = c.sect;
    return html`${head}<div class="cmdk__item${i === sel ? ' is-sel' : ''}" id="${'cmdk-opt-' + i}" data-ci="${i}" role="option" aria-selected="${i === sel}"><b>${c.label}</b>${c.detail ? html`<span style="font-size:var(--fs-meta);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.detail}</span>` : ''}${c.px ? html`<span class="px ${c.upDn ? 'up' : 'down'}">${c.px}</span>` : ''}</div>`;
  });
  setHTML(listEl, rows.length ? html`${rows}` : html`<div class="empty">No matches.</div>`);
  if (items.length) input.setAttribute('aria-activedescendant', 'cmdk-opt-' + sel); else input.removeAttribute('aria-activedescendant');
  $all('[data-ci]', listEl).forEach((el) => {
    const i = Number(el.getAttribute('data-ci'));
    el.addEventListener('click', () => run(i));
    el.addEventListener('mousemove', () => { if (sel !== i) { sel = i; render(); } });
  });
}
function scrollSel() {
  const el = listEl.querySelector('.is-sel');
  if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
}
function run(i) { const c = items[i]; closePalette(); if (c) c.run(); }

export function openPalette() {
  if (!cmdk) return;
  prevFocus = document.activeElement;
  cmdk.classList.add('open'); input.value = ''; sel = 0; render(); input.focus();
}
export function closePalette() {
  if (!isPaletteOpen()) return;
  cmdk.classList.remove('open');
  // A command that moves the user takes focus afterwards, which is why run()
  // closes before it runs.
  if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (e) { /* gone */ } }
  prevFocus = null;
}

/** `staticCommands()` returns the view/action entries; symbols are appended. */
export function initPalette(staticCommands, openDrawer) {
  cmdk = $('[data-cmdk]'); input = $('[data-cmdk-input]'); listEl = $('[data-cmdk-list]');
  commandsFor = () => staticCommands().concat(list().slice().sort((a, b) => String(a.sym).localeCompare(String(b.sym))).map((s) => ({
    sect: 'Symbols', label: String(s.sym), detail: s.name, px: F.money(s.price) + '  ' + F.pct(s.dayPct),
    upDn: s.dayPct != null && s.dayPct >= 0, run: () => openDrawer(s.sym),
  })));
  if (!cmdk) return;
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-controls', 'cmdk-list');
  input.setAttribute('aria-expanded', 'true');
  listEl.id = 'cmdk-list';
  cmdk.addEventListener('mousedown', (e) => { if (e.target === cmdk) closePalette(); });
  input.addEventListener('input', () => { sel = 0; render(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, items.length - 1); render(); scrollSel(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); render(); scrollSel(); }
    else if (e.key === 'Enter') { e.preventDefault(); run(sel); }
    else if (e.key === 'Escape') { closePalette(); }
  });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); openPalette(); }
    else if (e.key === 'Escape' && isPaletteOpen()) closePalette();
    else if (e.key === 'Tab' && isPaletteOpen()) trapTab(cmdk, e);
  });
  const gsearch = $('[data-global-search]');
  if (gsearch) {
    gsearch.addEventListener('click', openPalette);
    // Opening on focus trapped keyboard users; Enter or Space opens it, Tab passes.
    gsearch.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPalette(); } });
  }
  // The phone's route to the palette (the search field is hidden below 980px).
  const btn = $('[data-open-search]');
  if (btn) btn.addEventListener('click', openPalette);
}
