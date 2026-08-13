"""Returns service — time-weighted multi-period returns + benchmark comparison.

Backed by the pure ``twr`` module: historical holdings are reconstructed per
snapshot day and daily sub-period returns are chained, so deposits and the
timing/size of trades are neutralised (a contribution is never counted as a
gain). The benchmark (default SPY) is valued flat, so its TWR is a plain price
return — it excludes the benchmark's own dividends.

A ``Cutoff`` truncates the daily series rather than selecting a point: these are
series metrics, so "as of" means "stop the chain here", not "read this instant".
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

# Import deps first: it puts app/ on sys.path so the top-level `twr` module
# (a sibling of fifo.py/portfolio.py) resolves regardless of how tests are run.
from app.mcp.deps import get_conn
from app.mcp.services.cutoff import Cutoff, REPORTING_TZ

import corporate_actions
import twr

LOCAL_TZ = ZoneInfo(REPORTING_TZ)
_BASIS = "time_weighted_return"
DEFAULT_BENCHMARK = "SPY"
PERIODS = twr.PERIODS

# Trading days per year, for annualising daily volatility.
TRADING_DAYS_PER_YEAR = 252

# A benchmark comparison is refused below these. Both are endpoint inputs, so a
# caller who wants the number from patchy data can lower them deliberately.
MIN_ALIGNMENT_PCT = 80.0
MIN_ALIGNED_OBSERVATIONS = 10


def period_returns(
    *, today: date | None = None, cutoff: Cutoff | None = None
) -> dict[str, Any]:
    """Time-weighted return % for each standard period."""
    today = today or (cutoff.trade_date if cutoff else datetime.now(LOCAL_TZ).date())
    price_by_day = _price_by_day(cutoff)
    records = twr.build_daily_records(
        _fetch_lots(cutoff), price_by_day, _fetch_dividends(cutoff)
    )
    periods = twr.period_returns(records, today)
    return {
        "basis": _BASIS,
        "as_of": today.isoformat(),
        "periods": {p: periods.get(p) for p in PERIODS},
        "observations": len(records),
        "coverage": _coverage(price_by_day),
    }


def volatility(
    *, cutoff: Cutoff | None = None, annualised: bool = True
) -> dict[str, Any]:
    """Standard deviation of daily portfolio returns, read off the TWR curve.

    Taken from the growth curve rather than from raw value changes, so
    contributions do not register as volatility.
    """
    price_by_day = _price_by_day(cutoff)
    records = twr.build_daily_records(
        _fetch_lots(cutoff), price_by_day, _fetch_dividends(cutoff)
    )
    curve = twr.growth_curve(records)

    daily = [
        curve[i][1] / curve[i - 1][1] - 1.0
        for i in range(1, len(curve))
        if curve[i - 1][1]
    ]
    if len(daily) < 2:
        return {
            "daily_stdev_pct": None,
            "annualised_pct": None,
            "observations": len(daily),
            "null_reason": "insufficient_observations",
        }

    mean = sum(daily) / len(daily)
    # Sample stdev: these are observations of a process, not a whole population.
    variance = sum((r - mean) ** 2 for r in daily) / (len(daily) - 1)
    sd = math.sqrt(variance)
    return {
        "daily_stdev_pct": sd * 100.0,
        "annualised_pct": (
            sd * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0 if annualised else None
        ),
        "observations": len(daily),
        "basis": "stdev of daily TWR sub-period returns",
        "annualisation": f"x sqrt({TRADING_DAYS_PER_YEAR})" if annualised else None,
    }


def benchmark_comparison(
    period: str = "YTD",
    *,
    today: date | None = None,
    symbol: str | None = None,
    cutoff: Cutoff | None = None,
    min_alignment_pct: float = MIN_ALIGNMENT_PCT,
    min_observations: int = MIN_ALIGNED_OBSERVATIONS,
) -> dict[str, Any]:
    """Portfolio TWR vs a benchmark (default SPY) over one period.

    The benchmark is a price return from ``price_snapshots`` and excludes its own
    dividends, so it is slightly conservative against a dividend-inclusive
    portfolio TWR. Reported as ``dividends_included: false`` rather than left in
    prose.

    **Refuses rather than guesses.** If the benchmark and the portfolio do not
    share enough observed days, the returns come back null with a reason, the
    counts that produced the verdict, and a plain-English explanation. A
    benchmark number tends to get quoted later with any caveat stripped off, so
    a shaky one is worse than none. Lower the thresholds to override.
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    symbol = (symbol or os.getenv("PORTFOLIODB_BENCHMARK_SYMBOL", DEFAULT_BENCHMARK)).upper()
    today = today or (cutoff.trade_date if cutoff else datetime.now(LOCAL_TZ).date())

    price_by_day = _price_by_day(cutoff)
    records = twr.build_daily_records(
        _fetch_lots(cutoff), price_by_day, _fetch_dividends(cutoff)
    )
    port = twr.period_returns(records, today)
    bench = twr.period_returns(twr.benchmark_records(price_by_day, symbol), today)

    alignment = _alignment(price_by_day, records, symbol, period, today)
    base = {
        "period": period,
        "benchmark_symbol": symbol,
        "basis": _BASIS,
        "benchmark_source": "price_snapshots",
        "benchmark_currency": "USD",
        "dividends_included": False,
        "fx_applied": False,
        "note": "Portfolio TWR vs benchmark price return (excludes benchmark dividends).",
        "alignment": {
            **alignment,
            "required_pct": min_alignment_pct,
            "minimum_observations": min_observations,
        },
    }

    aligned = alignment["aligned_observations"]
    aligned_pct = alignment["aligned_pct"]
    insufficient = (
        aligned < min_observations
        or (aligned_pct is not None and aligned_pct < min_alignment_pct)
    )

    if insufficient:
        return {
            **base,
            "status": "insufficient_alignment",
            "portfolio_return_pct": None,
            "benchmark_return_pct": None,
            "relative_return_pct": None,
            "null_reasons": {
                "portfolio_return_pct": "insufficient_alignment",
                "benchmark_return_pct": "insufficient_alignment",
                "relative_return_pct": "insufficient_alignment",
            },
            "explanation": _alignment_explanation(
                symbol, alignment, min_alignment_pct, min_observations
            ),
        }

    p, b = port.get(period), bench.get(period)
    relative = (p - b) if (p is not None and b is not None) else None
    null_reasons: dict[str, str] = {}
    if p is None:
        null_reasons["portfolio_return_pct"] = "insufficient_portfolio_history"
    if b is None:
        null_reasons["benchmark_return_pct"] = "insufficient_benchmark_history"
    if relative is None:
        null_reasons["relative_return_pct"] = "missing_operand"

    return {
        **base,
        "status": "ok",
        "portfolio_return_pct": p,
        "benchmark_return_pct": b,
        "relative_return_pct": relative,
        "null_reasons": null_reasons,
    }


