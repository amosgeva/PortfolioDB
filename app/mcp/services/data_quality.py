"""Per-symbol data-quality diagnostics.

`get_health` reports whether the database is reachable, what the last snapshot
run did, and how fresh the FD tables are. None of that is per-symbol, so the
most consequential silent failure in the system is invisible over MCP: the FIFO
engine truncates a SELL that exceeds its open BUYs and only writes a *log
warning*. The realized P&L is then wrong and every endpoint reports it without
comment.

This module answers "can I trust each number I am about to read, and if not,
exactly which one and why".

**Staleness is measured against the collector's own runs, not the wall clock.**
A symbol is stale when a snapshot run completed successfully but that symbol got
no price in it. The tempting alternative — an age threshold in hours — cannot
work here: the measured weekend gap is 64.2 hours, every weekend (Friday 23:13
to Monday 15:23 reporting-local), so any threshold below that fires on every held
symbol every Monday morning, and any threshold above it cannot detect a
collector that died mid-week for two days. Comparing against `snapshot_runs`
sidesteps calendars, holidays and DST entirely: no run, nothing to be stale
against. Collector liveness is then a separate portfolio-level question, which is
where it belongs.

Scope: only symbols the collector actually targets — held (open quantity) or
watchlisted. The other 23 instruments in `instruments` are closed positions the
collector deliberately stopped snapshotting; their prices are up to 295 days old
by design, and reporting them would bury every real finding.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from app.mcp.deps import get_conn
from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import positions as positions_service
from app.mcp.services.cutoff import Cutoff, REPORTING_TZ

import corporate_actions

# Worst-wins ordering. `inconsistent` outranks `unavailable` because
# contradictory data yields a confidently wrong number, while missing data at
# least fails visibly.
STATUS_ORDER = ("complete", "partial", "stale", "unavailable", "inconsistent")
_RANK = {s: i for i, s in enumerate(STATUS_ORDER)}

# Issues that mean a reported number is *wrong* rather than merely absent or
# old. These count toward the overall status at any position size: an orphaned
# sell on a 0.1% holding still corrupts realized P&L. Freshness and
# completeness issues only escalate when the position is material, so one
# unclassified tiny holding cannot peg the portfolio at "partial" forever.
CORRECTNESS_CODES = frozenset({
    "orphan_sell",
    "missing_cost_basis",
    "suspected_split",
    "impossible_value",
})

# A position at or above this share of market value is material.
DEFAULT_MATERIALITY_PCT = 2.0

# Cash is entered by hand, so it goes stale on a human timescale.
DEFAULT_CASH_MAX_AGE_DAYS = 14

# Two same-day, same-quantity lots count as a suspected duplicate only when
# their prices agree this closely (0.1%). Wider than a rounding step, far
# narrower than the spread between two genuine fills of one order.
DUPLICATE_PRICE_TOLERANCE = 0.001


def worst(statuses: list[str]) -> str:
    """Highest-ranked status, or 'complete' for an empty list."""
    return max(statuses, key=lambda s: _RANK[s]) if statuses else "complete"


def portfolio_data_quality(
    *,
    cutoff: Cutoff | None = None,
    method: str = "fifo",
    materiality_pct: float = DEFAULT_MATERIALITY_PCT,
    cash_max_age_days: int = DEFAULT_CASH_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Machine-readable status plus a human-readable explanation per symbol.

    Returns {meta, overall_status, collector, counts, symbols, material_issues,
    minor_issues}. `overall_status` never reads 'complete' while a material
    holding has stale or missing data, or while any holding has a correctness
    issue.
    """
    cutoff = cutoff or cutoff_service.resolve()

    targeted = _targeted_symbols(cutoff)
    positions = {
        p["symbol"]: p
        for p in positions_service.current_positions(
            method, held_only=False, cutoff=cutoff
        )
    }
    last_run = _last_successful_run(cutoff)
    priced_in_last_run = _symbols_priced_at(last_run["ts_start"]) if last_run else set()
    classification = _classification_gaps(sorted(targeted))
    orphans = _orphan_sells(cutoff)
    duplicates = _suspect_duplicate_lots(cutoff)
    impossible = _impossible_values(cutoff)
    first_trade = _first_trade_dates(cutoff)
    splits = _suspected_splits(cutoff)

    issues_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for sym in sorted(targeted):
        pos = positions.get(sym, {})
        held = float(pos.get("qty") or 0.0) > 0
        seen = cutoff.price_ts(sym)

        if seen is None:
            issues_by_symbol[sym].append(_issue(
                "missing_price", "unavailable",
                "No price snapshot at or before the cutoff, so market value "
                "and unrealized P&L cannot be computed.",
            ))
        elif last_run and sym not in priced_in_last_run:
            age_h = (cutoff.ts - seen).total_seconds() / 3600.0
            issues_by_symbol[sym].append(_issue(
                "stale_price", "stale",
                f"Snapshot run {last_run['id']} completed but returned no price "
                f"for this symbol; the one in use is {age_h:.1f}h old.",
                last_price_ts=seen.isoformat(),
                last_run_id=last_run["id"],
                last_run_ts=last_run["ts_start"].isoformat(),
            ))

        if held and seen is not None and not float(pos.get("open_cost") or 0.0):
            issues_by_symbol[sym].append(_issue(
                "missing_cost_basis", "inconsistent",
                "Open quantity with zero cost basis — unrealized P&L would be "
                "reported as the full market value.",
            ))

        missing_fields = classification.get(sym, [])
        if missing_fields:
            issues_by_symbol[sym].append(_issue(
                "missing_classification", "partial",
                f"Missing {', '.join(missing_fields)}; this symbol falls into "
                f"the 'Unknown' bucket of the corresponding allocation view.",
                fields=missing_fields,
            ))

        if held and cutoff.coverage_start and sym in first_trade:
            if first_trade[sym] < cutoff.coverage_start:
                issues_by_symbol[sym].append(_issue(
                    "partial_history", "partial",
                    f"First traded {first_trade[sym].isoformat()} but price "
                    f"coverage starts {cutoff.coverage_start.isoformat()}; no "
                    f"return or drawdown figure can span the gap.",
                    first_trade=first_trade[sym].isoformat(),
                    coverage_start=cutoff.coverage_start.isoformat(),
                ))

    # Ledger-level checks, reported against whichever symbol they concern —
    # including symbols the collector no longer targets, because a broken lot
    # history still corrupts realized P&L.
    for sym, detail in orphans.items():
        issues_by_symbol[sym].append(_issue(
            "orphan_sell", "inconsistent",
            f"SELL quantity exceeds matched BUYs in account "
            f"{detail['account'] or '(none)'} on "
            f"{detail['trade_date'].isoformat()} — the FIFO engine truncates "
            f"the excess with only a log warning, so realized P&L is understated.",
            **{k: (v.isoformat() if hasattr(v, "isoformat") else v)
               for k, v in detail.items()},
        ))

    for sym, detail in duplicates.items():
        issues_by_symbol[sym].append(_issue(
            "possible_duplicate_lot", "partial",
            f"{detail['count']} lots share symbol/account/side/date/quantity at "
            f"near-identical prices ({detail['prices']}) — plausible double "
            f"entry that the lots_dedupe_idx guard cannot catch.",
            **detail,
        ))

    for sym, detail in impossible.items():
        detail = dict(detail)
        issues_by_symbol[sym].append(_issue(
            "impossible_value", "inconsistent", detail.pop("message"), **detail,
        ))

    for sym, detail in splits.items():
        issues_by_symbol[sym].append(_issue(
            "suspected_split", "inconsistent",
            f"Unexplained {detail['observed_ratio']:.4f}:1 price step on "
            f"{detail['day']} with no corporate_actions row. If it is a split, "
            f"returns and drawdown across that date are wrong. Confirm against "
            f"a real source, then record it — see app/check_splits.py.",
            **detail,
        ))

    symbols_out, material, minor = _assemble(
        issues_by_symbol, positions, targeted, materiality_pct
    )
    collector = _collector_status(cutoff, last_run, cash_max_age_days)

    candidates = [i["severity"] for i in material]
    candidates += [collector["status"]]
    overall = worst(candidates)

    return {
        "meta": cutoff_service.meta(
            cutoff, method=method, materiality_pct=materiality_pct
        ),
        "overall_status": overall,
        "overall_explanation": _explain(overall, material, minor, collector),
        "collector": collector,
        "counts": {
            "symbols_checked": len(symbols_out),
            "symbols_complete": sum(
                1 for s in symbols_out if s["status"] == "complete"
            ),
            "material_issues": len(material),
            "minor_issues": len(minor),
        },
        "symbols": symbols_out,
        "material_issues": material,
        "minor_issues": minor,
    }


