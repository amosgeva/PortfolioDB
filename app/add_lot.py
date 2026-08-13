"""Add a trade lot into PortfolioDB.

Usage:
  set PORTFOLIODB_PASSWORD=... (in your shell)
  python add_lot.py --symbol NVDA --account IBKR --trade-date 2026-02-13 --qty 1 --price 184.00 --fees 0 --notes "test"

This will upsert the instrument row and insert the lot (deduped by unique index).
"""

from __future__ import annotations

import argparse

from db import fetch_all, load_config, run, transaction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--account", default=None)
    ap.add_argument("--trade-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    ap.add_argument("--qty", type=float, required=True)
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--fees", type=float, default=0.0)
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()

    symbol = args.symbol.strip().upper()

    cfg = load_config()
    # transaction(): instrument upsert + lot insert commit together (and the
    # connection is actually closed — `with connect()` does neither).
    with transaction(cfg) as conn:
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
