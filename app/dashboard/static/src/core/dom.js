import { item } from './dict.js';
// Small DOM helpers shared by every view. Nothing here writes markup: that is
// html.js's job alone.

export function $(s, r) { return (r || document).querySelector(s); }
export function $all(s, r) { return Array.from((r || document).querySelectorAll(s)); }

/** textContent setter that tolerates a missing element. */
export function setText(el, text) { if (el) el.textContent = text == null ? '' : String(text); }

/** Click plus Enter/Space for elements that act as buttons without being one. */
export function clickable(el, fn) {
  el.addEventListener('click', (e) => { e.preventDefault(); fn(); });
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); }
  });
}

/** Toggle `is-active` across a chip group, marking `on`. */
export function activate(chips, on) {
  chips.forEach((c) => c.classList.toggle('is-active', c === on));
}

/** Find the element in `root` whose data-sym equals `sym` without building a
 *  selector from it: a symbol is operator-entered text, and a quote in one
 *  would make querySelector throw (or match something else). */
export function bySym(root, selector, sym) {
  return $all(selector, root).find((el) => el.getAttribute('data-sym') === sym) || null;
}

export const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
  'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/** Keep Tab inside an aria-modal container. */
export function trapTab(container, e) {
  const f = $all(FOCUSABLE, container).filter((el) =>
    el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  if (!f.length) { e.preventDefault(); return; }
  const first = item(f, 0), last = item(f, f.length - 1), active = document.activeElement;
  if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
}

export function prefersReducedMotion() {
  try { return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
  catch (e) { return false; }
}
