"""Convenience wrapper to add a SELL lot.

Usage:
  set PORTFOLIODB_PASSWORD=...
  python sell_lot.py --symbol APP --account IBKR --trade-date 2026-02-24 --qty 1 --price 440 --fees 2.5

It inserts the SELL lot (warning first if it exceeds the position) and prints
the updated FIFO position for that symbol. Same checks and exit status as
add_lot.py: 0 recorded, 1 duplicate (nothing written), 2 bad input or refused.
"""

from __future__ import annotations

from add_lot import EXIT_OK, build_parser, record_lot
from db import load_config, transaction


def main(argv=None) -> int:
    args = build_parser(side_choice=False).parse_args(argv)
    account = (args.account or "").strip() or None

    cfg = load_config()
    with transaction(cfg) as conn:
        # The oversell warning is said now, at the keyboard, rather than as a
        # log line the FIFO engine writes at read time (see app/oversell.py).
        code = record_lot(
            conn, symbol=args.symbol, account=account, side="SELL",
            trade_date=args.trade_date, qty=args.qty, price=args.price,
            fees=args.fees, notes=args.notes, allow_non_usd=args.allow_non_usd,
        )
    if code != EXIT_OK:
        return code

    # Print updated positions for this symbol. Do NOT call positions.main() —
    # it re-parses sys.argv and dies on this script's flags.
    from positions import print_positions
    print_positions(args.symbol)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
