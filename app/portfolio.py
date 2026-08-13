"""Shared portfolio computation: FIFO merge across (symbol, account) groups.

Used by streamlit_app.py, positions.py, and report_portfolio_db.py.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import pandas as pd

from fifo import Lot, run_fifo
from avg_cost import Lot as AvgLot, run_avg_cost

_COLUMNS = ["symbol", "qty", "open_cost", "avg_cost", "realized_pnl"]


def to_decimal(x) -> Decimal:
    return Decimal(str(x))


def _compute_merged(lot_rows: list[dict], lot_cls, engine) -> pd.DataFrame:
    """Group lots by (symbol, account), run `engine` per group, merge per
    symbol. Both engines share the Lot field set and return results with
    open_qty / open_cost / realized_pnl, so the merge is identical.

    Returns a DataFrame with columns:
      symbol, qty, open_cost, avg_cost, realized_pnl
    """
    grouped: dict[tuple[str, str | None], list] = defaultdict(list)
    for r in lot_rows:
        grouped[(r["symbol"], r["account"])].append(
            lot_cls(
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

    merged: dict[str, dict] = {}
    for (sym, _acct), lots in grouped.items():
        res = engine(lots)
        m = merged.get(sym)
        if not m:
            merged[sym] = {
                "symbol": sym,
                "qty": res.open_qty,
                "open_cost": res.open_cost,
                "realized_pnl": res.realized_pnl,
            }
        else:
            m["qty"] += res.open_qty
            m["open_cost"] += res.open_cost
            m["realized_pnl"] += res.realized_pnl

    out = []
    for sym, m in merged.items():
        qty = m["qty"]
        open_cost = m["open_cost"]
        realized = m["realized_pnl"]
        avg_cost = (open_cost / qty) if qty != 0 else Decimal("0")
        out.append(
            {
                "symbol": sym,
                "qty": float(qty),
                "open_cost": float(open_cost),
                "avg_cost": float(avg_cost),
                "realized_pnl": float(realized),
            }
        )

    if out:
        return pd.DataFrame(out).sort_values("symbol").reset_index(drop=True)
    return pd.DataFrame(columns=_COLUMNS)


def compute_fifo_merged(lot_rows: list[dict]) -> pd.DataFrame:
    """FIFO per (symbol, account), merged per symbol."""
    return _compute_merged(lot_rows, Lot, run_fifo)


def compute_avg_cost_merged(lot_rows: list[dict]) -> pd.DataFrame:
    """Moving average-cost per (symbol, account), merged per symbol."""
    return _compute_merged(lot_rows, AvgLot, run_avg_cost)
