"""Import portfolio/trade history from CSV into PortfolioDB.

Written against Yahoo Finance portfolio exports; any broker export works once
its columns are renamed (docs/csv-import.md).

Imports two things:
1) Lots (trade lots) from rows that have Trade Date + Purchase Price + Quantity
2) Price snapshots (append-only) for each CSV file timestamp for all rows with a Current Price.

Direction:
- An optional `Side` column carries BUY/SELL. Absent means BUY, which is what a
  holdings export is. A trade *history* needs it: without it every row imports
  as a purchase, which overstates open quantity and reports no realized P&L at
  all — wrong in the quiet way, since nothing errors.
- Quantity must be positive; the direction lives in `Side`. A negative quantity
  is rejected rather than assumed to be a sale.

Assumptions:
- CSV files live in the directory passed via --dir
- Each file contains a Date + Time column (e.g., 2026/02/20 and '16:00 EST')
- We treat EACH CSV file as one snapshot timestamp.

De-dupe:
- Lots: unique index (symbol, account, side, trade_date, quantity, price)
- Snapshots: PK (symbol, ts)

Account mapping:
- Each lot's account comes from the CSV Comment column when it mentions one of
  --tagged-accounts (case-insensitive substring match); otherwise it falls back
  to --default-account.

Usage:
  set PORTFOLIODB_PASSWORD=...
  python import_csv_history.py --dir "path/to/csv" --default-account IBKR --dry-run
  python import_csv_history.py --dir "path/to/csv" --default-account BrokerA --tagged-accounts IBKR,BrokerB

"""

from __future__ import annotations

import argparse
import csv
import glob
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

import ledger_numbers
import oversell
from db import connect, load_config, run


# zoneinfo, not pytz. A bare `pytz.timezone("America/New_York")` carries the
# LMT offset (-4:56, the earliest one in the database) until .localize() picks a
# real one, and this module handed exactly that object to dateutil as a tzinfos
# value — so every row with an explicit EST/EDT token was converted at -4:56.
# ZoneInfo has no such stand-in state: it resolves the offset from the datetime.
NY_TZ = ZoneInfo("America/New_York")

# The token in the file states its own offset, so honour it literally rather
# than re-deriving one from the date. It also sidesteps the ambiguous hour each
# November, where the date alone cannot say which side of the fold a row is on.
_TZ_TOKENS = {
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
}


def infer_account(comment: str | None, default_account: str, tagged_accounts: list[str]) -> str:
    """Pick the lot's account: a tagged account mentioned in the CSV comment
    wins; otherwise the --default-account."""
    if comment:
        c = comment.strip().upper()
        for tag in tagged_accounts:
            if tag.upper() in c:
                return tag
    return default_account


def parse_trade_date(yyyymmdd: str) -> datetime.date:
    # format like 20260220
    return datetime.strptime(yyyymmdd.strip(), "%Y%m%d").date()


def parse_snapshot_ts(date_str: str, time_str: str) -> datetime:
    # Examples:
    # date_str: 2026/02/20
    # time_str: 16:00 EST
    # time_str: 15:59 EDT
    s = f"{date_str.strip()} {time_str.strip()}"
    # Handle timezone tokens like 'EST'/'EDT' explicitly.
    dt = dtparser.parse(s, tzinfos=_TZ_TOKENS)

    # If tzinfo missing, assume New York time. `.replace()` is the zoneinfo
    # equivalent of pytz's .localize(): ZoneInfo works out EST vs EDT from the
    # datetime itself, so there is no separate localise step to forget.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)

    return dt.astimezone(timezone.utc)


BUY_WORDS = {"BUY", "B", "BOT", "BOUGHT", "PURCHASE"}
SELL_WORDS = {"SELL", "S", "SLD", "SOLD", "SALE"}


def parse_side(raw: str | None) -> str:
    """Map a broker's word for a direction onto BUY/SELL.

    Absent means BUY, because that is what a portfolio export is: a list of
    holdings you bought. Trade *history* exports do carry a direction, and
    before this existed every row imported as a BUY — so a history with sales
    produced overstated positions and a realized P&L of zero, with nothing
    saying so.
    """
    v = (raw or "").strip().upper()
    if not v:
        return "BUY"
    if v in BUY_WORDS:
        return "BUY"
    if v in SELL_WORDS:
        return "SELL"
    raise ValueError(
        f"unrecognised Side {raw!r} — use BUY or SELL "
        "(B/BOT/BOUGHT and S/SLD/SOLD are accepted too)"
    )


