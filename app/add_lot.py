"""Add a trade lot into PortfolioDB.

Usage:
  set PORTFOLIODB_PASSWORD=... (in your shell)
  python add_lot.py --symbol NVDA --account IBKR --trade-date 2026-02-13 --qty 1 --price 184.00 --fees 0 --notes "test"

Registers the instrument if it is new and inserts the lot, in one transaction.

Exit status:
  0  the lot was recorded
  1  nothing was written: an identical lot is already in the ledger (the dedupe
     guard refused it — see the message for what to do about a second fill)
  2  bad input (argparse — including the reserved account name '(none)'), or
     refused: a non-USD listing without --allow-non-usd
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import account_names
import ledger_inputs
import ledger_numbers
import ledger_writes
import oversell
import symbol_paths
from db import load_config, transaction

EXIT_OK = 0
EXIT_DUPLICATE = 1
EXIT_REFUSED = 2


def _plain(d) -> str:
    return format(d.normalize(), "f")


def record_lot(conn, *, symbol, account, side, trade_date, qty, price, fees, notes,
               allow_non_usd: bool = False, out=None, err=None) -> int:
    """Validate and insert one lot on ``conn`` (no commit); returns the exit code.

    Shared by add_lot and sell_lot so the two cannot drift: same USD-scope
    check, same oversell warning, same duplicate report.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    if account_names.is_reserved(account):
        print(f"REFUSED: {account_names.refusal(account)}", file=err)
        return EXIT_REFUSED
    problem = ledger_writes.currency_scope_problem(conn, symbol)
    if problem:
        if not allow_non_usd:
            print(ledger_writes.usd_scope_refusal(problem), file=err)
            return EXIT_REFUSED
        print(f"WARNING: {problem}; recorded anyway (--allow-non-usd). It will be "
              "summed as if priced in dollars.", file=err)

    if side == "SELL":
        # A sale bigger than the position is usually the wrong account or
        # date; say so now, and still record it (see app/oversell.py).
        warning = oversell.oversell_warning(conn, symbol, account, trade_date, qty)
        if warning:
            print(warning, file=err)

    ledger_writes.ensure_instrument(conn, symbol)
    inserted = ledger_writes.insert_lot(
        conn, symbol=symbol, account=account, side=side, trade_date=trade_date,
        quantity=qty, price=price, fees=fees, notes=notes,
    )
    if not inserted:
        print(ledger_writes.duplicate_lot_message(
            symbol=symbol, account=account, side=side, trade_date=trade_date,
            quantity=qty, price=price,
        ), file=err)
        return EXIT_DUPLICATE

    held = ledger_inputs.open_quantities(conn).get(symbol)
    print(f"OK recorded {side} {_plain(qty)} {symbol} @ {_plain(price)} on {trade_date}"
          f"{' in ' + account if account else ''}; open quantity now "
          f"{_plain(held) if held is not None else '0'}", file=out)
    return EXIT_OK


def build_parser(*, side_choice: bool = True) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    # Validated and uppercased here: a symbol is rendered into the dashboard
    # and becomes a file name, so it must look like one (audit A-3/D-2).
    ap.add_argument("--symbol", required=True, type=symbol_paths.symbol_arg)
    # Stripped, blank → no account; '(none)' — the label for "no account" —
    # is refused as a name, exit 2 (account_names; rescore NEW-1).
    ap.add_argument("--account", default=None, type=account_names.account_arg)
    ap.add_argument("--trade-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    if side_choice:
        ap.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    # Finite, in range, right sign, at most 8 decimals — as Decimal, exactly
    # as typed. float() accepted NaN and infinity, and NaN passed every later
    # check.
    ap.add_argument("--qty", type=ledger_numbers.quantity_arg, required=True)
    ap.add_argument("--price", type=ledger_numbers.price_arg, required=True)
    ap.add_argument("--fees", type=ledger_numbers.fees_arg, default=ledger_numbers.parse_fees("0"))
    ap.add_argument("--notes", default=None)
    ap.add_argument("--allow-non-usd", action="store_true",
                    help="Record a lot in a non-US listing anyway (the ledger is USD-only).")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # "" is not "no account": the engines would treat it as a separate stream.
    account = (args.account or "").strip() or None

    cfg = load_config()
    # transaction(): instrument upsert + lot insert commit together, and the
    # connection is actually closed — `with connect()` does neither.
    with transaction(cfg) as conn:
        return record_lot(
            conn, symbol=args.symbol, account=account, side=args.side,
            trade_date=args.trade_date, qty=args.qty, price=args.price,
            fees=args.fees, notes=args.notes, allow_non_usd=args.allow_non_usd,
        )


if __name__ == "__main__":
    raise SystemExit(main())
