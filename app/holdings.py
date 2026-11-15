"""Net quantity — the one implementation — and historical holdings. Pure, DB-free.

Answers *what did the portfolio actually hold at each point in time?* Before
1.8.0 this question had eight answers: this module, two report helpers, and SQL
``SUM(CASE WHEN side='BUY' …)`` copies in the collector, the add-lot CLI, the
logo fetcher, both enrichment scripts and three MCP services (audit R-1). None
of them agreed with the engines in the edge cases, because none of them did
what the engines do:

* **Matching is per (symbol, account).** A sale entered against an account
  that holds nothing does not reduce another account's position; the engines
  find no buys in its stream and drop it. A plain per-symbol sum subtracted it
  anyway (F-2).
* **A sale is truncated at the open quantity.** Shorts are unsupported: the
  engines match what is open and drop the rest. A plain running sum went
  negative and then let a later purchase "repay" it, so a later buy read as
  flat while the engines counted it.
* **Within a day, buys come before sells** (``ledger_core.processing_key``).
  Before this, a same-day sell stored ahead of its buy was dropped by the
  engines while every plain sum counted it (F-1).

So the walk below is the engines' walk, quantity only: ordered by
``processing_key`` within each (symbol, account) stream, clamped at zero, in
exact ``Decimal`` (float residue made ``0.7 + 0.1`` a false oversell of 0.8 and
left −1e−16 "positions" behind; F-13). Per-symbol figures are the per-account
figures summed, which is how ``portfolio`` merges the engines' results, so
``open_quantities(lots)[s]`` equals the FIFO and average-cost ``qty`` for every
symbol by construction. The SQL function ``lot_open_quantities`` in
``sql/schema.sql`` is the same walk for readers that only have SQL; a database
test holds the two to the same answers.

Lots are dicts with ``symbol``, ``side`` ('BUY'/'SELL'), ``trade_date`` (a
date) and ``quantity``; ``account`` and ``id`` are used when present (a missing
account is the unassigned account, a missing id falls back to input order). A
lot counts from its ``trade_date`` inclusive. Timestamps are resolved to a
reporting-timezone calendar day before comparison, matching how the rest of
the repo buckets snapshots into days. Pass lots already restated by
``ledger_inputs`` — this module does not apply splits.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence

import reporting_tz
from ledger_core import ZERO, side_rank, to_decimal

# Accepted by every function that takes a point in time.
Point = date | datetime
# (symbol, account) — the stream the engines match within.
PositionKey = tuple[str, "str | None"]


def _as_day(point: Point, tz) -> date:
    """Calendar day a point belongs to, in the reporting timezone."""
    if isinstance(point, datetime):
        ts = point if point.tzinfo else point.replace(tzinfo=tz)
        return ts.astimezone(tz).date()
    return point


def _tz_for(points: Iterable[Point]):
    """The reporting timezone, resolved per call rather than at import (audit
    R-5: importing this module must not reach the settings table) and only
    when a timestamp actually needs it."""
    return reporting_tz.tzinfo() if any(isinstance(p, datetime) for p in points) else None


def _ordered(lots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lots in engine processing order. Input position breaks ties when a lot
    carries no id, so a caller that passes rows in id order keeps it."""
    indexed = list(enumerate(lots))
    indexed.sort(key=lambda il: (
        il[1]["trade_date"],
        side_rank(il[1]["side"]),
        il[1].get("id") if il[1].get("id") is not None else il[0],
        il[0],
    ))
    return [lot for _, lot in indexed]


def _key(lot: dict[str, Any]) -> PositionKey:
    return (lot["symbol"], lot.get("account"))


def _step(held: Decimal, lot: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """(open quantity after this lot, quantity the engines would drop)."""
    qty = to_decimal(lot["quantity"])
    if str(lot["side"]).upper() == "BUY":
        return held + qty, ZERO
    if qty <= held:
        return held - qty, ZERO
    return ZERO, qty - held


def walk(lots: Iterable[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], Decimal, Decimal, Decimal]]:
    """Yield ``(lot, held_before, held_after, dropped)`` in processing order.

    ``held_*`` are the lot's own (symbol, account) stream; ``dropped`` is the
    part of a SELL the engines truncate because nothing was open to match it.
    """
    running: dict[PositionKey, Decimal] = defaultdict(lambda: ZERO)
    for lot in _ordered(lots):
        key = _key(lot)
        before = running[key]
        after, dropped = _step(before, lot)
        running[key] = after
        yield lot, before, after, dropped


