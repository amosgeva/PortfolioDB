"""Returns service — time-weighted multi-period returns + benchmark comparison.

Backed by the pure ``twr`` module: historical holdings are reconstructed per
snapshot day and daily sub-period returns are chained, so a contribution is
never counted as a gain nor a sale as a loss (within a day, purchases are
start-weighted and proceeds end-weighted — see twr.py). The benchmark (default
SPY) is valued flat, so its TWR is a plain price return — it excludes the
benchmark's own dividends.

A ``Cutoff`` truncates the daily series rather than selecting a point: these are
series metrics, so "as of" means "stop the chain here", not "read this instant".
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

# Import deps first: it puts app/ on sys.path so the top-level `twr` module
# (a sibling of fifo.py/portfolio.py) resolves regardless of how tests are run.
from psycopg2 import sql

from app.mcp.deps import get_conn
from app.mcp.services import common
from app.mcp.services.cutoff import Cutoff, LOCAL_TZ, tz_name

import benchmark
import corporate_actions
import ledger_inputs
import portfolio_kpis
import twr

_BASIS = "time_weighted_return"
DEFAULT_BENCHMARK = benchmark.DEFAULT_BENCHMARK
PERIODS = twr.PERIODS

# Trading days per year, for annualising daily volatility.
TRADING_DAYS_PER_YEAR = 252

# A benchmark comparison is refused below these. Both are endpoint inputs, so a
# caller who wants the number from patchy data can lower them deliberately.
MIN_ALIGNMENT_PCT = 80.0
MIN_ALIGNED_OBSERVATIONS = 10

# Short periods cannot reach ten observed days: a 1D return spans two (the
# last observed day before today, and today), a WTD one at most six. The
# blanket minimum made both periods the tool advertises permanently
# unanswerable (audit X-6). Their floor is the two observations a return needs
# at all; the alignment percentage still applies to every period.
PERIOD_MIN_OBSERVATIONS = {"1D": 2, "WTD": 2}


def period_minimum(period: str) -> int:
    return PERIOD_MIN_OBSERVATIONS.get(period, MIN_ALIGNED_OBSERVATIONS)


def _records(cutoff: Cutoff | None, account: str | None, price_by_day):
    """The daily TWR records for the whole book or one account.

    Account-scoped means both flows are scoped: that account's trades and that
    account's income. Prices are shared — a quote is not per-account.
    """
    return twr.build_daily_records(
        _fetch_lots(cutoff, account=account),
        price_by_day,
        _fetch_dividends(cutoff, account=account),
    )


def period_returns(
    *,
    today: date | None = None,
    cutoff: Cutoff | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    """Time-weighted return % for each standard period.

    ``account`` scopes the return to one account's trades and income ('(none)'
    for lots recorded without an account); None is the whole book.
    """
    today = today or (cutoff.trade_date if cutoff else datetime.now(LOCAL_TZ).date())
    price_by_day = _price_by_day(cutoff)
    records = _records(cutoff, account, price_by_day)
    periods = twr.period_returns(records, today)
    return {
        "basis": _BASIS,
        "as_of": today.isoformat(),
        "account": account,
        "periods": {p: periods.get(p) for p in PERIODS},
        "observations": len(records),
        "coverage": _coverage(price_by_day),
    }


def volatility(
    *,
    cutoff: Cutoff | None = None,
    annualised: bool = True,
    account: str | None = None,
) -> dict[str, Any]:
    """Standard deviation of daily portfolio returns, read off the TWR curve.

    Taken from the growth curve rather than from raw value changes, so
    contributions do not register as volatility.
    """
    price_by_day = _price_by_day(cutoff)
    records = _records(cutoff, account, price_by_day)
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
    min_observations: int | None = None,
    account: str | None = None,
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
    if min_observations is None:
        min_observations = period_minimum(period)
    # The tool schema floors this at 2; a direct service caller may pass 0 to
    # switch the observation guard off deliberately (the alignment % remains).
    if isinstance(min_observations, bool) or not isinstance(min_observations, int) or min_observations < 0:
        raise ValueError("min_observations must be a non-negative integer")
    if not (0.0 <= float(min_alignment_pct) <= 100.0):
        raise ValueError("min_alignment_pct must be between 0 and 100")
    # One resolution for every surface (app/benchmark.py): the dashboard
    # computes against the same symbol (audit U-3).
    symbol = benchmark.benchmark_symbol(symbol)
    today = today or (cutoff.trade_date if cutoff else datetime.now(LOCAL_TZ).date())

    price_by_day = _price_by_day(cutoff)
    records = _records(cutoff, account, price_by_day)
    port = twr.period_returns(records, today)
    bench = twr.period_returns(twr.benchmark_records(price_by_day, symbol), today)

    alignment = _alignment(price_by_day, records, symbol, period, today)
    base = {
        "period": period,
        "account": account,
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


def _period_start(
    period: str, today: date, records: list[dict[str, Any]] | None = None
) -> date | None:
    """First day the period's return is measured from. None for MAX.

    The same base day ``twr.period_returns`` uses: the last observed day
    *before* the period's boundary (for 1Y, on or before it). A 1D return on a
    Monday is measured from Friday, so the alignment window must include
    Friday — counting from the calendar boundary (Sunday) found one day and
    could never pass. Without records, the calendar boundary.
    """
    from datetime import timedelta

    if period == "MAX":
        return None
    strict = True
    if period == "1D":
        boundary = today
    elif period == "WTD":
        boundary = today - timedelta(days=today.weekday())
    elif period == "MTD":
        boundary = today.replace(day=1)
    elif period == "YTD":
        boundary = today.replace(month=1, day=1)
    elif period == "1Y":
        boundary, strict = today - timedelta(days=365), False
    else:
        return None
    if records:
        days = sorted(
            r["day"] for r in records
            if (r["day"] < boundary if strict else r["day"] <= boundary)
        )
        if days:
            return days[-1]
    return boundary


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
    start = _period_start(period, today, records)
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


def _fetch_lots(
    cutoff: Cutoff | None = None, *, account: str | None = None
) -> list[dict[str, Any]]:
    """Lots restated into today's units, optionally for one account.

    Composed on the parity contract (ledger_inputs.LOTS_SQL + prepare) with
    every recorded action applied, because the price series below is restated
    with every action too: the TWR walk multiplies these quantities by those
    prices. Cash flow (quantity × price) is invariant under the adjustment; the
    reconstructed share count is not, which is what the holdings walk needs.

    ``account`` filters with the same rule as every other reader ('(none)'
    selects lots without an account). The rows carry ``account`` — the review's
    account-scoped XIRR used to filter on a key this loader never selected,
    and so received no lots at all (audit X-1).
    """
    trade_date = cutoff.trade_date if cutoff else None
    query = ledger_inputs.LOTS_SQL
    params: list[Any] = []
    query, params = common.add_account_filter(query, params, account)
    if trade_date is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(trade_date)
    query += ledger_inputs.LOTS_ORDER
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    adjusted = ledger_inputs.prepare(rows, actions).lots
    for r in adjusted:
        r["quantity"] = float(r["quantity"])
        r["price"] = float(r["price"])
        r["fees"] = float(r.get("fees") or 0)
    return adjusted


def _fetch_dividends(
    cutoff: Cutoff | None = None, *, account: str | None = None
) -> list[dict[str, Any]]:
    """Income received by the cutoff, optionally for one account.

    Scoped with the lots: an account's money-weighted or time-weighted return
    built from its own trades and every account's dividends was wrong by the
    other accounts' income (audit Q-3).

    USD only: an amount recorded in another currency is not a dollar amount
    and is left out of the TWR and XIRR flows, as it is from every income
    total (docs/methodology.md §14). income_summary lists what was excluded.
    """
    pay_date = cutoff.trade_date if cutoff else None
    query = sql.SQL("SELECT pay_date, amount, account, currency FROM income WHERE 1=1")
    params: list[Any] = []
    query, params = common.add_account_filter(query, params, account)
    if pay_date is not None:
        query += sql.SQL(" AND pay_date <= %s")
        params.append(pay_date)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            recs = [dict(zip(cols, r)) for r in cur.fetchall()]
    return [
        {"pay_date": r["pay_date"], "amount": float(r["amount"]), "account": r.get("account")}
        for r in recs
        if r.get("pay_date") is not None and r.get("amount") is not None
        and portfolio_kpis.currency_of(r) == portfolio_kpis.BASE_CURRENCY
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
                WHERE last_price > 0 AND (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY symbol,
                         date_trunc('day', ts AT TIME ZONE %s),
                         ts DESC
                """,
                (tz_name(), tz_name(), as_of_ts, as_of_ts, tz_name()),
            )
            out: dict[date, dict[str, float]] = {}
            for day, sym, price in cur.fetchall():
                out.setdefault(day, {})[sym] = float(price)
    return corporate_actions.adjust_price_by_day(out, actions)
