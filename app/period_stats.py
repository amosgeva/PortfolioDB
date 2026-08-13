"""Period statistics — best/worst day, week and month. Pure, DB-free.

Built on the TWR growth curve rather than raw portfolio value, so a deposit is
never mistaken for a good week. The curve is the same one `app/twr.py` produces
for the returns strip and the MCP endpoints, so these figures agree with those
by construction instead of being a second opinion.

**Partial periods are excluded from the records.** Price coverage starts
mid-week and mid-month, and the current week and month are still running. A
half-month is not comparable to a full one, so the first and last groups are
marked `partial` and kept out of best/worst — they still appear in the monthly
table, because a gap there would be more confusing than a labelled part-month.

Every figure carries its sample size. With coverage starting 2025-09-22 there
are only about eleven months of history, and "best month" out of nine full ones
deserves to be read with that in mind.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from typing import Any, Iterable, Sequence

MONTH_LABELS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def daily_returns(curve: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Per-day return from a cumulative growth curve.

    The curve is [(day, growth_factor)] starting at 1.0; the return for day i is
    g_i / g_{i-1} - 1. The first point has no predecessor and is dropped.
    """
    out: list[tuple[date, float]] = []
    for i in range(1, len(curve)):
        prev_g = curve[i - 1][1]
        if prev_g:
            out.append((curve[i][0], curve[i][1] / prev_g - 1.0))
    return out


def _week_key(day: date) -> tuple[int, int]:
    iso = day.isocalendar()
    return (iso[0], iso[1])


def _month_key(day: date) -> tuple[int, int]:
    return (day.year, day.month)


def _compound(returns: Iterable[float]) -> float:
    total = 1.0
    for r in returns:
        total *= (1.0 + r)
    return total - 1.0


def _group(
    dailies: Sequence[tuple[date, float]], keyfn
) -> list[dict[str, Any]]:
    """Compound daily returns into ordered groups, flagging the partial ends."""
    buckets: OrderedDict[Any, list[tuple[date, float]]] = OrderedDict()
    for day, r in dailies:
        buckets.setdefault(keyfn(day), []).append((day, r))

    groups: list[dict[str, Any]] = []
    keys = list(buckets)
    for idx, key in enumerate(keys):
        rows = buckets[key]
        groups.append({
            "key": key,
            "start": rows[0][0],
            "end": rows[-1][0],
            "observations": len(rows),
            "return_pct": _compound(r for _d, r in rows) * 100.0,
            # The first group began before coverage did; the last is still
            # running. Neither is comparable with a complete period.
            "partial": idx == 0 or idx == len(keys) - 1,
        })
    return groups


def _label_week(key: tuple[int, int]) -> str:
    return f"{key[0]}-W{key[1]:02d}"


def _label_month(key: tuple[int, int]) -> str:
    return f"{MONTH_LABELS[key[1] - 1]} {key[0]}"


def _records(groups: Sequence[dict[str, Any]], labeller) -> dict[str, Any]:
    """Best/worst plus distribution, over complete groups only."""
    complete = [g for g in groups if not g["partial"]]
    payload: dict[str, Any] = {
        "total": len(groups),
        "complete": len(complete),
        "partial_excluded": len(groups) - len(complete),
    }

    if not complete:
        payload.update({
            "best": None, "worst": None, "positive": 0, "negative": 0,
            "flat": 0, "hit_rate_pct": None, "average_pct": None,
            "null_reason": "no_complete_periods",
        })
        return payload

    best = max(complete, key=lambda g: g["return_pct"])
    worst = min(complete, key=lambda g: g["return_pct"])
    positive = [g for g in complete if g["return_pct"] > 0]
    negative = [g for g in complete if g["return_pct"] < 0]
    decided = len(positive) + len(negative)

    def entry(g: dict[str, Any]) -> dict[str, Any]:
        return {
            "label": labeller(g["key"]),
            "return_pct": g["return_pct"],
            "start": g["start"].isoformat(),
            "end": g["end"].isoformat(),
            "observations": g["observations"],
        }

    payload.update({
        "best": entry(best),
        "worst": entry(worst),
        "positive": len(positive),
        "negative": len(negative),
        "flat": len(complete) - decided,
        # Flat periods are excluded from the denominator, matching how
        # trade_quality treats breakeven trades.
        "hit_rate_pct": (len(positive) / decided * 100.0) if decided else None,
        "average_pct": sum(g["return_pct"] for g in complete) / len(complete),
        "best_average_pct": (
            sum(g["return_pct"] for g in positive) / len(positive) if positive else None
        ),
        "worst_average_pct": (
            sum(g["return_pct"] for g in negative) / len(negative) if negative else None
        ),
    })
    return payload


