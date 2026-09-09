"""Time-weighted return (TWR) — pure, DB-free.

The dashboard's old "returns" strip multiplied *today's* share counts by
historical prices, which back-projects current winners onto the past and
ignores contributions — wildly overstating performance. TWR fixes that: it
reconstructs the *historical* holdings on each snapshot day and chains daily
sub-period returns, so a contribution is never a gain and a withdrawal never a
loss. Within a day, timing is approximated as described below.

Sub-period return — purchases weighted at the start of the day, sale proceeds
at the end:

    r_i = (MV_i + div_i + out_i) / (MV_{i-1} + in_i) - 1

  MV_i   market value of the holdings actually held at the end of day i
  in_i   cash that went INTO securities that day: BUY cost including fees
  out_i  cash that came OUT of securities that day: SELL proceeds net of fees
  div_i  dividends/income earned that day (part of the return)

Why the two flows are weighted differently. Trades carry a date, not a time,
so the day's return has to be approximated. A purchase is valued from its own
price to the close (it sits in the denominator); a sale is valued from the
previous close to its own price (its proceeds sit in the numerator). Both
directions count the move between the trade price and the neighbouring
snapshot as return, and neither counts the cash itself. The earlier form,
which netted a sale into the denominator as a negative flow, could not
represent a day on which the position was closed: selling one share bought
at 100 for 110 gave a denominator of 100 − 110 and the code either froze the
factor (0%) or, on a losing sale, multiplied it by zero (−100%, forever). It
also overstated any partial-sale day — a +10% day with half the position
sold read as +22% (audit F04, 2026-09-09).

The cumulative growth curve G is G_i = G_{i-1}·(1+r_i), starting at 1 on the
first day with positive market value. A period's TWR is G_end / G_base − 1.
A day on which no capital was at work — nothing held at the previous close
and nothing bought — carries the factor through unchanged, and a period made
only of such days reports None rather than a fictitious 0%. Income that
arrives while nothing is held (a dividend paid after the position was sold)
is credited against the last capital base that earned it.

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

    Returns sorted [{day, mv, inflow, outflow, flow, div}] where mv = market
    value of holdings held that day, inflow = BUY cost (fees included),
    outflow = SELL proceeds (fees deducted), flow = inflow − outflow for
    readers that only want the net, div = income.
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

    inflow_by_day = {d: 0.0 for d in days}
    outflow_by_day = {d: 0.0 for d in days}
    for lot in lots:
        d = _bucket(lot["trade_date"])
        if d is None:
            continue
        gross = lot["quantity"] * lot["price"]
        if str(lot["side"]).upper() == "BUY":
            inflow_by_day[d] += gross + lot["fees"]
        else:
            outflow_by_day[d] += gross - lot["fees"]

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
        {
            "day": d,
            "mv": mv,
            "inflow": inflow_by_day[d],
            "outflow": outflow_by_day[d],
            "flow": inflow_by_day[d] - outflow_by_day[d],
            "div": div_by_day[d],
        }
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


def _flows(rec: dict[str, Any]) -> tuple[float, float]:
    """(inflow, outflow) for a record.

    Records from build_daily_records carry both. A producer that only knows
    the net (`flow`) — benchmark_records, or anything written against the old
    shape — has it split by sign: money in is an inflow, money out an outflow.
    """
    if "inflow" in rec or "outflow" in rec:
        return float(rec.get("inflow", 0.0)), float(rec.get("outflow", 0.0))
    net = float(rec.get("flow", 0.0))
    return (net, 0.0) if net >= 0 else (0.0, -net)


def growth_curve(records: list[dict[str, Any]]) -> list[tuple[date, float]]:
    """Cumulative growth factor per day, starting at 1.0 on the first day with
    positive market value. Days before that are dropped.

    The denominator is the capital at work during the day: the previous
    close's value plus what was bought. It is zero only when nothing was held
    and nothing was bought, and on such a day the factor passes through
    unchanged — there was no capital to earn a return. Income that arrives on
    such a day is credited against the last base that had capital, because
    that is the capital that earned it.
    """
    out: list[tuple[date, float]] = []
    g = 1.0
    prev_mv: float | None = None
    last_base: float | None = None
    for rec in records:
        mv = rec["mv"]
        if prev_mv is None:
            if mv > 0:
                prev_mv = mv
                out.append((rec["day"], g))
            continue
        inflow, outflow = _flows(rec)
        div = rec.get("div", 0.0)
        denom = prev_mv + inflow
        if denom > 0:
            g *= (mv + div + outflow) / denom
            last_base = denom
        elif div and last_base:
            g *= 1.0 + div / last_base
        out.append((rec["day"], g))
        prev_mv = mv
    return out


def active_days(records: list[dict[str, Any]]) -> set[date]:
    """Days whose sub-period had capital at work (a positive denominator).

    The inception day is not one: its return is not computed. A period whose
    days are all inactive — everything sold, nothing bought since — has no
    return to report, and period_returns says None rather than 0%.
    """
    out: set[date] = set()
    prev_mv: float | None = None
    for rec in records:
        mv = rec["mv"]
        if prev_mv is None:
            if mv > 0:
                prev_mv = mv
            continue
        inflow, _ = _flows(rec)
        if prev_mv + inflow > 0:
            out.add(rec["day"])
        prev_mv = mv
    return out


def period_returns(records: list[dict[str, Any]], today: date) -> dict[str, float | None]:
    """TWR % for each period in PERIODS, read off the growth curve.

    None means "not available": no observation before the period's start, or
    no capital at work at any point inside it.
    """
    g = growth_curve(records)
    if len(g) < 2:
        return {p: None for p in PERIODS}

    active = active_days(records)
    _end_day, end_g = g[-1]
    first_day, first_g = g[0]

    def base(boundary: date, strict: bool) -> tuple[date, float] | None:
        cand = [(d, gf) for (d, gf) in g if (d < boundary if strict else d <= boundary)]
        return cand[-1] if cand else None

    def pct(b: tuple[date, float] | None) -> float | None:
        if not b or not b[1]:
            return None
        base_day, base_g = b
        if not any(d > base_day for d in active):
            return None
        return round((end_g / base_g - 1.0) * 100.0, 2)

    wk = today - timedelta(days=today.weekday())
    yr = base(today - timedelta(days=365), False)
    return {
        "1D": pct(base(today, True)),
        "WTD": pct(base(wk, True)),
        "MTD": pct(base(today.replace(day=1), True)),
        "YTD": pct(base(today.replace(month=1, day=1), True)),
        "1Y": pct(yr if yr is not None else (first_day, first_g)),
        "MAX": pct((first_day, first_g)),
    }