# ────────────────────────── assembly ──────────────────────────


def _issue(code: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    if severity not in _RANK:
        raise ValueError(f"severity must be one of {STATUS_ORDER}")
    out = {"code": code, "severity": severity, "message": message}
    out.update({k: v for k, v in extra.items() if k not in out})
    return out


def _assemble(
    issues_by_symbol: dict[str, list[dict[str, Any]]],
    positions: dict[str, dict[str, Any]],
    targeted: set[str],
    materiality_pct: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-symbol rows plus the material/minor split.

    A correctness issue is material at any weight; a freshness or completeness
    issue is material only above the weight threshold.
    """
    material: list[dict[str, Any]] = []
    minor: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for sym in sorted(set(targeted) | set(issues_by_symbol)):
        pos = positions.get(sym, {})
        weight = float(pos.get("weight_pct") or 0.0)
        issues = issues_by_symbol.get(sym, [])

        for issue in issues:
            entry = dict(issue, symbol=sym, weight_pct=weight)
            is_material = (
                issue["code"] in CORRECTNESS_CODES or weight >= materiality_pct
            )
            (material if is_material else minor).append(entry)

        rows.append({
            "symbol": sym,
            "status": worst([i["severity"] for i in issues]),
            "held": float(pos.get("qty") or 0.0) > 0,
            "weight_pct": weight,
            "market_value": pos.get("market_value"),
            "issues": issues,
        })

    # Most severe first, then by position size — a reader should hit the
    # thing that matters most before anything else.
    material.sort(key=lambda i: (-_RANK[i["severity"]], -i["weight_pct"], i["symbol"]))
    minor.sort(key=lambda i: (-_RANK[i["severity"]], -i["weight_pct"], i["symbol"]))
    rows.sort(key=lambda r: (-_RANK[r["status"]], -r["weight_pct"], r["symbol"]))
    return rows, material, minor


def _explain(
    overall: str,
    material: list[dict[str, Any]],
    minor: list[dict[str, Any]],
    collector: dict[str, Any],
) -> str:
    if overall == "complete":
        base = "Every targeted symbol has a current price, a cost basis and a consistent lot history."
        if minor:
            return f"{base} {len(minor)} non-material issue(s) noted below."
        return base

    parts: list[str] = []
    if material:
        worst_code = material[0]["code"]
        parts.append(
            f"{len(material)} material issue(s); most severe: {worst_code} on "
            f"{material[0]['symbol']} ({material[0]['weight_pct']:.1f}% of market value)"
        )
    if collector["status"] != "complete":
        parts.append(collector["message"])
    if minor:
        parts.append(f"{len(minor)} non-material issue(s)")
    return ". ".join(parts) + "."


# ────────────────────────── collector ──────────────────────────


def _collector_status(
    cutoff: Cutoff, last_run: dict[str, Any] | None, cash_max_age_days: int
) -> dict[str, Any]:
    """Is the price collector alive, and is the manual cash entry current?

    Judged from `snapshot_runs` rather than from price ages, so a quiet weekend
    reads as quiet rather than as broken.
    """
    issues: list[dict[str, Any]] = []
    status = "complete"

    if last_run is None:
        last_run_payload = None
        if cutoff.price_ts_by_symbol:
            # Prices exist for this cutoff but no run does. `snapshot_runs` was
            # added to the schema after collection had already been running
            # (first run 2026-04-17 against prices from 2025-09-22), so any
            # historical review before that date lands here. The data is fine;
            # what is missing is the means to verify collector liveness — which
            # is 'we cannot check', not 'the collector is down'.
            status = "partial"
            issues.append(_issue(
                "run_history_unavailable", "partial",
                "Prices exist at this cutoff but no snapshot run is recorded "
                "at or before it — run tracking began after collection did, so "
                "collector liveness cannot be verified for this period. "
                "Per-symbol staleness is skipped for the same reason.",
            ))
            message = "No run recorded at this cutoff (run tracking started later)"
        else:
            status = "unavailable"
            issues.append(_issue(
                "collector_never_succeeded", "unavailable",
                "No successful snapshot run and no prices at or before the "
                "cutoff — nothing has been collected.",
            ))
            message = "No successful snapshot run on record"
    else:
        gap_h = (cutoff.ts - last_run["ts_start"]).total_seconds() / 3600.0
        last_run_payload = {
            "id": last_run["id"],
            "ts_start": last_run["ts_start"].isoformat(),
            "status": last_run["status"],
            "symbols_ok": last_run["symbols_ok"],
            "symbols_failed": last_run["symbols_failed"],
            "hours_ago": round(gap_h, 2),
        }
        message = (
            f"Last successful run {last_run['id']} was {gap_h:.1f}h ago "
            f"({last_run['symbols_ok']}/{last_run['symbols_total']} symbols)"
        )
        # 64.2h is the measured Friday-to-Monday gap; past that the collector
        # has missed a session it should have run.
        if gap_h > 72:
            status = "stale"
            issues.append(_issue(
                "collector_silent", "stale",
                f"No successful snapshot run for {gap_h:.1f}h, longer than the "
                f"64.2h weekend gap — the collector has missed a session.",
                hours_since_last_run=round(gap_h, 2),
            ))
        if last_run["status"] == "partial":
            issues.append(_issue(
                "last_run_partial", "partial",
                f"Most recent run reported {last_run['symbols_failed']} "
                f"problem symbol(s): {last_run.get('error') or 'no detail'}",
            ))
            status = worst([status, "partial"])

    stale_accounts = [
        {
            "account": acct,
            "last_ts": ts.isoformat(),
            "days_old": (cutoff.ts - ts).days,
        }
        for acct, ts in sorted(cutoff.cash_ts_by_account.items())
        if (cutoff.ts - ts) > timedelta(days=cash_max_age_days)
    ]
    if stale_accounts:
        issues.append(_issue(
            "stale_cash", "stale",
            "Cash balances are entered by hand and have not been updated for "
            f"more than {cash_max_age_days} days: "
            + ", ".join(f"{a['account']} ({a['days_old']}d)" for a in stale_accounts),
            accounts=stale_accounts,
        ))
        status = worst([status, "stale"])

    return {
        "status": status,
        "message": message,
        "last_successful_run": last_run_payload,
        "issues": issues,
    }


# ────────────────────────── queries ──────────────────────────


def _targeted_symbols(cutoff: Cutoff) -> set[str]:
    """Symbols the collector actually snapshots: open quantity OR watchlisted.

    Mirrors the selection in snapshot_prices.py. Anything else is a closed,
    unwatched position whose stale price is intentional.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH pos AS (
                  SELECT symbol,
                         SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS qty
                  FROM lots
                  WHERE trade_date <= %s
                  GROUP BY symbol
                )
                SELECT i.symbol
                FROM instruments i
                LEFT JOIN pos p ON p.symbol = i.symbol
                WHERE COALESCE(p.qty, 0) > 0 OR i.watchlist = TRUE
                """,
                (cutoff.trade_date,),
            )
            return {r[0] for r in cur.fetchall()}


def _last_successful_run(cutoff: Cutoff) -> dict[str, Any] | None:
    """Most recent run at or before the cutoff that actually wrote prices."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ts_start, status, symbols_total, symbols_ok,
                       symbols_failed, error
                FROM snapshot_runs
                WHERE status IN ('ok', 'partial')
                  AND ts_start <= %s
                ORDER BY ts_start DESC
                LIMIT 1
                """,
                (cutoff.ts,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def _symbols_priced_at(run_ts: datetime) -> set[str]:
    """Symbols with a price row stamped exactly with this run's timestamp.

    The collector stamps every row of a run with the run's start time, so this
    is an exact membership test rather than a window comparison.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM price_snapshots WHERE ts = %s", (run_ts,)
            )
            return {r[0] for r in cur.fetchall()}


def _classification_gaps(symbols: list[str]) -> dict[str, list[str]]:
    """Which of sector / country / asset_type each symbol is missing."""
    if not symbols:
        return {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.symbol,
                       COALESCE(i.sector, f.sector) AS sector,
                       i.country,
                       i.asset_type
                FROM instruments i
                LEFT JOIN fd_company_facts f USING (symbol)
                WHERE i.symbol = ANY(%s)
                """,
                (symbols,),
            )
            rows = cur.fetchall()
    out: dict[str, list[str]] = {}
    for sym, sector, country, asset_type in rows:
        missing = [
            name
            for name, value in (
                ("sector", sector), ("country", country), ("asset_type", asset_type)
            )
            if not value
        ]
        if missing:
            out[sym] = missing
    return out


def _orphan_sells(cutoff: Cutoff) -> dict[str, dict[str, Any]]:
    """First point per symbol where cumulative SELL exceeds cumulative BUY.

    Scoped to (symbol, account) because that is how the engines match. The
    running total is the same walk run_fifo performs, so a hit here is exactly
    the case where it logs a warning and truncates.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ordered AS (
                  SELECT symbol, account, trade_date, id, side, quantity,
                         SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END)
                           OVER (PARTITION BY symbol, COALESCE(account,'')
                                 ORDER BY trade_date, id) AS running
                  FROM lots
                  WHERE trade_date <= %s
                )
                SELECT DISTINCT ON (symbol)
                       symbol, account, trade_date, id, quantity, running
                FROM ordered
                WHERE running < -0.00000001
                ORDER BY symbol, trade_date, id
                """,
                (cutoff.trade_date,),
            )
            return {
                r[0]: {
                    "account": r[1],
                    "trade_date": r[2],
                    "lot_id": int(r[3]),
                    "sell_quantity": float(r[4]),
                    "shortfall": float(r[5]),
                }
                for r in cur.fetchall()
            }


def _suspect_duplicate_lots(cutoff: Cutoff) -> dict[str, dict[str, Any]]:
    """Lots that look like the same trade entered twice.

    `lots_dedupe_idx` already blocks exact repeats, so the residual risk is a
    re-import where the price differs by a rounding step. Two conditions keep
    this from crying wolf on the real ledger:

    1. **Prices must be near-identical** (within DUPLICATE_PRICE_TOLERANCE).
       Splitting an order across fills produces same-day, same-quantity lots at
       genuinely different prices — SPCX sold 1 share at 209.06 and 1 at 198.54
       on 2026-06-16, which is an ordinary two-fill exit, not a duplicate.

    2. **Reversal pairs are excluded.** `lots` is append-only, so the only way
       to correct a mistaken entry is an equal-and-opposite one. VOO on
       2026-03-19 has BUY 0.747 @ 605.01 alongside SELL 0.747 @ 605.01 — a
       correction that already nets to zero. Flagging it would be permanent and
       un-actionable, since the rows cannot be deleted.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH scoped AS (
                  SELECT id, symbol, COALESCE(account,'') AS acct, side,
                         trade_date, quantity, price
                  FROM lots
                  WHERE trade_date <= %s
                ),
                -- A lot with an exact opposite-side twin: same instrument,
                -- account, date, quantity and price. The pair is a correction.
                reversed_pairs AS (
                  SELECT DISTINCT a.id
                  FROM scoped a
                  JOIN scoped b
                    ON a.symbol = b.symbol
                   AND a.acct = b.acct
                   AND a.trade_date = b.trade_date
                   AND a.quantity = b.quantity
                   AND a.price = b.price
                   AND a.side <> b.side
                )
                SELECT symbol, acct, side, trade_date, quantity,
                       COUNT(*) AS n,
                       ARRAY_AGG(price ORDER BY price) AS prices
                FROM scoped
                WHERE id NOT IN (SELECT id FROM reversed_pairs)
                GROUP BY symbol, acct, side, trade_date, quantity
                HAVING COUNT(*) > 1
                   AND (MAX(price) - MIN(price))
                       <= NULLIF(MIN(price), 0) * %s
                ORDER BY symbol
                """,
                (cutoff.trade_date, DUPLICATE_PRICE_TOLERANCE),
            )
            return {
                r[0]: {
                    "account": r[1],
                    "side": r[2],
                    "trade_date": r[3].isoformat(),
                    "quantity": float(r[4]),
                    "count": int(r[5]),
                    "prices": [float(p) for p in r[6]],
                }
                for r in cur.fetchall()
            }


def _impossible_values(cutoff: Cutoff) -> dict[str, dict[str, Any]]:
    """Values the schema permits but that cannot be real.

    The CHECK constraints already exclude negative quantity and price, so what
    remains is a zero price on a BUY (a free acquisition, almost always a
    data-entry slip that silently understates cost basis).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, COUNT(*) AS n, MIN(trade_date) AS first_seen
                FROM lots
                WHERE trade_date <= %s AND price = 0 AND side = 'BUY'
                GROUP BY symbol
                """,
                (cutoff.trade_date,),
            )
            return {
                r[0]: {
                    "message": (
                        f"{int(r[1])} BUY lot(s) recorded at a price of zero "
                        f"(first {r[2].isoformat()}) — cost basis is understated."
                    ),
                    "count": int(r[1]),
                    "first_seen": r[2].isoformat(),
                }
                for r in cur.fetchall()
            }


def _first_trade_dates(cutoff: Cutoff) -> dict[str, date]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, MIN(trade_date)
                FROM lots WHERE trade_date <= %s GROUP BY symbol
                """,
                (cutoff.trade_date,),
            )
            return {r[0]: r[1] for r in cur.fetchall()}


