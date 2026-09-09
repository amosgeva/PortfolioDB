"""Add a trade lot into PortfolioDB.

Usage:
  set PORTFOLIODB_PASSWORD=... (in your shell)
  python add_lot.py --symbol NVDA --account IBKR --trade-date 2026-02-13 --qty 1 --price 184.00 --fees 0 --notes "test"

This will upsert the instrument row and insert the lot (deduped by unique index).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import ledger_numbers
import oversell
from db import fetch_all, load_config, run, transaction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--account", default=None)
    ap.add_argument("--trade-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    # Finite, in range, right sign — as Decimal, exactly as typed. float()
    # accepted NaN and infinity, and NaN passed every later check.
    ap.add_argument("--qty", type=ledger_numbers.quantity_arg, required=True)
    ap.add_argument("--price", type=ledger_numbers.price_arg, required=True)
    ap.add_argument("--fees", type=ledger_numbers.fees_arg, default=ledger_numbers.parse_fees("0"))
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()

    symbol = args.symbol.strip().upper()

    cfg = load_config()
    # transaction(): instrument upsert + lot insert commit together (and the
    # connection is actually closed — `with connect()` does neither).
    with transaction(cfg) as conn:
        if args.side == "SELL":
            # A sale bigger than the position is usually the wrong account or
            # date; say so now, and still record it (see app/oversell.py).
            warning = oversell.oversell_warning(
                conn, symbol, args.account, date.fromisoformat(args.trade_date), args.qty
            )
            if warning:
                print(warning, file=sys.stderr)

        run(
            conn,
            """
            INSERT INTO instruments(symbol) VALUES (%s)
            ON CONFLICT(symbol) DO NOTHING
            """,
            (symbol,),
        )

        run(
            conn,
            """
            INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (symbol, args.account, args.side, args.trade_date, args.qty, args.price, args.fees, args.notes),
        )

        pos = fetch_all(
            conn,
            """
            SELECT symbol,
                   SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS qty
            FROM lots
            WHERE symbol=%s
            GROUP BY symbol
            """,
            (symbol,),
        )

    print("OK", pos[0] if pos else {"symbol": symbol, "qty": 0})


if __name__ == "__main__":
    main()
