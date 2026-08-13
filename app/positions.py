"""Compute current positions (FIFO) from PortfolioDB lots.

Usage:
  set PORTFOLIODB_PASSWORD=...
  python positions.py
  python positions.py --symbol NVDA

Outputs:
- Per (symbol, account): open qty, open cost basis, realized pnl
- Merged per symbol totals
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from decimal import Decimal

from db import connect, fetch_all, load_config
from fifo import Lot, run_fifo
from portfolio import compute_fifo_merged

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def to_decimal(x) -> Decimal:
    return Decimal(str(x))


def print_positions(symbol: str | None = None) -> None:
    """Print per-account and merged FIFO positions, optionally for one symbol.

    Callable directly (e.g. from sell_lot.py) — must not re-parse sys.argv.
    """
    sym = symbol.upper() if symbol else None

    cfg = load_config()

    with connect(cfg) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT id, symbol, account, side, trade_date, quantity, price, fees
            FROM lots
            WHERE (%s IS NULL OR symbol = %s)
            ORDER BY symbol, COALESCE(account,''), trade_date, id
            """,
            (sym, sym),
        )

    # Per-account view using raw FIFO
    grouped: dict[tuple[str, str | None], list[Lot]] = defaultdict(list)
    for r in rows:
        grouped[(r["symbol"], r["account"])].append(
            Lot(
                id=int(r["id"]),
                symbol=r["symbol"],
                account=r["account"],
                side=r["side"],
                trade_date=r["trade_date"],
                quantity=to_decimal(r["quantity"]),
                price=to_decimal(r["price"]),
                fees=to_decimal(r["fees"]),
            )
        )

    lines = []
    for (sym, acct), lots in grouped.items():
        res = run_fifo(lots)
        lines.append((sym, acct or "(none)", res.open_qty, res.open_cost, res.realized_pnl))

    lines.sort(key=lambda x: (x[0], x[1]))
    log.info("PER-ACCOUNT POSITIONS (FIFO)")
    for sym, acct, qty, cost, realized in lines:
        if qty == 0 and realized == 0:
            continue
        avg_cost = (cost / qty) if qty != 0 else Decimal("0")
        log.info(f"{sym:6} | {acct:6} | qty={qty} | open_cost=${cost:.2f} | avg_cost=${avg_cost:.4f} | realized=${realized:.2f}")

    # Merged view via shared module
    merged_df = compute_fifo_merged(rows)
    log.info("MERGED POSITIONS")
    for _, row in merged_df.iterrows():
        if row["qty"] == 0 and row["realized_pnl"] == 0:
            continue
        log.info(
            f"{row['symbol']:6} | qty={row['qty']} | open_cost=${row['open_cost']:.2f} "
            f"| avg_cost=${row['avg_cost']:.4f} | realized=${row['realized_pnl']:.2f}"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="optional filter")
    args = ap.parse_args()
    print_positions(args.symbol)


if __name__ == "__main__":
    main()
