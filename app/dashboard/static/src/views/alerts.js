// Alerts & News: stored price alerts beside the news feed.
//
// Alerts used to be armed from a form on this page and kept in a JavaScript
// array, so the refresh the page told you to do deleted them and an "armed"
// alert could never fire (audit U-1). They are now rows in the database,
// created and deactivated on Manage → Alerts and evaluated by the server
// against the latest snapshot; this view only shows them. A payload from a
// server without stored alerts (no `alerts` key) renders an empty list with
// the same route to Manage, never a form that pretends to persist.

import { $, $all, activate, setText } from '../core/dom.js';
import { html, setHTML, safeUrl } from '../core/html.js';
import { app, get } from '../core/state.js';
import * as F from '../core/format.js';
import { FEED } from '../ui/icons.js';
import { toast } from '../ui/toast.js';
import { goNative } from '../ui/nav.js';

const READ_KEY = 'pdb_alerts_read';
function readSet() {
  try { return new Set(JSON.parse(localStorage.getItem(READ_KEY) || '[]').map(String)); } catch (e) { return new Set(); }
}
function saveRead(set) {
  try { localStorage.setItem(READ_KEY, JSON.stringify(Array.from(set).slice(-500))); } catch (e) { /* not remembered */ }
}

function dirOf(a) {
  const d = String(a.direction || '').toLowerCase();
  return (d === 'below' || d === 'down' || d === 'under') ? 'below' : 'above';
}
function when(iso) {
  const t = Date.parse(String(iso || ''));
  return Number.isNaN(t) ? (iso ? String(iso) : '') : F.fmtDate(t, 'dayTime');
}

/** Payload alerts as feed items. */
export function alertItems(alerts, read) {
  return alerts.map((a) => {
    const sym = String(a.symbol == null ? '' : a.symbol);
    const dir = dirOf(a);
    // effective_level is the level actually compared: the entered level
    // restated through any split recorded since. Say so when they differ.
    const entered = Number(a.level);
    const eff = a.effective_level == null ? entered : Number(a.effective_level);
    const levelTxt = Number.isFinite(eff) ? F.money(eff) : '—';
    const restated = Number.isFinite(eff) && Number.isFinite(entered) && F.money(eff) !== F.money(entered)
      ? ' (entered as ' + F.money(entered) + ', restated for a split)' : '';
    const last = a.last_price == null ? null : Number(a.last_price);
    const lastTxt = last == null || !Number.isFinite(last) ? 'no price collected yet'
      : 'last ' + F.money(last) + (a.last_price_ts ? ' at ' + when(a.last_price_ts) : '');
    // triggered: true / false, or null when the symbol has no quote - unknown,
    // which must not read as "not triggered".
    const triggered = a.triggered === true;
    const unknown = a.triggered !== true && a.triggered !== false;
    const title = triggered
      ? sym + (dir === 'above' ? ' is above ' : ' is below ') + levelTxt
      : sym + (dir === 'above' ? ' rises above ' : ' falls below ') + levelTxt;
    return {
      key: 'alert:' + a.id, id: String(a.id), cat: 'alert', sym, dir, triggered, unknown,
      status: triggered ? 'triggered' : 'armed', title,
      body: (a.note ? String(a.note) + ' — ' : '') + lastTxt + restated + '.',
      src: 'Price alert', time: when(a.created_at) ? 'set ' + when(a.created_at) : '',
      unread: triggered && !read.has(String(a.id)),
    };
  });
}

function newsItems(news) {
  return news.map((n, i) => ({ key: 'news:' + i, cat: 'news', sym: n.sym, title: n.title, body: n.body || '',
    src: n.src, time: n.time, url: n.url, unread: false }));
}

let items = [];
const state = { filter: 'all' };