def parse_quantity(raw: str) -> Decimal:
    """Quantity is always positive; `side` carries the direction.

    A negative quantity is how several brokers encode a sale, but guessing that
    is not safe: it is also how some encode a short, an option assignment or a
    corrective entry. Refusing costs the reader one column; guessing wrong
    corrupts a cost basis and shows up months later as an inexplicable P&L.

    Finite and exact: this used to be ``float(raw)``, which accepted "NaN" —
    and NaN is neither < 0 nor == 0, so it sailed through both checks below
    and into a column whose `> 0` constraint NaN also satisfies (audit F09).
    """
    # Sign first, so the message for the common broker case stays the useful
    # one; ledger_numbers then handles NaN, infinity, range and text.
    try:
        probe = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        probe = None
    if probe is not None and probe.is_finite() and probe < 0:
        raise ValueError(
            f"negative quantity {probe}. Some brokers encode a sale this way — "
            "if that is what this row is, add a Side column with SELL and make "
            "the quantity positive. This importer will not guess."
        )
    if probe is not None and probe.is_finite() and probe == 0:
        raise ValueError("quantity is 0")
    return ledger_numbers.parse_quantity(raw)


def to_float(val: str | None) -> float | None:
    """A price for a snapshot, or None when the cell is blank, not a number,
    or not finite — a NaN 'Current Price' is no price, not a price of NaN."""
    if val is None:
        return None
    v = str(val).strip()
    if not v:
        return None
    try:
        f = float(v)
    except Exception:
        return None
    return f if math.isfinite(f) else None


# Every write below runs INSIDE the caller's transaction (db.run, never
# db.execute): the file is the unit of work, and _import_file decides when it
# commits. Each returns the row count, so an ON CONFLICT DO NOTHING that skipped
# a duplicate reads as 0 and an inserted row as 1.


def upsert_instrument(conn, symbol: str) -> int:
    return run(
        conn,
        "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
        (symbol,),
    )


