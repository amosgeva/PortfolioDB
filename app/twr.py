"""Time-weighted return (TWR) — pure, DB-free.

The dashboard's old "returns" strip multiplied *today's* share counts by
historical prices, which back-projects current winners onto the past and
ignores contributions — wildly overstating performance. TWR fixes that: it
reconstructs the *historical* holdings on each snapshot day and chains daily
sub-period returns, so the size/timing of deposits and trades is neutralised.

Sub-period return (start-weighted flows):

    r_i = (MV_i + div_i) / (MV_{i-1} + flow_i) - 1

  MV_i   market value of the holdings actually held on day i
  flow_i net external cash invested that day (BUY cost +, SELL proceeds -),
         i.e. money moving in/out of the securities, NOT a return
  div_i  dividends/income earned that day (part of the return)

The cumulative growth curve G is G_i = G_{i-1}·(1+r_i), starting at 1 on the
first day with positive market value. A period's TWR is G_end / G_base - 1.

Inputs are plain structures so the MCP service, the Streamlit dashboard, and
the reports can all feed it from their own DB connections.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import holdings

PERIODS = ("1D", "WTD", "MTD", "YTD", "1Y", "MAX")


def build_daily_records(
    lots: list[dict[str, Any]],
    price_by_day: dict[date, dict[str, float]],
    dividends: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per-snapshot-day records using *historical* holdings.

    Args:
        lots: {symbol, side ('BUY'/'SELL'), trade_date (date), quantity,
            price, fees} — all numbers floats.
        price_by_day: {day: {symbol: last_price_that_day}}. Prices may be
            sparse; the last known price per symbol is carried forward.
        dividends: {pay_date (date), amount}.

    Returns sorted [{day, mv, flow, div}] where mv = market value of holdings
    held that day, flow = net external cash invested that day, div = income.
    """
    days = sorted(price_by_day.keys())
    if not days:
        return []

    def _bucket(event_date: date) -> date | None:
        # Attribute an event to the first snapshot day on/after it, so a trade
        # between snapshots lands on the following snapshot.
        for d in days:
            if d >= event_date:
                return d
        return None  # after the last snapshot — not yet valuable, ignore

    flow_by_day = {d: 0.0 for d in days}
    for lot in lots:
        d = _bucket(lot["trade_date"])
        if d is None:
            continue
        gross = lot["quantity"] * lot["price"]
        if str(lot["side"]).upper() == "BUY":
            flow_by_day[d] += gross + lot["fees"]
        else:
            flow_by_day[d] -= gross - lot["fees"]

    div_by_day = {d: 0.0 for d in days}
    for dv in dividends:
        d = _bucket(dv["pay_date"])
        if d is None:
            continue
        div_by_day[d] += dv["amount"]

    # Holdings reconstruction lives in `holdings` so the drawdown and value-history
    # readers share this exact logic instead of re-deriving it (they used to hold
    # today's quantities constant, which back-projects current positions).
    valued = holdings.value_series(
        lots, [(d, price_by_day[d]) for d in days], carry_forward=True
    )
    return [
        {"day": d, "mv": mv, "flow": flow_by_day[d], "div": div_by_day[d]}
        for d, mv in valued
    ]


def benchmark_records(
    price_by_day: dict[date, dict[str, float]], symbol: str
) -> list[dict[str, Any]]:
    """Records for a benchmark held flat (no flows/divs) → TWR == price return."""
    out: list[dict[str, Any]] = []
    last: float | None = None
    for d in sorted(price_by_day.keys()):
        p = price_by_day[d].get(symbol, last)
        last = p
        if p is None:
            continue
        out.append({"day": d, "mv": p, "flow": 0.0, "div": 0.0})
    return out


def growth_curve(records: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """Cumulative growth factor per day, starting at 1.0 on the first day with
    positive market value. Days before that are dropped."""
    out: list[tuple[date, float]] = []
    g = 1.0
    prev_mv: float | None = None
    for rec in records:
        mv = rec["mv"]
        if prev_mv is None:
            if mv > 0:
                prev_mv = mv
                out.append((rec["day"], g))
            continue
        denom = prev_mv + rec.get("flow", 0.0)
        if denom > 0:
            g *= (mv + rec.get("div", 0.0)) / denom
        out.append((rec["day"], g))
        prev_mv = mv
    return out


def period_returns(records: list[dict[str, Any]], today: date) -> dict[str, float | None]:
    """TWR % for each period in PERIODS, read off the growth curve."""
    g = growth_curve(records)
    if len(g) < 2:
        return {p: None for p in PERIODS}

    _end_day, end_g = g[-1]
    _first_day, first_g = g[0]

    def base_g(boundary: date, strict: bool) -> float | None:
        cand = [gf for (d, gf) in g if (d < boundary if strict else d <= boundary)]
        return cand[-1] if cand else None

    def pct(bg: float | None) -> float | None:
        return round((end_g / bg - 1.0) * 100.0, 2) if bg else None

    wk = today - timedelta(days=today.weekday())
    yr = base_g(today - timedelta(days=365), False)
    return {
        "1D": pct(base_g(today, True)),
        "WTD": pct(base_g(wk, True)),
        "MTD": pct(base_g(today.replace(day=1), True)),
        "YTD": pct(base_g(today.replace(month=1, day=1), True)),
        "1Y": pct(yr if yr is not None else first_g),
        "MAX": pct(first_g),
    }