function passes(it) {
  if (state.filter === 'triggered') return it.cat === 'alert' && it.status === 'triggered';
  if (state.filter === 'armed') return it.cat === 'alert' && it.status === 'armed';
  if (state.filter === 'news') return it.cat === 'news';
  return true;
}
function iconFor(it) {
  if (it.cat === 'news') return { cls: 'ic--news', svg: FEED.news };
  if (it.status === 'armed') return { cls: 'ic--info', svg: FEED.bell };
  return it.dir === 'below' ? { cls: 'ic--down', svg: FEED.down } : { cls: 'ic--up', svg: FEED.up };
}
function itemHTML(it) {
  const ic = iconFor(it);
  let badge;
  if (it.cat === 'alert' && it.unknown) badge = html`<span class="tag tag--neu" title="No price has been collected for this symbol, so the alert cannot be evaluated">NO QUOTE</span>`;
  else if (it.cat === 'alert' && it.status === 'armed') badge = html`<span class="tag tag--neu">WATCHING</span>`;
  else if (it.cat === 'alert') badge = html`<span class="tag ${it.dir === 'below' ? 'tag--down' : 'tag--up'}">TRIGGERED</span>`;
  else badge = html`<span class="tag tag--neu">${it.sym || '—'}</span>`;
  const url = safeUrl(it.url);
  const title = url ? html`<a href="${url}" target="_blank" rel="noopener noreferrer">${it.title}</a>` : html`${it.title}`;
  const s = it.cat === 'alert' ? get(it.sym) : null;
  return html`<div class="fitem ${it.unread ? 'unread' : ''}" data-key="${it.key}"><span class="fitem__ic ${ic.cls}">${ic.svg}</span><div class="fitem__bd"><h3>${title}</h3>${it.body ? html`<p>${it.body}</p>` : ''}<div class="fitem__meta"><span class="src">${it.src}</span><span>•</span>${badge}${s ? html`<span>•</span><span>now ${F.money(s.price)}</span>` : ''}</div></div><div class="fitem__side"><span class="fitem__time">${it.time}</span>${it.unread ? html`<button type="button" class="markread" data-read="${it.key}">Mark read</button>` : ''}</div></div>`;
}

function markRead(keys) {
  const read = readSet();
  items.forEach((it) => { if (keys.indexOf(it.key) >= 0) { it.unread = false; if (it.cat === 'alert') read.add(it.id); } });
  saveRead(read);
}

function updateCounts() {
  const unread = items.filter((it) => it.unread).length;
  $all('.nav .badge').forEach((b) => { b.textContent = unread; b.style.display = unread ? '' : 'none'; });
  setText($('#unread-count'), unread);
  const ma = $('#mark-all');
  if (ma) { ma.disabled = unread === 0; ma.title = unread === 0 ? 'Nothing unread' : 'Mark ' + unread + ' as read'; }
  setText($('#triggered-count'), items.filter((it) => it.cat === 'alert' && it.status === 'triggered').length);
  setText($('#armed-count'), items.filter((it) => it.cat === 'alert' && it.status === 'armed').length);
}

export function renderFeed() {
  const host = $('#feed');
  if (!host) return;
  const shown = items.filter(passes);
  setHTML(host, shown.length ? html`${shown.map(itemHTML)}` : html`<div class="empty">Nothing here yet.</div>`);
  $all('[data-read]', host).forEach((b) => b.addEventListener('click', () => { markRead([b.getAttribute('data-read')]); renderFeed(); }));
  updateCounts();
}

function renderAlertList() {
  const host = $('#alert-list');
  if (!host) return;
  const alerts = items.filter((it) => it.cat === 'alert');
  if (!alerts.length) {
    setHTML(host, html`<div class="empty" style="padding:14px 4px">No price alerts set.</div>`);
    return;
  }
  const tag = (it) => (it.status === 'triggered' ? 'TRIGGERED' : it.unknown ? 'NO QUOTE' : 'WATCHING');
  setHTML(host, html`<ul class="alist">${alerts.map((it) => html`<li class="alist__row"><span class="tag ${it.status === 'triggered' ? (it.dir === 'below' ? 'tag--down' : 'tag--up') : 'tag--neu'}">${tag(it)}</span><span class="alist__t">${it.title}</span></li>`)}</ul>`);
}

export function markAllRead() {
  markRead(items.filter((it) => it.unread).map((it) => it.key));
  renderFeed();
  toast('All notifications marked read');
}

export function initAlerts() {
  const alerts = Array.isArray(app.D.alerts) ? app.D.alerts : [];
  const news = Array.isArray(app.D.news) ? app.D.news : [];
  // Triggered first, then watching, then headlines.
  const al = alertItems(alerts, readSet()).sort((a, b) => Number(b.triggered) - Number(a.triggered));
  items = al.concat(newsItems(news));
  const chips = $all('[data-filter]');
  chips.forEach((chip) => chip.addEventListener('click', () => { state.filter = chip.dataset.filter; activate(chips, chip); renderFeed(); }));
  const ma = $('#mark-all');
  if (ma) ma.addEventListener('click', markAllRead);
  const manage = $('[data-manage-alerts]');
  if (manage) manage.addEventListener('click', () => goNative('manage'));
  renderAlertList();
  renderFeed();
}
