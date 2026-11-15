"""Analytics for the executive report — pure functions over prepared rows.

No SQL and no HTML: everything here takes the restated lots, quotes and cash
that `exec_report.gather()` fetched and returns frames, dicts and the
`ReportData` the renderer consumes. Split out of `exec_report.py` (audit R-4);
`exec_report` re-exports these under their old underscore names.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import ledger_inputs
from fifo import Lot, run_fifo
from portfolio import to_decimal


POSITION_COLUMNS = [
    "symbol", "qty", "open_cost", "avg_cost", "realized_pnl",
    "last_price", "market_value", "unrealized_pnl", "unrealized_pct", "weight",
]


@dataclass
class ReportData:
    as_of: datetime
    positions: pd.DataFrame
    totals: dict
    cash_by_account: dict[str, float]
    account_allocation: pd.DataFrame
    equity_curve: pd.DataFrame
    movers: dict = field(default_factory=dict)
    realized_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    advisor_brief: dict | None = None
    fd_metrics: dict[str, dict] = field(default_factory=dict)
    earnings_past: list[dict] = field(default_factory=list)
    earnings_upcoming: list[dict] = field(default_factory=list)
    # The reporting timezone the report was gathered in, resolved at gather
    # time rather than at import (audit R-5: importing a report must not open a
    # database connection through the settings lookup).
    tz_name: str = "UTC"


def restate_latest(ledger: ledger_inputs.PreparedLedger, latest: dict[str, dict]) -> dict[str, dict]:
    """Latest quotes in the ledger's units.

    The lots come restated, so a quote must be too: a symbol whose newest
    snapshot predates a recorded split was valued at the pre-split price
    against post-split shares — twice its worth (1.7.2 re-audit, F03 case B).
    Each entry carries its own timestamp, which is what decides.
    """
    rows = ledger.price_rows(
        [{"symbol": s, "ts": v.get("ts"), "last_price": v.get("last_price")} for s, v in latest.items()]
    )
    return {r["symbol"]: {"ts": r["ts"], "last_price": r["last_price"]} for r in rows}


def holdings_at_date(lot_rows: list[dict], as_of: date) -> dict[str, float]:
    qty: dict[str, float] = defaultdict(float)
    for r in lot_rows:
        if r["trade_date"] > as_of:
            continue
        sign = 1 if r["side"].upper() == "BUY" else -1
        qty[r["symbol"]] += sign * float(r["quantity"])
    return {s: q for s, q in qty.items() if q > 1e-9}


def build_equity_curve(lot_rows, eod_by_day) -> pd.DataFrame:
    """Daily holdings market value, carrying forward last-known prices."""
    if not eod_by_day or not lot_rows:
        return pd.DataFrame(columns=["date", "value"])

    sorted_days = sorted(eod_by_day.keys())
    last_known: dict[str, float] = {}
    pts = []
    for d in sorted_days:
        last_known.update(eod_by_day[d])
        held = holdings_at_date(lot_rows, d)
        if not held:
            continue
        v = sum(q * last_known[s] for s, q in held.items() if s in last_known)
        if v > 0:
            pts.append({"date": d, "value": v})
    return pd.DataFrame(pts)


def carry_forward_prices_at(eod_by_day, target_day) -> dict[str, float]:
    """Last-known price per symbol as of end-of-target_day."""
    out: dict[str, float] = {}
    for d in sorted(eod_by_day.keys()):
        if d > target_day:
            break
        out.update(eod_by_day[d])
    return out


def movers(positions: pd.DataFrame, eod_by_day, today: date) -> dict:
    if positions.empty or not eod_by_day:
        return {}
    sorted_days = sorted(eod_by_day.keys())
    prior_day = next((d for d in reversed(sorted_days) if d < today), None)
    week_day = next((d for d in reversed(sorted_days) if d <= today - timedelta(days=7)), None)

    prior_prices = carry_forward_prices_at(eod_by_day, prior_day) if prior_day else {}
    week_prices = carry_forward_prices_at(eod_by_day, week_day) if week_day else {}

    held = positions[positions["qty"] > 0]
    rows_day, rows_week, rows_all = [], [], []
    for _, row in held.iterrows():
        sym = row["symbol"]
        qty = float(row["qty"])
        last = float(row["last_price"]) if pd.notna(row["last_price"]) else None
        if last is None:
            continue
        pp = prior_prices.get(sym)
        if pp:
            rows_day.append({"symbol": sym, "delta": qty * (last - pp), "pct": (last / pp - 1) * 100})
        wp = week_prices.get(sym)
        if wp:
            rows_week.append({"symbol": sym, "delta": qty * (last - wp), "pct": (last / wp - 1) * 100})
        rows_all.append({"symbol": sym, "delta": float(row["unrealized_pnl"]), "pct": float(row["unrealized_pct"])})

    def split(rs):
        s = sorted(rs, key=lambda x: x["delta"], reverse=True)
        return s[:5], list(reversed(s[-5:]))

    d_top, d_bot = split(rows_day)
    w_top, w_bot = split(rows_week)
    a_top, a_bot = split(rows_all)
    return {
        "day_top": d_top, "day_bot": d_bot,
        "week_top": w_top, "week_bot": w_bot,
        "all_top": a_top, "all_bot": a_bot,
    }


def realized_log(lot_rows: list[dict]) -> pd.DataFrame:
    grouped: dict[tuple[str, str | None], list[Lot]] = defaultdict(list)
    for r in lot_rows:
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
    sell_dates = {int(r["id"]): r["trade_date"] for r in lot_rows if r["side"].upper() == "SELL"}

    out = []
    for _, lots in grouped.items():
        res = run_fifo(lots)
        for m in res.matches:
            out.append({
                "sell_date": sell_dates.get(m.sell_lot_id),
                "symbol": m.symbol,
                "account": m.account or "",
                "qty": float(m.qty),
                "buy_cost_ps": float(m.buy_cost_ps),
                "sell_proceeds_ps": float(m.sell_proceeds_ps),
                "realized_pnl": float(m.realized_pnl),
            })
    if not out:
        return pd.DataFrame(columns=[
            "sell_date", "symbol", "account", "qty",
            "buy_cost_ps", "sell_proceeds_ps", "realized_pnl",
        ])
    return pd.DataFrame(out).sort_values(["sell_date", "symbol"], ascending=[False, True]).reset_index(drop=True)


def price_positions(fifo_pos: pd.DataFrame, latest: dict[str, dict]) -> pd.DataFrame:
    """The FIFO frame with each symbol's latest quote and what follows from it."""
    if fifo_pos.empty:
        return pd.DataFrame(columns=POSITION_COLUMNS)
    positions = fifo_pos.copy()
    positions["last_price"] = positions["symbol"].map(lambda s: latest.get(s, {}).get("last_price"))
    positions["market_value"] = positions["qty"] * positions["last_price"]
    positions["unrealized_pnl"] = positions["market_value"] - positions["open_cost"]
    oc = positions["open_cost"].replace(0, pd.NA)
    positions["unrealized_pct"] = (positions["unrealized_pnl"] / oc * 100).fillna(0.0).astype(float)
    return positions


