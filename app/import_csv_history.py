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
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from db import connect, execute, load_config


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


def parse_quantity(raw: str) -> float:
    """Quantity is always positive; `side` carries the direction.

    A negative quantity is how several brokers encode a sale, but guessing that
    is not safe: it is also how some encode a short, an option assignment or a
    corrective entry. Refusing costs the reader one column; guessing wrong
    corrupts a cost basis and shows up months later as an inexplicable P&L.
    """
    qty = float(raw)
    if qty < 0:
        raise ValueError(
            f"negative quantity {qty:g}. Some brokers encode a sale this way — "
            "if that is what this row is, add a Side column with SELL and make "
            "the quantity positive. This importer will not guess."
        )
    if qty == 0:
        raise ValueError("quantity is 0")
    return qty


def to_float(val: str | None) -> float | None:
    if val is None:
        return None
    v = str(val).strip()
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None


def upsert_instrument(conn, symbol: str):
    execute(
        conn,
        "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
        (symbol,),
    )


def insert_lot(conn, symbol: str, account: str | None, trade_date, qty: float, price: float, fees: float, notes: str | None, dry_run: bool, side: str = "BUY"):
    if dry_run:
        return
    execute(
        conn,
        """
        INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (symbol, account, side, trade_date, qty, price, fees, notes),
    )


def insert_snapshot(conn, ts_utc: datetime, symbol: str, last_price: float, dry_run: bool):
    if dry_run:
        return
    execute(
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--pattern", default="*.csv")
    ap.add_argument("--default-account", required=True,
                    help="Account for lots whose Comment doesn't name a tagged account")
    ap.add_argument("--tagged-accounts", default="",
                    help="Comma-separated account names detected in the Comment column (e.g. IBKR,BrokerB)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    tagged_accounts = [t.strip() for t in args.tagged_accounts.split(",") if t.strip()]

    base_dir = Path(args.dir).resolve()
    if not base_dir.is_dir():
        print(f"--dir is not a directory: {base_dir}")
        return

    try:
        files, escaped = contained_matches(base_dir, args.pattern)
    except ValueError as exc:
        print(f"Refusing to run: {exc}")
        return
    if escaped:
        print(f"Ignored {escaped} match(es) resolving outside {base_dir}.")
    if not files:
        print("No files found")
        return

    cfg = load_config()

    total_files = 0
    total_lots = 0
    total_sells = 0
    total_snaps = 0
    total_errors = 0

    with connect(cfg) as conn:
        for fp in files:
            base = os.path.basename(fp).lower()
            # Skip the moving target file and any temp files
            if base == 'latest.csv' or base.endswith('_tmp.csv'):
                continue

            with open(fp, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            if not rows:
                continue

            # Find first row that has Date+Time
            date_str = None
            time_str = None
            for r in rows:
                if r.get('Date') and r.get('Time'):
                    date_str = r['Date']
                    time_str = r['Time']
                    break

            if not date_str or not time_str:
                # Can't create snapshot ts; skip snapshot import
                ts_utc = None
            else:
                try:
                    ts_utc = parse_snapshot_ts(date_str, time_str)
                except Exception:
                    ts_utc = None

            file_lots = 0
            file_sells = 0
            file_snaps = 0
            file_errors = 0

            for r in rows:
                sym = (r.get('Symbol') or '').strip().upper()
                if not sym:
                    continue

                # Must respect --dry-run too: this was the one write that
                # used to slip through and mutate `instruments` on a dry run.
                if not args.dry_run:
                    upsert_instrument(conn, sym)

                # Lots
                trade_date_raw = (r.get('Trade Date') or '').strip()
                purchase_raw = (r.get('Purchase Price') or '').strip()
                qty_raw = (r.get('Quantity') or '').strip()
                comm_raw = (r.get('Commission') or '').strip()
                comment = r.get('Comment')

                if trade_date_raw and purchase_raw and qty_raw:
                    try:
                        td = parse_trade_date(trade_date_raw)
                        side = parse_side(r.get('Side'))
                        qty = parse_quantity(qty_raw)
                        price = float(purchase_raw)
                        fees = float(comm_raw) if comm_raw else 0.0
                        account = infer_account(comment, args.default_account, tagged_accounts)
                        insert_lot(conn, sym, account, td, qty, price, fees, comment,
                                   args.dry_run, side=side)
                        file_lots += 1
                        if side == "SELL":
                            file_sells += 1
                    except Exception as e:
                        file_errors += 1
                        print(
                            f"ERROR {os.path.basename(fp)} | {sym} | "
                            f"trade_date={trade_date_raw!r} qty={qty_raw!r} price={purchase_raw!r}: {e}"
                        )

                # Snapshots (last price)
                if ts_utc is not None:
                    cp = to_float(r.get('Current Price'))
                    if cp is not None:
                        insert_snapshot(conn, ts_utc, sym, cp, args.dry_run)
                        file_snaps += 1

            total_files += 1
            total_lots += file_lots
            total_sells += file_sells
            total_snaps += file_snaps
            total_errors += file_errors

            print(
                f"{os.path.basename(fp)} | lots={file_lots} (sells={file_sells}) snaps={file_snaps}"
                f" errors={file_errors} ts_utc={ts_utc.isoformat() if ts_utc else 'N/A'}"
            )

    print("-")
    print(f"Files processed: {total_files}")
    print(f"Lots inserted (attempted): {total_lots} — {total_lots - total_sells} BUY, {total_sells} SELL")
    print(f"Snapshots inserted (attempted): {total_snaps}")
    print(f"Rows failed: {total_errors}")
    if args.dry_run:
        print("DRY RUN: no inserts were committed")


if __name__ == '__main__':
    main()
