"""Split adjustment — pure, DB-free (except one connection-taking loader).

PortfolioDB stores raw quotes and raw share counts. A split therefore puts a
step change in the middle of both series: `price_snapshots` halves overnight
for a 2:1, and `lots.quantity` keeps whatever was entered at the time. Left
alone, ``twr.build_daily_records`` reads the price step as a real -50% day and
chains it into every period return that spans the ex-date.

Adjustment happens at **read** time. Nothing rewrites `lots` or
`price_snapshots`: the ledger stays append-only (a design invariant — see
CLAUDE.md), and an action is undone by deleting its row rather than by trying
to reverse an in-place UPDATE. The cost is that every historical series reader
has to opt in by calling one of the adjust_* helpers below.

Ratio convention: **new shares per old share**. A 2:1 split is ``2``; a 1:10
reverse split is ``0.1``. A record dated before the ex-date is restated into
today's units by multiplying quantity by the ratio and dividing price by it, so
that quantity × price — the money — is unchanged.

The two flags on an action are independent:

  adjust_prices  the quote series was rebased at the ex-date. Nearly always
                 true; this is the flag that fixes TWR.
  adjust_lots    extra shares were actually credited to the account. False when
                 the recorded quantity is already in post-split units.

Boundary: a *date* is pre-split when ``d < ex_date``. A *timestamp* is
pre-split when ``ts < ex_ts``, falling back to reporting-local midnight on the
ex-date when ``ex_ts`` is unknown. The fallback is imprecise for intraday
readers, because a split takes effect at the session open rather than at local
midnight — set ``ex_ts`` (the ex-date's regular-session open) whenever a raw
timestamp series is consumed, which is what ``drawdown_stats`` and
``portfolio_value_history`` do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

import reporting_tz

LOCAL_TZ = reporting_tz.tzinfo()

# Ratios a real split plausibly takes. Used only by the detection heuristic —
# adjustment itself accepts any positive ratio.
COMMON_RATIOS: tuple[Decimal, ...] = tuple(
    Decimal(str(r))
    for r in (2, 3, 4, 5, 6, 8, 10, 20, 1.5, 0.5, Decimal(1) / 3, 0.25, 0.2, 0.1, 0.05)
)

# A day-over-day ratio must land within this fraction of a common ratio to be
# reported as a suspected split. 2% is wide enough to survive the gap between a
# stale prior close and the first post-split print, tight enough that ordinary
# moves (even a +41% biotech day) do not reach the nearest candidate.
DETECT_TOLERANCE = 0.02

# Day-over-day moves inside this band are never split candidates, whatever the
# arithmetic says — it keeps the scan off the noise floor.
DETECT_MIN_MOVE = 0.25


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    # SPLIT | REVERSE_SPLIT | NONE. 'NONE' records a discontinuity that was
    # investigated and found not to be a corporate action — a real one-day
    # crash is indistinguishable from a split in a price series, and without
    # somewhere to write down "we checked, it wasn't one" the heuristic
    # re-reports it forever. Those rows carry ratio 1, the identity factor, so
    # they cannot adjust anything even if the flags are set.
    kind: str
    ex_date: date
    ratio: Decimal
    ex_ts: datetime | None = None
    adjust_prices: bool = True
    adjust_lots: bool = True
    notes: str | None = None
    reviewed: bool = False

    def boundary(self) -> datetime:
        """Instant at which this action took effect."""
        if self.ex_ts is not None:
            return self.ex_ts
        return datetime.combine(self.ex_date, time.min, tzinfo=LOCAL_TZ)


# ────────────────────────── loading ──────────────────────────


def fetch_actions(conn) -> list[CorporateAction]:
    """Load every recorded action. Takes an open connection so this module
    never owns connection lifecycle (mirrors how twr is fed by its callers).

    Returns an empty list when the table does not exist yet, so a database that
    has not had the migration applied degrades to "no adjustment" instead of
    breaking every price reader.
    """
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT symbol, kind, ex_date, ratio, ex_ts,
                       adjust_prices, adjust_lots, notes, reviewed
                FROM corporate_actions
                ORDER BY symbol, ex_date
                """
            )
            rows = cur.fetchall()
        except Exception:
            conn.rollback()
            return []
    return [
        CorporateAction(
            symbol=r[0],
            kind=r[1],
            ex_date=r[2],
            ratio=Decimal(str(r[3])),
            ex_ts=r[4],
            adjust_prices=bool(r[5]),
            adjust_lots=bool(r[6]),
            notes=r[7],
            reviewed=bool(r[8]) if len(r) > 8 else False,
        )
        for r in rows
    ]


def by_symbol(
    actions: Iterable[CorporateAction],
) -> dict[str, list[CorporateAction]]:
    out: dict[str, list[CorporateAction]] = {}
    for a in actions:
        out.setdefault(a.symbol.upper(), []).append(a)
    return out


# ────────────────────────── factors ──────────────────────────


def lot_factor(
    actions: Sequence[CorporateAction] | None, trade_date: date
) -> Decimal:
    """Cumulative share multiplier applied to a lot traded on ``trade_date``.

    1 when nothing applies. Compounds when several splits follow the trade.
    """
    factor = Decimal(1)
    for a in actions or ():
        if a.adjust_lots and trade_date < a.ex_date:
            factor *= a.ratio
    return factor


