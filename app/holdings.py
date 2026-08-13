"""Historical holdings reconstruction — pure, DB-free.

Answers one question: *what did the portfolio actually hold at each point in
time?* Several callers need it and, before this module existed, only
``twr.build_daily_records`` got it right — ``analytics._portfolio_value_series``
and ``prices.portfolio_value_history`` both valued history at **today's** share
counts. That back-projects current holdings onto a past in which they were not
held, or were held in a different size: a position opened last month appears to
have been owned all year, and one sold at a loss vanishes from the record. Both
the drawdown statistics and the dashboard value chart were computed off those
series.

The reconstruction is a running sum of signed lot quantities, so it is O(lots +
points) rather than O(lots × points) — which matters because the raw-timestamp
callers pass every price snapshot, of which there are currently ~112k.

Lots are dicts with at least ``symbol``, ``side`` ('BUY'/'SELL'), ``trade_date``
(a date) and ``quantity`` (float). A lot counts from its ``trade_date``
inclusive. Timestamps are resolved to a reporting-timezone calendar day before
comparison, matching how the rest of the repo buckets snapshots into days.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Sequence

import reporting_tz

LOCAL_TZ = reporting_tz.tzinfo()

# Accepted by every function that takes a point in time.
Point = date | datetime


def _as_day(point: Point) -> date:
    """Calendar day a point belongs to, in the reporting timezone."""
    if isinstance(point, datetime):
        ts = point if point.tzinfo else point.replace(tzinfo=LOCAL_TZ)
        return ts.astimezone(LOCAL_TZ).date()
    return point


def signed_quantity(lot: dict[str, Any]) -> float:
    """+qty for a BUY, -qty for a SELL."""
    qty = float(lot["quantity"])
    return qty if str(lot["side"]).upper() == "BUY" else -qty


def holdings_series(
    lots: Iterable[dict[str, Any]], points: Sequence[Point]
) -> list[tuple[Point, dict[str, float]]]:
    """Holdings as of each point, ascending.

    Returns [(point, {symbol: qty})] with one entry per input point, sorted by
    point. Symbols whose net quantity has gone to zero are dropped, so a closed
    position stops contributing rather than lingering at 0.
    """
    deltas = sorted(
        ((lot["trade_date"], lot["symbol"], signed_quantity(lot)) for lot in lots),
        key=lambda d: d[0],
    )

    running: dict[str, float] = {}
    cursor = 0
    out: list[tuple[Point, dict[str, float]]] = []
    for point in sorted(points, key=_as_day):
        day = _as_day(point)
        while cursor < len(deltas) and deltas[cursor][0] <= day:
            _, symbol, qty = deltas[cursor]
            running[symbol] = running.get(symbol, 0.0) + qty
            cursor += 1
        out.append((point, {s: q for s, q in running.items() if q != 0}))
    return out


def holdings_on(lots: Iterable[dict[str, Any]], point: Point) -> dict[str, float]:
    """Holdings as of a single point. Convenience wrapper over holdings_series."""
    series = holdings_series(lots, [point])
    return series[0][1] if series else {}


def value_series(
    lots: Iterable[dict[str, Any]],
    price_points: Sequence[tuple[Point, dict[str, float]]],
    *,
    carry_forward: bool = True,
) -> list[tuple[Point, float]]:
    """Market value at each point, using the holdings actually held then.

    Args:
        price_points: [(point, {symbol: price})] ascending. Prices may be
            sparse — a symbol missing at one point is valued at its last known
            price when ``carry_forward`` is set, and skipped otherwise.

    A held symbol with no price at or before a point contributes nothing to
    that point's value. That understates rather than invents, and the gap is
    reported separately by the data-quality checks.
    """
    lots = list(lots)
    points = [p for p, _ in price_points]
    holdings = dict(holdings_series(lots, points))

    last_price: dict[str, float] = {}
    out: list[tuple[Point, float]] = []
    for point, prices in price_points:
        if carry_forward:
            last_price.update(prices)
            lookup = last_price
        else:
            lookup = prices
        held = holdings.get(point, {})
        total = sum(qty * lookup[sym] for sym, qty in held.items() if sym in lookup)
        out.append((point, total))
    return out
