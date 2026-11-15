// The frame around the views: market clock, snapshot pill, degraded banner,
// mobile rail, ticker tape, rail watchlist and the refresh button.

import { $, $all, setText } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app, get } from '../core/state.js';
import * as F from '../core/format.js';
import { marketState } from '../core/market.js';
import { softRefresh, topNavigate } from '../core/host.js';
import { symTrigger } from './symbols.js';
import { toast } from './toast.js';

const MKT_COLORS = { open: 'var(--up)', pre: 'var(--warn)', after: 'var(--warn)', closed: 'var(--faint)' };

function tzHM(tz) {
  return new Intl.DateTimeFormat(F.locale(), { timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date());
}
// Short zone label for the clock (e.g. "IDT", "GMT+3"); the IANA name otherwise.
function tzLabel(tz) {
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'short' }).formatToParts(new Date());
    const name = parts.find((p) => p.type === 'timeZoneName');
    return name ? name.value : tz;
  } catch (e) { return tz; }
}

export function updClock() {
  const local = app.cfg.reportingTz;
  const clockTxt = tzHM('America/New_York') + ' ET · ' + tzHM(local) + ' ' + tzLabel(local);
  setText($('[data-clock]'), clockTxt);
  const s = marketState(new Date());
  // Below 860px the two clocks are hidden, so the pill itself carries them.
  const pill = $('[data-mkt-pill]');
  if (pill) pill.title = s.label + (s.detail ? ' (' + s.detail + ')' : '') + ' · ' + clockTxt;
  setText($('.mkt-pill .lbl'), s.label);
  const dot = $('.mkt-pill .dot');
  if (dot) {
    dot.style.setProperty('--dot-c', MKT_COLORS[s.level]);
    dot.style.backgroundColor = MKT_COLORS[s.level];
    dot.classList.toggle('closed', s.level === 'closed');
  }
}

function initStatus() {
  $all('[data-asof]').forEach((e) => { e.textContent = app.D.asOf || '—'; });
  const snapEl = $('[data-snap]');
  if (snapEl) {
    const snap = app.D.snapshot || {};
    setText(snapEl.querySelector('[data-snap-text]'), snap.text || '—');
    // This pill is the first to give up width, so the full sentence survives here.
    snapEl.title = snap.text || 'Last price snapshot';
    snapEl.classList.remove('warn', 'error', 'none');
    if (snap.level === 'warn' || snap.level === 'error' || snap.level === 'none') snapEl.classList.add(snap.level);
  }
  // Sections the payload could not load are named instead of rendering empty.
  const degEl = $('#degraded');
  if (degEl) {
    const degraded = Array.isArray(app.D.degraded) ? app.D.degraded : [];
    degEl.hidden = !degraded.length;
    degEl.textContent = degraded.length
      ? 'Some sections could not be loaded and are shown empty — ' + degraded.join(' · ') + '. Check the dashboard log.'
      : '';
  }
}

let rail, scrim, menuBtn;
export function openRail(v) {
  if (!rail) return;
  rail.classList.toggle('open', v);
  if (scrim) scrim.classList.toggle('show', v);
  if (menuBtn) menuBtn.setAttribute('aria-expanded', v ? 'true' : 'false');
}
function initRail() {
  rail = $('.rail'); scrim = $('.scrim'); menuBtn = $('.menu-btn');
  if (menuBtn) menuBtn.addEventListener('click', () => openRail(!rail.classList.contains('open')));
  if (scrim) scrim.addEventListener('click', () => openRail(false));
  const close = $('[data-rail-close]');
  if (close) close.addEventListener('click', () => { openRail(false); if (menuBtn) menuBtn.focus(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && rail && rail.classList.contains('open')) { openRail(false); if (menuBtn) menuBtn.focus(); }
  });
}

function initTape() {
  const track = $('[data-tape]');
  const syms = (app.D.tapeSyms || []).filter((s) => get(s));
  if (track && syms.length) {
    const items = syms.map((sym) => {
      const s = get(sym);
      return html`<span class="tape__item"><b>${s.sym}</b><span class="num">${F.money(s.price)}</span><span class="num ${F.chgCls(s.dayPct)}">${F.pct(s.dayPct)}</span></span>`;
    });
    setHTML(track, html`<span style="display:inline-flex">${items}</span><span style="display:inline-flex" aria-hidden="true">${items}</span>`);
  }
  const tape = $('.tape');
  if (tape) {
    tape.title = 'Click to pause / resume';
    tape.addEventListener('click', () => tape.classList.toggle('paused'));
  }
}

function initWatchlist() {
  const el = $('[data-watchlist]');
  if (!el) return;
  const wl = (app.D.watchSyms || []).filter((s) => get(s));
  setHTML(el, wl.length
    ? html`${wl.map((sym) => {
      const s = get(sym);
      return html`<div class="wl__row" ${symTrigger(s.sym)}><span class="wl__sym">${s.sym}</span><span class="wl__px">${F.money(s.price)}</span><span class="wl__chg ${F.chgCls(s.dayPct)}">${F.pct(s.dayPct)}</span></div>`;
    })}`
    : html`<div style="padding:8px 10px;font-size:var(--fs-meta);color:var(--rail-muted)">No watchlist symbols</div>`);
}

// Name every card region from the heading it already has, so a screen reader
// can jump between them. Done here so a new card cannot forget to.
function labelSections() {
  let n = 0;
  $all('section.card').forEach((sec) => {
    if (sec.getAttribute('aria-labelledby') || sec.getAttribute('aria-label')) return;
    const h = sec.querySelector('.card__hd h2');
    if (!h) return;
    if (!h.id) { n += 1; h.id = 'sect-h-' + n; }
    sec.setAttribute('aria-labelledby', h.id);
  });
}

export function initRefresh(currentView) {
  const btn = $('[data-refresh]');
  if (!btn) return null;
  btn.addEventListener('click', () => {
    toast('Refreshing…');
    // Prefer a soft rerun (clears the data cache over the websocket, no F5);
    // the freshly mounted frame restores the pane from session storage.
    if (softRefresh(currentView())) return;
    topNavigate('?view=' + currentView() + '&r=' + Date.now());
  });
  return btn;
}

export function initShell() {
  updClock();
  setInterval(updClock, 1000);
  initStatus();
  initRail();
  initTape();
  initWatchlist();
  labelSections();
}
