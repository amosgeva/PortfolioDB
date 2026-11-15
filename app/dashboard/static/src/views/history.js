// History: every recorded lot and the snapshot log, plus both CSV exports.

import { $ } from '../core/dom.js';
import { html, setHTML } from '../core/html.js';
import { app, holdingRows, lotTotalAll } from '../core/state.js';
import * as F from '../core/format.js';
import { toCsv } from '../core/csv.js';
import { toast } from '../ui/toast.js';

/** Offer `text` as a file download. Best effort: a sandboxed frame may refuse. */
export function download(name, text) {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; a.style.display = 'none';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export const LOT_COLUMNS = ['id', 'symbol', 'account', 'side', 'date', 'qty', 'price', 'fees', 'notes'];
export const POSITION_COLUMNS = ['symbol', 'name', 'sector', 'qty', 'avgCost', 'price', 'mktVal', 'cost', 'gl', 'glPct'];

export function lotsCsv() { return toCsv(LOT_COLUMNS, app.D.histLots || []); }
export function positionsCsv() {
  return toCsv(POSITION_COLUMNS, holdingRows().map((r) => ({ ...r, symbol: r.sym })));
}

export function exportLots() {
  const n = (app.D.histLots || []).length;
  try { download('lots.csv', lotsCsv()); toast('Exported ' + n + ' lots'); }
  catch (e) { toast('Export blocked by browser sandbox'); }
}
export function exportPositions() {
  const n = holdingRows().length;
  try { download('positions.csv', positionsCsv()); toast('Exported ' + n + ' positions'); }
  catch (e) { toast('Export blocked — use Manage › Exports'); }
}

export function renderHistory() {
  const host = $('#history-body');
  if (!host) return;
  const lots = app.D.histLots || [], snaps = app.D.snapLog || [];
  const total = lotTotalAll();
  const lotRow = (l) => html`<tr><td class="num" style="color:var(--faint)">${l.id}</td><td><b>${l.symbol}</b></td><td style="text-align:left;color:var(--muted)">${l.account}</td><td><span class="tag ${l.side === 'BUY' ? 'tag--up' : 'tag--down'}">${l.side}</span></td><td class="num">${l.date}</td><td class="num">${F.qty(l.qty)}</td><td class="num">${F.money(l.price)}</td><td class="num" style="color:var(--muted)">${F.money(l.fees)}</td><td style="text-align:left;color:var(--muted);max-width:240px;overflow:hidden;text-overflow:ellipsis">${l.notes}</td></tr>`;
  const snapRow = (s) => html`<tr><td class="num" style="text-align:left">${s.ts}</td><td class="num">${s.symbols}</td><td style="text-align:left;color:var(--muted)">${s.source}</td></tr>`;
  setHTML(host, html`<section class="card"><div class="card__hd"><h2>All lots</h2><span class="sub">${total > lots.length ? 'showing ' + lots.length + ' of ' + total : 'all ' + lots.length}</span><button type="button" class="btn btn--ghost" id="lots-csv" style="margin-left:auto">Export CSV</button></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>ID</th><th>Symbol</th><th style="text-align:left">Account</th><th>Side</th><th>Date</th><th>Qty</th><th>Price</th><th>Fees</th><th style="text-align:left">Notes</th></tr></thead><tbody>${lots.length ? lots.map(lotRow) : html`<tr><td colspan="9"><div class="empty">No lots recorded.</div></td></tr>`}</tbody></table></div></section><section class="card" style="margin-top:20px"><div class="card__hd"><h2>Snapshot log</h2><span class="sub">last 30 runs</span></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th style="text-align:left">Timestamp</th><th>Symbols</th><th style="text-align:left">Source</th></tr></thead><tbody>${snaps.length ? snaps.map(snapRow) : html`<tr><td colspan="3"><div class="empty">No snapshots.</div></td></tr>`}</tbody></table></div></section>`);
  const btn = $('#lots-csv');
  if (btn) btn.addEventListener('click', exportLots);
}
