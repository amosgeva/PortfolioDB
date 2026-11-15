// Phone disclosure. On a phone the Portfolio view is reordered by CSS so the
// check-in comes first; cards that are reference rather than answer keep their
// heading and open on request. The control is built here so a desktop visit
// never carries a button it cannot use, and the state resets to closed on every
// load - a remembered fold is a page that looks different for no visible reason.

import { $all } from '../core/dom.js';
import { setHTML } from '../core/html.js';
import { CHEVRON } from './icons.js';

const PHONE = '(max-width:640px)';
let seq = 0;

function title(card) {
  const h = card.querySelector('.card__hd h2');
  return h ? h.textContent.trim() : 'section';
}
function setFold(card, open) {
  card.classList.toggle('is-open', open);
  const btn = card.querySelector(':scope > .card__hd > .fold-btn');
  if (!btn) return;
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  btn.setAttribute('aria-label', (open ? 'Hide ' : 'Show ') + title(card));
}

export function wireFolds() {
  let on = false;
  try { on = Boolean(window.matchMedia && window.matchMedia(PHONE).matches); } catch (e) { on = false; }
  $all('[data-fold]').forEach((card) => {
    const hd = card.querySelector(':scope > .card__hd');
    if (!hd) return;
    // The body is whatever is not the heading; aria-controls names what hides.
    const bd = Array.from(card.children).find((c) => !c.classList.contains('card__hd'));
    if (!bd) return;
    let btn = card.querySelector(':scope > .card__hd > .fold-btn');
    if (!on) {
      card.classList.remove('foldable', 'is-open');
      if (btn) btn.remove();
      return;
    }
    if (!bd.id) { seq += 1; bd.id = 'fold-bd-' + seq; }
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'fold-btn';
      btn.setAttribute('aria-controls', bd.id);
      setHTML(btn, CHEVRON);
      hd.appendChild(btn);
      // The whole heading is the target, but a chip or select inside keeps its job.
      if (!hd.dataset.foldWired) {
        hd.dataset.foldWired = '1';
        hd.addEventListener('click', (e) => {
          if (!card.classList.contains('foldable')) return;
          if (e.target.closest('a,button,input,select,label') && !e.target.closest('.fold-btn')) return;
          setFold(card, !card.classList.contains('is-open'));
        });
      }
    }
    card.classList.add('foldable');
    setFold(card, card.classList.contains('is-open'));
  });
}

export function initFolds() {
  wireFolds();
  try {
    const mq = window.matchMedia && window.matchMedia(PHONE);
    if (mq && mq.addEventListener) mq.addEventListener('change', wireFolds);
  } catch (e) { /* matchMedia unavailable: folds stay as rendered */ }
}