def insert_lot(conn, symbol: str, account: str | None, trade_date, qty, price, fees, notes: str | None, side: str = "BUY") -> int:
    return run(
        conn,
        """
        INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (symbol, account, side, trade_date, qty, price, fees, notes),
    )


def insert_snapshot(conn, ts_utc: datetime, symbol: str, last_price) -> int:
    return run(
        conn,
        """
        INSERT INTO price_snapshots(ts, symbol, last_price, bid, ask, source)
        VALUES (%s, %s, %s, NULL, NULL, 'csv')
        ON CONFLICT DO NOTHING
        """,
        (ts_utc, symbol, last_price),
    )


def contained_matches(base_dir: Path, pattern: str) -> tuple[list[str], int]:
    """Glob `pattern` under `base_dir`, dropping anything that escapes it.

    Returns (sorted file paths, count ignored).

    --dir and --pattern are both caller-supplied and were joined straight into
    a glob, so `--pattern '../../*.csv'` read outside the directory the
    operator named — the finding behind this function. Resolving both sides and
    requiring base_dir to be a genuine parent closes that, and because
    .resolve() collapses symlinks it also means a link pointing out of the tree
    is excluded rather than quietly followed.

    Separated from main() so the containment is testable on its own: it is the
    only security-relevant logic in this script, and it should not need a
    database and a CSV corpus to exercise.

    Two layers, in this order, because the order is the point:

    1. The pattern is rejected outright if it is absolute or contains a `..`
       segment. This happens BEFORE the glob, so a traversal pattern never
       reaches the filesystem at all. Filtering afterwards was the first
       attempt and it is not equivalent: glob still walks `../..` to build the
       match list, so the process touches directories the operator never named
       even though nothing outside is returned.
    2. Surviving matches are still resolved and re-checked against base_dir,
       because step 1 cannot see through a symlink — a link *inside* the
       directory can still point out of it, and only the resolved path says so.

    Raises ValueError for a rejected pattern; returns (sorted paths, ignored).
    """
    if os.path.isabs(pattern) or any(
        segment == ".." for segment in pattern.replace("\\", "/").split("/")
    ):
        raise ValueError(
            f"--pattern must stay inside --dir; refusing {pattern!r} "
            "(absolute paths and '..' segments are not allowed)"
        )

    files: list[str] = []
    escaped = 0
    for match in glob.glob(os.path.join(str(base_dir), pattern)):
        resolved = Path(match).resolve()
        if not resolved.is_file():
            continue
        if base_dir not in resolved.parents:
            escaped += 1
            continue
        files.append(str(resolved))
    return sorted(files), escaped


def _snapshot_ts(rows: list[dict]) -> datetime | None:
    """UTC timestamp of the file, taken from the first row carrying Date + Time.

    None when no row has both or that pair does not parse: the file's lots are
    still imported, only its price snapshot is skipped.
    """
    for r in rows:
        if r.get('Date') and r.get('Time'):
            try:
                return parse_snapshot_ts(r['Date'], r['Time'])
            except Exception:
                return None
    return None


# ─── Transaction policy ─────────────────────────────────────────────────────
#
# A file is the unit of work. Validation happens first, over every row, with
# no database in sight; only a file that parsed clean is written, and it is
# written in one transaction that commits at the end. So an import either
# happened or it did not, and "Rows failed: 1" never again means "and the
# other 213 are in, plus the instruments they referenced".
#
# --continue-on-error is the explicit partial-success mode: the parse-clean
# rows are written, each under its own SAVEPOINT so a row PostgreSQL rejects
# (a constraint) is rolled back to and the rest carry on. Without a savepoint
# a failed statement leaves the whole transaction aborted, and every later
# statement fails with "current transaction is aborted" — which is what this
# script used to do while reporting the later rows as errors of their own and
# having already committed the earlier ones (audit F08).
#
# Exit status says what happened, for the scheduler or the operator's shell:
#   0  everything the files described is in the database (or was already)
#   1  something was rejected and NOT written — a file rolled back, no files,
#      a refused pattern
#   2  partial success under --continue-on-error: the valid rows are in, the
#      rejected ones are listed above
# --dry-run validates and reports with the same codes and writes nothing.

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2

COUNT_KEYS = ("attempted", "inserted", "duplicate", "rejected", "sells", "oversells",
              "snaps_inserted", "snaps_duplicate")


def _empty_counts() -> dict[str, int]:
    return {k: 0 for k in COUNT_KEYS}


def _parse_lot(r: dict, sym: str, args, tagged_accounts: list[str]) -> dict | None:
    """Validate one CSV row into a lot dict, or None when the row is not a lot.

    Raises ValueError with the reason when the row is a lot and is malformed.
    Pure: no database.
    """
    trade_date_raw = (r.get('Trade Date') or '').strip()
    purchase_raw = (r.get('Purchase Price') or '').strip()
    qty_raw = (r.get('Quantity') or '').strip()
    present = {"Trade Date": trade_date_raw, "Purchase Price": purchase_raw, "Quantity": qty_raw}
    if not any(present.values()):
        return None                      # a quote-only / watchlist row, by design
    missing = [name for name, value in present.items() if not value]
    if missing:
        # Some trade fields filled, some not: an incomplete trade, not a quote
        # row. This used to fall through as "not a lot" and import nothing for
        # the trade while still counting the row as clean (re-audit N02).
        raise ValueError(
            f"incomplete trade row — missing {', '.join(missing)}. A row with a "
            "Trade Date, Purchase Price or Quantity is a trade and needs all three; "
            "leave all three blank for a quote-only row."
        )
    comm_raw = (r.get('Commission') or '').strip()
    comment = r.get('Comment')
    return {
        "symbol": sym,
        "account": infer_account(comment, args.default_account, tagged_accounts),
        "side": parse_side(r.get('Side')),
        "trade_date": parse_trade_date(trade_date_raw),
        "quantity": parse_quantity(qty_raw),
        "price": ledger_numbers.parse_price(purchase_raw),
        "fees": ledger_numbers.parse_fees(comm_raw),
        "notes": comment,
    }


# The columns whose presence makes a line a row rather than blank padding.
_CONTENT_COLUMNS = ('Trade Date', 'Purchase Price', 'Quantity', 'Current Price')


def _validate_file(rows: list[dict], fp: str, args, tagged_accounts: list[str]) -> tuple[list[dict], list[tuple[str, float]], int]:
    """Parse every row before anything is written.

    Returns (lots, snapshots as (symbol, price), rejected count). Each
    rejected row is printed with its line number and reason.
    """
    lots: list[dict] = []
    snaps: list[tuple[str, float]] = []
    rejected = 0
    for line_no, r in enumerate(rows, start=2):   # line 1 is the header
        sym = (r.get('Symbol') or '').strip().upper()
        try:
            if not sym:
                if not any((r.get(k) or '').strip() for k in _CONTENT_COLUMNS):
                    continue             # a wholly blank line, not a row
                raise ValueError("no Symbol on a row that carries trade or price data")
            lot = _parse_lot(r, sym, args, tagged_accounts)
        except Exception as e:
            print(
                f"ERROR {os.path.basename(fp)}:{line_no} | {sym or '(no symbol)'} | "
                f"trade_date={(r.get('Trade Date') or '').strip()!r} "
                f"qty={(r.get('Quantity') or '').strip()!r} "
                f"price={(r.get('Purchase Price') or '').strip()!r}: {e}"
            )
            rejected += 1
            continue
        if lot is not None:
            lots.append(lot)
        cp = to_float(r.get('Current Price'))
        if cp is not None:
            snaps.append((sym, cp))
    return lots, snaps, rejected


def _write_file(conn, lots: list[dict], snaps: list[tuple[str, float]], ts_utc: datetime | None, fp: str, args) -> dict[str, int]:
    """Write one validated file inside one transaction.

    Atomic by default: any database error rolls the whole file back and is
    re-raised. With --continue-on-error each row runs under a savepoint, a
    rejected row is rolled back to its savepoint and counted, and the file
    commits with whatever succeeded.
    """
    counts = _empty_counts()
    base = os.path.basename(fp)

    def _row(i: int, work) -> bool:
        if not args.continue_on_error:
            work()
            return True
        run(conn, f"SAVEPOINT row_{i}")
        try:
            work()
        except Exception as e:
            run(conn, f"ROLLBACK TO SAVEPOINT row_{i}")
            print(f"ERROR {base} | row {i + 1} rejected by the database: {str(e).strip().splitlines()[0]}")
            return False
        run(conn, f"RELEASE SAVEPOINT row_{i}")
        return True

    try:
        for i, lot in enumerate(lots):
            counts["attempted"] += 1

            def _insert(lot=lot):
                if lot["side"] == "SELL":
                    # Against the ledger as it stands inside this transaction,
                    # so earlier rows of the same file count. Warns, never
                    # refuses — see app/oversell.py.
                    warning = oversell.oversell_warning(
                        conn, lot["symbol"], lot["account"], lot["trade_date"], lot["quantity"]
                    )
                    if warning:
                        counts["oversells"] += 1
                        print(f"{base} | {warning}")
                upsert_instrument(conn, lot["symbol"])
                n = insert_lot(conn, lot["symbol"], lot["account"], lot["trade_date"],
                               lot["quantity"], lot["price"], lot["fees"], lot["notes"],
                               side=lot["side"])
                counts["inserted" if n else "duplicate"] += 1
                if n and lot["side"] == "SELL":
                    counts["sells"] += 1

            if not _row(i, _insert):
                counts["rejected"] += 1

        if ts_utc is not None:
            for j, (sym, cp) in enumerate(snaps):
                def _snap(sym=sym, cp=cp):
                    upsert_instrument(conn, sym)
                    n = insert_snapshot(conn, ts_utc, sym, cp)
                    counts["snaps_inserted" if n else "snaps_duplicate"] += 1

                if not _row(len(lots) + j, _snap):
                    counts["rejected"] += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(
            f"ERROR {base} | the database rejected a row: {str(e).strip().splitlines()[0]}\n"
            f"       The whole file was rolled back; nothing from it was written. "
            f"Fix the row, or use --continue-on-error to import the rows that pass."
        )
        rolled_back = _empty_counts()
        rolled_back["attempted"] = counts["attempted"]
        rolled_back["rejected"] = 1
        return rolled_back
    return counts


def _import_file(conn, fp: str, args, tagged_accounts: list[str]) -> dict[str, int] | None:
    """Import one CSV file: its lots, and one price snapshot per priced row.

    Returns the file's counts, or None when the file had no rows and was
    skipped without being counted. See the transaction policy above.
    """
    with open(fp, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    base = os.path.basename(fp)

    ts_utc = _snapshot_ts(rows)
    lots, snaps, rejected = _validate_file(rows, fp, args, tagged_accounts)
    counts = _empty_counts()
    counts["rejected"] = rejected

    if rejected and not args.continue_on_error:
        print(
            f"{base} | REJECTED: {rejected} row(s) failed to parse; nothing from this "
            f"file was written. Fix them, or use --continue-on-error to import the rest."
        )
        return counts

    if args.dry_run:
        counts["attempted"] = len(lots)
        counts["sells"] = sum(1 for lot in lots if lot["side"] == "SELL")
        print(
            f"{base} | DRY RUN: {len(lots)} lot(s) ({counts['sells']} SELL) and "
            f"{len(snaps) if ts_utc else 0} snapshot(s) would be written; {rejected} row(s) rejected"
        )
        return counts

    written = _write_file(conn, lots, snaps, ts_utc, fp, args)
    written["rejected"] += rejected
    print(
        f"{base} | lots: {written['inserted']} inserted, {written['duplicate']} already present"
        f" ({written['sells']} SELL) | snapshots: {written['snaps_inserted']} inserted,"
        f" {written['snaps_duplicate']} already present | rejected={written['rejected']}"
        f" oversells={written['oversells']}"
        f" ts_utc={ts_utc.isoformat() if ts_utc else 'N/A'}"
    )
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--pattern", default="*.csv")
    ap.add_argument("--default-account", required=True,
                    help="Account for lots whose Comment doesn't name a tagged account")
    ap.add_argument("--tagged-accounts", default="",
                    help="Comma-separated account names detected in the Comment column (e.g. IBKR,BrokerB)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate and report; write nothing")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="Import the rows that pass and skip the ones that do not (exit 2 if any "
                         "were skipped). Default: a file with a bad row is not written at all.")
    args = ap.parse_args()
    tagged_accounts = [t.strip() for t in args.tagged_accounts.split(",") if t.strip()]

    base_dir = Path(args.dir).resolve()
    if not base_dir.is_dir():
        print(f"--dir is not a directory: {base_dir}")
        return EXIT_FAILED

    try:
        files, escaped = contained_matches(base_dir, args.pattern)
    except ValueError as exc:
        print(f"Refusing to run: {exc}")
        return EXIT_FAILED
    if escaped:
        print(f"Ignored {escaped} match(es) resolving outside {base_dir}.")
    if not files:
        print("No files found")
        return EXIT_FAILED

    cfg = load_config()

    totals = _empty_counts()
    totals["files"] = 0
    # A plain connection, committed per file by _write_file. Not `with connect()
    # as conn`, whose exit would commit whatever a file left half-done.
    conn = connect(cfg)
    try:
        for fp in files:
            base = os.path.basename(fp).lower()
            # Skip the moving target file and any temp files
            if base == 'latest.csv' or base.endswith('_tmp.csv'):
                continue
            counts = _import_file(conn, fp, args, tagged_accounts)
            if counts is None:
                continue
            totals["files"] += 1
            for k, v in counts.items():
                totals[k] += v
    finally:
        conn.close()

    print("-")
    print(f"Files processed: {totals['files']}")
    print(f"Lots attempted: {totals['attempted']} — {totals['inserted']} inserted "
          f"({totals['inserted'] - totals['sells']} BUY, {totals['sells']} SELL), "
          f"{totals['duplicate']} already present")
    print(f"Snapshots: {totals['snaps_inserted']} inserted, {totals['snaps_duplicate']} already present")
    print(f"Rows rejected: {totals['rejected']}")
    if args.dry_run:
        print("DRY RUN: nothing was written")
    if totals["rejected"]:
        return EXIT_PARTIAL if args.continue_on_error else EXIT_FAILED
    return EXIT_OK


if __name__ == '__main__':
    raise SystemExit(main())
