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
import portfolio_kpis as kpis
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
    # Held symbols valued at a quote from an earlier day (audit F-8).
    stale: list[dict] = field(default_factory=list)
    # Money recorded in a currency other than USD (audit F-11 / Q-8).
    currency_warnings: list[dict] = field(default_factory=list)
    # Each account's latest cash entry: {account, cash, ts, currency}.
    cash_rows: list[dict] = field(default_factory=list)


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


def movers(positions: pd.DataFrame, eod_by_day, today: date, stale: set[str] = frozenset()) -> dict:
    """Top and bottom movers over the day, the week and all time.

    The day and the week are measured from the held symbols' own sessions
    (held_session_days), like the totals; a stale symbol has no observed move
    today and is left out of the day's list (audit F-8).
    """
    if positions.empty or not eod_by_day:
        return {}
    sorted_days = held_session_days(eod_by_day, held_symbols(positions)) or sorted(eod_by_day.keys())
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
        pp = prior_prices.get(sym) if sym not in stale else None
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
    """The FIFO frame with each symbol's latest quote and what follows from it.

    An unpriced row keeps NaN market value, P&L and P&L % — unknown, which the
    renderer prints as "—", not +0.00%.
    """
    if fifo_pos.empty:
        return pd.DataFrame(columns=POSITION_COLUMNS)
    positions = fifo_pos.copy()
    positions["last_price"] = pd.to_numeric(
        positions["symbol"].map(lambda s: latest.get(s, {}).get("last_price")), errors="coerce"
    )
    positions["market_value"] = positions["qty"] * positions["last_price"]
    positions["unrealized_pnl"] = positions["market_value"] - positions["open_cost"]
    oc = positions["open_cost"].where(positions["open_cost"] != 0)
    pct = positions["unrealized_pnl"] / oc * 100
    # A zero-cost row (fully closed) is 0%; an unpriced one stays unknown.
    positions["unrealized_pct"] = pct.where(oc.notna() | positions["last_price"].isna(), 0.0).astype(float)
    return positions


def held_symbols(positions: pd.DataFrame) -> list[str]:
    return positions[positions["qty"] > 0]["symbol"].tolist() if not positions.empty else []


def report_as_of(latest: dict[str, dict], tz: ZoneInfo, held: list[str] = (),
                 benchmarks: set[str] = frozenset()) -> datetime:
    """When the holdings were last priced (portfolio_kpis.portfolio_as_of), in
    the reporting zone; now when nothing was ever priced."""
    ts = kpis.portfolio_as_of({s: v.get("ts") for s, v in latest.items()}, held, benchmarks)
    return ts.astimezone(tz) if ts is not None else datetime.now(tz)


def held_session_days(eod_by_day, held: list[str]) -> list[date]:
    """The days on which at least one held symbol was quoted — the sessions a
    day change can be measured between. A day that only a benchmark traded on
    (futures on a Sunday) is not a session of this portfolio (audit F-8)."""
    held_set = set(held)
    return sorted(d for d, quotes in eod_by_day.items() if held_set & set(quotes))


def compute_totals(
    positions: pd.DataFrame,
    cash_by_account: dict[str, float],
    eod_by_day,
    today: date,
    lot_rows: list[dict] = (),
    income_total=0,
    stale_symbols: set[str] = frozenset(),
) -> dict:
    """Portfolio roll-ups. Also writes each position's `weight` into `positions`.

    Value, cost and P&L come from portfolio_kpis (the dashboard's and the
    Advisor's definitions): unpriced positions are reported, not counted as
    losses, and total return is over peak net invested capital. The day change
    is measured from the previous session of the HELD symbols, over the symbols
    quoted today; a stale symbol has no observed move and is left out.
    """
    held = positions[positions["qty"] > 0]
    rows = [
        {"symbol": r["symbol"], "qty": r["qty"], "open_cost": r["open_cost"],
         "realized_pnl": r["realized_pnl"],
         "last_price": r["last_price"] if pd.notna(r["last_price"]) else None}
        for _, r in positions.iterrows()
    ]
    s = kpis.summary(rows, lot_rows, income=kpis.to_dec(income_total) or kpis.ZERO)
    total_value = float(s["market_value"])
    cash_total = sum(cash_by_account.values())

    if not positions.empty:
        # NaN for an unpriced row (no market value to weigh), 0 when nothing
        # held is priced.
        mv = positions["market_value"]
        positions["weight"] = (mv / total_value * 100) if total_value else mv * 0.0

    held_syms = held["symbol"].tolist()
    sessions = held_session_days(eod_by_day, held_syms)
    prior_day = next((d for d in reversed(sessions) if d < today), None)
    week_day = next((d for d in reversed(sessions) if d <= today - timedelta(days=7)), None)
    prior_prices = carry_forward_prices_at(eod_by_day, prior_day) if prior_day else {}
    week_prices = carry_forward_prices_at(eod_by_day, week_day) if week_day else {}

    def _portfolio_delta(prices: dict, skip: set[str]) -> tuple[float | None, float | None]:
        if not prices or held.empty:
            return None, None
        d_abs, prev_v, seen = 0.0, 0.0, False
        for _, r in held.iterrows():
            if r["symbol"] in skip:
                continue
            pp = prices.get(r["symbol"])
            last = float(r["last_price"]) if pd.notna(r["last_price"]) else None
            if pp is not None and last is not None:
                qty = float(r["qty"])
                d_abs += qty * (last - pp)
                prev_v += qty * pp
                seen = True
        if not seen:
            return None, None
        return d_abs, (d_abs / prev_v * 100) if prev_v else None

    day_chg, day_pct = _portfolio_delta(prior_prices, set(stale_symbols))
    week_chg, week_pct = _portfolio_delta(week_prices, set())

    return {
        "value": total_value,
        "cost": float(s["cost_basis"]),
        "priced_cost": float(s["priced_cost_basis"]),
        "unpriced_cost": float(s["unpriced_cost_basis"]),
        "unpriced_symbols": s["unpriced_symbols"],
        "unrl": float(s["unrealized_pnl"]),
        "unrl_pct": kpis.as_float(s["unrealized_pct"]),
        "realized": float(s["realized_pnl"]),
        "invested": float(s["invested_capital"]),
        "total_return_pct": kpis.as_float(s["total_return_pct"]),
        "total_return_with_income_pct": kpis.as_float(s["total_return_with_income_pct"]),
        "total_return_partial": s["total_return_partial"],
        "income": float(kpis.to_dec(income_total) or 0),
        "cash": cash_total, "aum": total_value + cash_total,
        "day_chg": day_chg, "day_pct": day_pct, "prior_day": prior_day,
        "week_chg": week_chg, "week_pct": week_pct, "week_day": week_day,
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