# ────────────────────────── alignment ──────────────────────────


def _period_start(period: str, today: date) -> date | None:
    """First day the period covers. None for MAX (all of history)."""
    from datetime import timedelta

    if period == "MAX":
        return None
    if period == "1D":
        return today - timedelta(days=1)
    if period == "WTD":
        return today - timedelta(days=today.weekday())
    if period == "MTD":
        return today.replace(day=1)
    if period == "YTD":
        return today.replace(month=1, day=1)
    if period == "1Y":
        return today - timedelta(days=365)
    return None


def _alignment(
    price_by_day: dict[date, dict[str, float]],
    records: list[dict[str, Any]],
    symbol: str,
    period: str,
    today: date,
) -> dict[str, Any]:
    """How many days in the period both sides actually observed.

    "Expected" is the number of days the *portfolio* has an observation for, not
    a trading-day count from a calendar — there is no market calendar in this
    database, and inventing one would put a guess underneath a guard.
    """
    start = _period_start(period, today)
    port_days = {
        r["day"] for r in records
        if r["mv"] > 0 and (start is None or r["day"] >= start) and r["day"] <= today
    }
    bench_days = {
        day for day, prices in price_by_day.items()
        if symbol in prices and (start is None or day >= start) and day <= today
    }
    aligned = port_days & bench_days
    expected = len(port_days)

    return {
        "aligned_observations": len(aligned),
        "expected_observations": expected,
        "missing_observations": expected - len(aligned),
        "aligned_pct": (len(aligned) / expected * 100.0) if expected else None,
        "benchmark_observations": len(bench_days),
        "coverage_period": {
            "start": min(aligned).isoformat() if aligned else None,
            "end": max(aligned).isoformat() if aligned else None,
        },
        "requested_period": {
            "start": start.isoformat() if start else None,
            "end": today.isoformat(),
        },
    }


