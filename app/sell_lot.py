"""Convenience wrapper to add a SELL lot.

Usage:
  set PORTFOLIODB_PASSWORD=...
  python sell_lot.py --symbol APP --account IBKR --trade-date 2026-02-24 --qty 1 --price 440 --fees 2.5

It inserts the SELL lot and prints updated FIFO position for that symbol.
"""

from __future__ import annotations

import argparse

from db import load_config, run, transaction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--account", default=None)
    ap.add_argument("--trade-date", required=True)
    ap.add_argument("--qty", type=float, required=True)
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--fees", type=float, default=0.0)
    ap.add_argument("--notes", default=None)
    args = ap.parse_args()

    sym = args.symbol.strip().upper()

    cfg = load_config()
    with transaction(cfg) as conn:
        run(conn, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING", (sym,))
        run(
            conn,
            """
            INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees, notes)
            VALUES (%s, %s, 'SELL', %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (sym, args.account, args.trade_date, args.qty, args.price, args.fees, args.notes),
        )

    # Print updated positions for this symbol. Do NOT call positions.main() —
    # it re-parses sys.argv and dies on this script's flags.
    from positions import print_positions
    print_positions(sym)


if __name__ == "__main__":
    main()
