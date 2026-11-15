// Light/dark theme: persisted per viewer, defaulting to the OS scheme (the
// pre-paint script in shell.html picks the first value before any CSS runs).

import { $ } from '../core/dom.js';
import { setHTML } from '../core/html.js';
import { themeParent } from '../core/host.js';
import { SUN, MOON } from './icons.js';

const listeners = [];

export function isDark() { return document.documentElement.getAttribute('data-theme') === 'dark'; }

/** Charts that compute colours in JS (heatmap, correlation grid) re-render. */
export function onThemeChange(fn) { listeners.push(fn); }

export function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  const b = $('[data-theme-toggle]');
  if (b) {
    setHTML(b, t === 'dark' ? SUN : MOON);
    b.setAttribute('aria-pressed', t === 'dark' ? 'true' : 'false');
  }
  try { localStorage.setItem('pdb_theme', t); } catch (e) { /* storage blocked: not remembered */ }
  try { themeParent(t === 'dark'); } catch (e) { /* parent unreachable */ }
  listeners.forEach((fn) => { try { fn(t); } catch (e) { /* one chart failing must not stop the rest */ } });
}

export function toggleTheme() { applyTheme(isDark() ? 'light' : 'dark'); }

export function initTheme() {
  applyTheme(isDark() ? 'dark' : 'light');
  const btn = $('[data-theme-toggle]');
  if (btn) btn.addEventListener('click', toggleTheme);
}
