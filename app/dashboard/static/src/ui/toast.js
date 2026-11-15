// Transient confirmation messages. role="status" + aria-live="polite" so a
// screen reader announces "Exported 12 lots" instead of it appearing silently
// (audit U-7). The live region exists before its text changes, which is what
// makes assistive technology pick the change up.

import { setHTML } from '../core/html.js';
import { CHECK } from './icons.js';

let el = null;
let timer = null;

function ensure() {
  if (el) return el;
  el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.setAttribute('aria-atomic', 'true');
  setHTML(el, CHECK);
  const span = document.createElement('span');
  el.appendChild(span);
  document.body.appendChild(el);
  return el;
}

export function toast(msg) {
  const t = ensure();
  t.querySelector('span').textContent = msg;
  t.classList.add('show');
  clearTimeout(timer);
  timer = setTimeout(() => { t.classList.remove('show'); }, 2400);
}

/** Create the region at mount so the first message is announced too. */
export function initToast() { ensure(); }