def _streaks(dailies: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """Longest and current runs of up/down days.

    A flat day breaks a streak rather than extending it — a run of gains
    interrupted by an unchanged day is two runs, not one.
    """
    if not dailies:
        return {
            "longest_up": None, "longest_down": None, "current": None,
            "null_reason": "no_observations",
        }

    runs: list[dict[str, Any]] = []
    for day, r in dailies:
        direction = "up" if r > 0 else "down" if r < 0 else "flat"
        if runs and runs[-1]["direction"] == direction:
            runs[-1]["days"] += 1
            runs[-1]["end"] = day
            runs[-1]["return_pct"] = (
                (1 + runs[-1]["return_pct"] / 100.0) * (1 + r) - 1
            ) * 100.0
        else:
            runs.append({
                "direction": direction, "days": 1,
                "start": day, "end": day, "return_pct": r * 100.0,
            })

    def fmt(run: dict[str, Any] | None) -> dict[str, Any] | None:
        if run is None:
            return None
        return {
            "direction": run["direction"],
            "days": run["days"],
            "start": run["start"].isoformat(),
            "end": run["end"].isoformat(),
            "return_pct": run["return_pct"],
        }

    ups = [r for r in runs if r["direction"] == "up"]
    downs = [r for r in runs if r["direction"] == "down"]
    return {
        "longest_up": fmt(max(ups, key=lambda r: r["days"]) if ups else None),
        "longest_down": fmt(max(downs, key=lambda r: r["days"]) if downs else None),
        "current": fmt(runs[-1]),
    }


def _monthly_table(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Calendar grid of monthly returns, one row per year.

    Months with no observations are null rather than zero — the portfolio did
    not return 0% in a month it was not being priced.
    """
    by_year: dict[int, list[float | None]] = {}
    partial_keys = {g["key"] for g in groups if g["partial"]}
    for g in groups:
        year, month = g["key"]
        by_year.setdefault(year, [None] * 12)[month - 1] = g["return_pct"]

    rows = []
    for year in sorted(by_year):
        months = by_year[year]
        present = [m for m in months if m is not None]
        rows.append({
            "year": year,
            "months": months,
            # Compounded across the months actually observed in this year, so
            # it is a part-year figure whenever coverage is partial.
            "year_pct": _compound(m / 100.0 for m in present) * 100.0 if present else None,
            "months_observed": len(present),
            "partial_months": [
                MONTH_LABELS[m - 1] for (y, m) in partial_keys if y == year
            ],
        })
    return {"labels": list(MONTH_LABELS), "rows": rows}


def build(
    curve: Sequence[tuple[date, float]], *, today: date | None = None
) -> dict[str, Any]:
    """Full statistics payload from a TWR growth curve."""
    dailies = daily_returns(curve)
    if not dailies:
        return {
            "ok": False,
            "null_reason": "insufficient_history",
            "observations": len(curve),
        }

    weeks = _group(dailies, _week_key)
    months = _group(dailies, _month_key)
    # Days have no partial concept — every observation is a whole day — so they
    # are grouped by themselves and never excluded.
    day_groups = [
        {"key": d, "start": d, "end": d, "observations": 1,
         "return_pct": r * 100.0, "partial": False}
        for d, r in dailies
    ]

    return {
        "ok": True,
        "basis": "time_weighted_return",
        "note": (
            "Computed from the time-weighted return curve, so contributions "
            "and withdrawals do not register as performance. Partial first and "
            "last periods are excluded from the records."
        ),
        "coverage": {
            "start": dailies[0][0].isoformat(),
            "end": dailies[-1][0].isoformat(),
            "days": len(dailies),
        },
        "day": _records(day_groups, lambda k: k.isoformat()),
        "week": _records(weeks, _label_week),
        "month": _records(months, _label_month),
        "streaks": _streaks(dailies),
        "monthly_table": _monthly_table(months),
    }
