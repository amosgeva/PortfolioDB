// CSV export cells.
//
// Quoting a cell is not enough: a spreadsheet still evaluates a quoted cell
// that starts with = + - @ (or a tab / carriage return, which some importers
// strip before looking). lots.notes is free text and can arrive from an
// imported broker CSV, so an exported "=HYPERLINK(…)" note would run in the
// operator's spreadsheet. Text cells with a leading trigger character get a
// single quote in front (the OWASP-recommended neutraliser), which every
// spreadsheet shows as text. Numbers are written as numbers: a negative
// quantity or P&L is data, not a formula, and must stay numeric.

const TRIGGER = /^[=+\-@\t\r]/;

export function csvCell(v) {
  if (v === null || v === undefined) return '""';
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : '""';
  let s = String(v);
  if (TRIGGER.test(s)) s = '\'' + s;
  return '"' + s.replace(/"/g, '""') + '"';
}

/** Rows of objects to CSV text, one column per header key. */
export function toCsv(header, rows) {
  const lines = [header.map(csvCell).join(',')];
  rows.forEach((r) => { lines.push(header.map((k) => csvCell(r[k])).join(',')); });
  return lines.join('\r\n') + '\r\n';
}