def report_as_of(latest: dict[str, dict], tz: ZoneInfo) -> datetime:
    if latest:
        max_ts = max((v["ts"] for v in latest.values() if v.get("ts")), default=None)
        return max_ts.astimezone(tz) if max_ts else datetime.now(tz)
    return datetime.now(tz)


def compute_totals(positions: pd.DataFrame, cash_by_account: dict[str, float], eod_by_day, today: date) -> dict:
    """Portfolio roll-ups. Also writes each position's `weight` into `positions`."""
    held = positions[positions["qty"] > 0]
    total_value = float(held["market_value"].sum(skipna=True)) if not held.empty else 0.0
    total_cost = float(held["open_cost"].sum()) if not held.empty else 0.0
    total_unrl = float(held["unrealized_pnl"].sum(skipna=True)) if not held.empty else 0.0
    total_unrl_pct = (total_unrl / total_cost * 100) if total_cost else 0.0
    realized = float(positions["realized_pnl"].sum()) if not positions.empty else 0.0
    cash_total = sum(cash_by_account.values())

    if not positions.empty:
        positions["weight"] = (positions["market_value"] / total_value * 100).fillna(0.0) if total_value else 0.0

    prior_day = next((d for d in reversed(sorted(eod_by_day.keys())) if d < today), None)
    week_day = next((d for d in reversed(sorted(eod_by_day.keys())) if d <= today - timedelta(days=7)), None)
    prior_prices = carry_forward_prices_at(eod_by_day, prior_day) if prior_day else {}
    week_prices = carry_forward_prices_at(eod_by_day, week_day) if week_day else {}

    def _portfolio_delta(prices: dict) -> tuple[float, float]:
        if not prices or held.empty:
            return 0.0, 0.0
        d_abs, prev_v = 0.0, 0.0
        for _, r in held.iterrows():
            pp = prices.get(r["symbol"])
            last = float(r["last_price"]) if pd.notna(r["last_price"]) else None
            if pp is not None and last is not None:
                qty = float(r["qty"])
                d_abs += qty * (last - pp)
                prev_v += qty * pp
        return d_abs, (d_abs / prev_v * 100) if prev_v else 0.0

    day_chg, day_pct = _portfolio_delta(prior_prices)
    week_chg, week_pct = _portfolio_delta(week_prices)

    total_return_pct = ((realized + total_unrl) / total_cost * 100) if total_cost else 0.0
    return {
        "value": total_value, "cost": total_cost,
        "unrl": total_unrl, "unrl_pct": total_unrl_pct,
        "realized": realized, "total_return_pct": total_return_pct,
        "cash": cash_total, "aum": total_value + cash_total,
        "day_chg": day_chg, "day_pct": day_pct,
        "week_chg": week_chg, "week_pct": week_pct,
    }


def account_allocation(lot_rows: list[dict], latest: dict[str, dict], cash_by_account: dict[str, float]) -> pd.DataFrame:
    """Holdings value + cash, per account."""
    acct_alloc = pd.DataFrame()
    if lot_rows:
        per_acct: dict[tuple[str, str], float] = defaultdict(float)
        for r in lot_rows:
            sign = 1 if r["side"].upper() == "BUY" else -1
            acct = r["account"] or "(none)"
            per_acct[(acct, r["symbol"])] += sign * float(r["quantity"])
        acct_value: dict[str, float] = defaultdict(float)
        for (acct, sym), q in per_acct.items():
            if q <= 1e-9:
                continue
            p = latest.get(sym, {}).get("last_price")
            if p is not None:
                acct_value[acct] += q * p
        for acct, c in cash_by_account.items():
            acct_value[acct] += c
        if acct_value:
            acct_alloc = pd.DataFrame(
                [{"account": a, "value": v} for a, v in acct_value.items() if v > 0]
            ).sort_values("value", ascending=False).reset_index(drop=True)
            tot = acct_alloc["value"].sum()
            acct_alloc["weight"] = (acct_alloc["value"] / tot * 100) if tot else 0.0
    return acct_alloc