def net_quantities(
    lots: Iterable[dict[str, Any]], *, as_of: date | None = None
) -> dict[PositionKey, Decimal]:
    """Open quantity per (symbol, account), engine semantics. Closed streams are
    left out. ``as_of`` keeps lots with ``trade_date <= as_of``."""
    selected = [l for l in lots if as_of is None or l["trade_date"] <= as_of]
    out: dict[PositionKey, Decimal] = {}
    for lot, _before, after, _dropped in walk(selected):
        out[_key(lot)] = after
    return {k: q for k, q in out.items() if q != 0}


def open_quantities(
    lots: Iterable[dict[str, Any]], *, as_of: date | None = None
) -> dict[str, Decimal]:
    """Open quantity per symbol: the per-account figures summed — exactly the
    ``qty`` column ``portfolio.compute_fifo_merged`` produces."""
    merged: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for (symbol, _account), qty in net_quantities(lots, as_of=as_of).items():
        merged[symbol] += qty
    return {s: q for s, q in merged.items() if q != 0}


def open_symbols(lots: Iterable[dict[str, Any]], *, as_of: date | None = None) -> set[str]:
    """Symbols with a positive open quantity."""
    return {s for s, q in open_quantities(lots, as_of=as_of).items() if q > 0}


def truncations(lots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every SELL the engines would truncate, with what was open and what is
    dropped — the condition behind their "SELL exceeds BUYs" warning.

    Same walk as everything above, so a data-quality check built on this
    flags exactly the rows the engines truncate (and, fed prepared lots, does
    not mistake a post-split sale for an oversell).
    """
    out: list[dict[str, Any]] = []
    for lot, before, _after, dropped in walk(lots):
        if dropped > 0:
            out.append({
                "id": lot.get("id"),
                "symbol": lot["symbol"],
                "account": lot.get("account"),
                "trade_date": lot["trade_date"],
                "quantity": to_decimal(lot["quantity"]),
                "held": before,
                "dropped": dropped,
            })
    return out


def signed_quantity(lot: dict[str, Any]) -> Decimal:
    """+qty for a BUY, -qty for a SELL — the raw delta, before truncation."""
    qty = to_decimal(lot["quantity"])
    return qty if str(lot["side"]).upper() == "BUY" else -qty


def holdings_series(
    lots: Iterable[dict[str, Any]], points: Sequence[Point]
) -> list[tuple[Point, dict[str, float]]]:
    """Holdings as of each point, ascending.

    Returns [(point, {symbol: qty})] with one entry per input point, sorted by
    point. Quantities are computed exactly and handed out as floats, because
    every caller multiplies them by a float price. Symbols whose net quantity
    has gone to zero are dropped, so a closed position stops contributing
    rather than lingering at 0. Points with the same holdings share one dict:
    treat the dicts as read-only.
    """
    tz = _tz_for(points)
    events = list(walk(lots))
    merged: dict[str, Decimal] = defaultdict(lambda: ZERO)
    cursor = 0
    current: dict[str, float] = {}
    out: list[tuple[Point, dict[str, float]]] = []
    for point in sorted(points, key=lambda p: _as_day(p, tz)):
        day = _as_day(point, tz)
        changed = False
        while cursor < len(events) and events[cursor][0]["trade_date"] <= day:
            lot, before, after, _dropped = events[cursor]
            merged[lot["symbol"]] += after - before
            cursor += 1
            changed = True
        if changed:
            current = {s: float(q) for s, q in merged.items() if q != 0}
        # Points between trades share one dict (callers only read it): the
        # raw-timestamp readers pass ~100k points and a handful of trades.
        out.append((point, current))
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
    reported separately by the data-quality checks. A price that is not
    positive is no price: a zero quote is a vendor fault, never a valuation
    (audit F-15), so it is skipped and the last good price carries forward.
    """
    lots = list(lots)
    points = [p for p, _ in price_points]
    held_at = dict(holdings_series(lots, points))

    last_price: dict[str, float] = {}
    out: list[tuple[Point, float]] = []
    for point, prices in price_points:
        good = {s: p for s, p in prices.items() if p is not None and p > 0}
        if carry_forward:
            last_price.update(good)
            lookup = last_price
        else:
            lookup = good
        held = held_at.get(point, {})
        total = sum(qty * lookup[sym] for sym, qty in held.items() if sym in lookup)
        out.append((point, total))
    return out