def _alignment_explanation(
    symbol: str, alignment: dict[str, Any], min_pct: float, min_obs: int
) -> str:
    aligned = alignment["aligned_observations"]
    expected = alignment["expected_observations"]
    pct = alignment["aligned_pct"]

    if expected == 0:
        return (
            "The portfolio has no valued days in this period, so there is "
            "nothing to compare a benchmark against."
        )
    if aligned < min_obs:
        return (
            f"{symbol} and the portfolio share only {aligned} observed day(s) in "
            f"this period, below the {min_obs} required for a meaningful "
            f"comparison."
        )
    return (
        f"{symbol} has prices for {aligned} of the {expected} days the portfolio "
        f"was valued in this period ({pct:.1f}%, below the {min_pct:.0f}% "
        f"required). Comparing over the overlap alone would describe a different "
        f"period than the one requested."
    )


def _coverage(price_by_day: dict[date, dict[str, float]]) -> dict[str, Any]:
    days = sorted(price_by_day)
    return {
        "start": days[0].isoformat() if days else None,
        "end": days[-1].isoformat() if days else None,
        "days": len(days),
    }


# ────────────────────────── data loaders ──────────────────────────


def _fetch_lots(cutoff: Cutoff | None = None) -> list[dict[str, Any]]:
    """Lots restated into post-split units.

    Cash flow (quantity × price) is invariant under the adjustment; the
    reconstructed share count is not, which is what the holdings walk needs.
    """
    trade_date = cutoff.trade_date if cutoff else None
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, side, trade_date, quantity, price, fees
                FROM lots
                WHERE (%s::date IS NULL OR trade_date <= %s)
                """,
                (trade_date, trade_date),
            )
            rows = [
                {
                    "symbol": r[0], "side": r[1], "trade_date": r[2],
                    "quantity": r[3], "price": r[4], "fees": float(r[5] or 0),
                }
                for r in cur.fetchall()
            ]
    adjusted = corporate_actions.adjust_lot_rows(rows, actions)
    for r in adjusted:
        r["quantity"] = float(r["quantity"])
        r["price"] = float(r["price"])
    return adjusted


def _fetch_dividends(cutoff: Cutoff | None = None) -> list[dict[str, Any]]:
    pay_date = cutoff.trade_date if cutoff else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pay_date, amount FROM income
                WHERE (%s::date IS NULL OR pay_date <= %s)
                """,
                (pay_date, pay_date),
            )
            return [
                {"pay_date": r[0], "amount": float(r[1])}
                for r in cur.fetchall()
                if r[0] is not None and r[1] is not None
            ]


def _price_by_day(cutoff: Cutoff | None = None) -> dict[date, dict[str, float]]:
    """One split-adjusted last_price per symbol per reporting-timezone day.

    Without the split adjustment the chained TWR reads a 2:1 split as a -50%
    day and never recovers it, understating every period that spans the
    ex-date.
    """
    as_of_ts = cutoff.ts if cutoff else None
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (symbol, date_trunc('day', ts AT TIME ZONE %s))
                       date_trunc('day', ts AT TIME ZONE %s)::date AS day_local,
                       symbol, last_price
                FROM price_snapshots
                WHERE (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY symbol,
                         date_trunc('day', ts AT TIME ZONE %s),
                         ts DESC
                """,
                (REPORTING_TZ, REPORTING_TZ, as_of_ts, as_of_ts, REPORTING_TZ),
            )
            out: dict[date, dict[str, float]] = {}
            for day, sym, price in cur.fetchall():
                out.setdefault(day, {})[sym] = float(price)
    return corporate_actions.adjust_price_by_day(out, actions)