def _suspected_splits(cutoff: Cutoff) -> dict[str, dict[str, Any]]:
    """Unrecorded split-shaped price steps, from the Phase 0 heuristic.

    Only the most recent hit per symbol is reported: the point is to prompt one
    investigation, not to enumerate every day of a corrupted series.
    """
    with get_conn() as conn:
        known = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (symbol, date_trunc('day', ts AT TIME ZONE %s))
                       symbol,
                       date_trunc('day', ts AT TIME ZONE %s)::date AS day_local,
                       last_price
                FROM price_snapshots
                WHERE last_price > 0 AND ts <= %s
                ORDER BY symbol,
                         date_trunc('day', ts AT TIME ZONE %s),
                         ts DESC
                """,
                (REPORTING_TZ, REPORTING_TZ, cutoff.ts, REPORTING_TZ),
            )
            series: dict[str, list[tuple[date, float]]] = defaultdict(list)
            for sym, day, price in cur.fetchall():
                series[sym].append((day, float(price)))

    out: dict[str, dict[str, Any]] = {}
    for hit in corporate_actions.detect_suspected_splits(series, known=known):
        out[hit["symbol"]] = {
            "day": hit["day"].isoformat(),
            "observed_ratio": hit["observed_ratio"],
            "nearest_ratio": hit["nearest_ratio"],
            "prev_price": hit["prev_price"],
            "price": hit["price"],
        }
    return out