def price_factor(
    actions: Sequence[CorporateAction] | None, when: date | datetime
) -> Decimal:
    """Cumulative divisor applied to a quote observed at ``when``.

    Accepts a date (compared against ex_date) or a timestamp (compared against
    the action boundary), so daily and raw series share one code path.
    """
    factor = Decimal(1)
    for a in actions or ():
        if not a.adjust_prices:
            continue
        if isinstance(when, datetime):
            ts = when if when.tzinfo else when.replace(tzinfo=LOCAL_TZ)
            pre = ts < a.boundary()
        else:
            pre = when < a.ex_date
        if pre:
            factor *= a.ratio
    return factor


# ────────────────────────── adjusters ──────────────────────────


def adjust_lot_rows(
    rows: list[dict[str, Any]], actions: Iterable[CorporateAction]
) -> list[dict[str, Any]]:
    """Restate lot rows into post-split units.

    Quantity is multiplied and price divided by the same factor, so cost
    (quantity × price) and therefore realized P&L are unchanged — only the
    per-share view moves. Fees are untouched: they were paid in cash.

    Always returns fresh dicts, including for rows nothing applied to, so a
    caller can safely coerce or annotate the result without reaching back into
    the ledger rows it passed in.
    """
    idx = by_symbol(actions)

    out: list[dict[str, Any]] = []
    for r in rows:
        adjusted = dict(r)
        acts = idx.get(str(r["symbol"]).upper()) if idx else None
        factor = lot_factor(acts, r["trade_date"]) if acts else Decimal(1)
        if factor != 1:
            adjusted["quantity"] = Decimal(str(r["quantity"])) * factor
            adjusted["price"] = Decimal(str(r["price"])) / factor
        out.append(adjusted)
    return out


def adjust_price_by_day(
    price_by_day: Mapping[date, Mapping[str, float]],
    actions: Iterable[CorporateAction],
) -> dict[date, dict[str, float]]:
    """Restate a {day: {symbol: price}} map into post-split units.

    This is the shape ``twr`` and ``returns`` use.
    """
    idx = by_symbol(actions)
    if not idx:
        return {d: dict(m) for d, m in price_by_day.items()}

    out: dict[date, dict[str, float]] = {}
    for day, per_symbol in price_by_day.items():
        row: dict[str, float] = {}
        for sym, price in per_symbol.items():
            acts = idx.get(sym.upper())
            factor = price_factor(acts, day) if acts else Decimal(1)
            row[sym] = price / float(factor) if factor != 1 else price
        out[day] = row
    return out


def adjust_price_points(
    points: Iterable[tuple[Any, str, float]],
    actions: Iterable[CorporateAction],
) -> list[tuple[Any, str, float]]:
    """Restate (when, symbol, price) triples. ``when`` may be a date or a
    timestamp — see the module docstring on boundary precision."""
    idx = by_symbol(actions)
    pts = list(points)
    if not idx:
        return pts

    out: list[tuple[Any, str, float]] = []
    for when, sym, price in pts:
        acts = idx.get(sym.upper())
        factor = price_factor(acts, when) if acts else Decimal(1)
        out.append((when, sym, price / float(factor) if factor != 1 else price))
    return out


# ────────────────────────── detection ──────────────────────────


def detect_suspected_splits(
    daily_prices: Mapping[str, Sequence[tuple[date, float]]],
    known: Iterable[CorporateAction] = (),
    *,
    tolerance: float = DETECT_TOLERANCE,
    min_move: float = DETECT_MIN_MOVE,
) -> list[dict[str, Any]]:
    """Flag day-over-day price steps that look like an unrecorded split.

    A backstop, not a source: it catches splits that predate the collector or
    that the upstream feed never reported. It cannot distinguish a split from a
    genuine one-day collapse, so callers must present results as *suspected*
    and never act on them automatically.

    Args:
        daily_prices: {symbol: [(day, price)]}, ascending by day. One price per
            day — pass the day's last snapshot, not raw intraday points.
        known: already-recorded actions, which are excluded from the result.
        tolerance: max fractional distance from a common ratio.
        min_move: ignore day-over-day moves smaller than this fraction.

    Returns [{symbol, day, prev_day, prev_price, price, observed_ratio,
    nearest_ratio, deviation}] sorted by symbol then day.
    """
    known_keys = {
        (a.symbol.upper(), a.ex_date) for a in known
    }

    found: list[dict[str, Any]] = []
    for symbol, series in daily_prices.items():
        sym = symbol.upper()
        ordered = sorted(series, key=lambda p: p[0])
        for (prev_day, prev_price), (day, price) in zip(ordered, ordered[1:]):
            if not prev_price or not price or prev_price <= 0:
                continue
            observed = price / prev_price
            if abs(observed - 1.0) < min_move:
                continue
            if (sym, day) in known_keys:
                continue

            nearest, deviation = _nearest_ratio(observed)
            if nearest is None or deviation > tolerance:
                continue
            found.append({
                "symbol": sym,
                "day": day,
                "prev_day": prev_day,
                "prev_price": prev_price,
                "price": price,
                # Prices move inversely to share count: a price ratio of 0.5 is
                # a 2:1 split, so the share ratio is the reciprocal.
                "observed_ratio": round(1.0 / observed, 6),
                "nearest_ratio": float(nearest),
                "deviation": round(deviation, 6),
            })
    found.sort(key=lambda r: (r["symbol"], r["day"]))
    return found


def _nearest_ratio(observed_price_ratio: float) -> tuple[Decimal | None, float]:
    """Closest common *price* ratio (reciprocal of the share ratio) and its
    fractional distance."""
    best: Decimal | None = None
    best_dev = float("inf")
    for share_ratio in COMMON_RATIOS:
        price_ratio = 1.0 / float(share_ratio)
        dev = abs(observed_price_ratio - price_ratio) / price_ratio
        if dev < best_dev:
            best, best_dev = share_ratio, dev
    return best, best_dev
