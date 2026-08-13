"""Analytics service — concentration, sector allocation, correlation, drawdown.

These tools operate on data already in PortfolioDB (positions + price
snapshots + FD facts). Nothing here calls external APIs; correlation
specifically reads from `price_snapshots` rather than yfinance, which is
the divergence from the archived corr_pairs.py script.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import pandas as pd

from app.mcp.deps import get_conn
from app.mcp.services import common
from app.mcp.services import positions as positions_service
from app.mcp.services import prices as prices_service
from app.mcp.services.cutoff import Cutoff, REPORTING_TZ

# Import deps first (above): it puts app/ on sys.path so these top-level modules
# resolve however the tests are invoked.
import corporate_actions
import holdings as holdings_module


# ────────────────────────── concentration ──────────────────────────


def concentration(top_n: int = 10, *, cutoff: Cutoff | None = None) -> dict[str, Any]:
    """Top-N weights, HHI, and percentage held in the top-N.

    HHI is the Herfindahl-Hirschman Index = Σ wᵢ² over position weights
    expressed as decimals (0-1). 1.0 = single name; 1/N = perfectly equal.
    """
    positions = positions_service.current_positions("fifo", held_only=True, cutoff=cutoff)
    weighted = [
        (p["symbol"], float(p.get("market_value") or 0.0), float(p.get("weight_pct") or 0.0))
        for p in positions
        if p.get("market_value") is not None
    ]
    weighted.sort(key=lambda x: x[1], reverse=True)
    if not weighted:
        # No priced positions: concentration is undefined, not zero. A HHI of
        # 0.0 would read as perfect diversification.
        return {
            "top_n": top_n, "total_positions": 0, "total_market_value": 0.0,
            "top_n_share_pct": None, "single_largest_pct": None,
            "hhi": None, "effective_n": None, "rows": [],
            "null_reasons": {
                f: "no_priced_positions"
                for f in ("top_n_share_pct", "single_largest_pct", "hhi", "effective_n")
            },
        }

    total_mv = sum(mv for _, mv, _ in weighted)
    top = weighted[:top_n]
    top_share_pct = (sum(mv for _, mv, _ in top) / total_mv * 100.0) if total_mv else None
    largest_pct = weighted[0][2]
    weights_decimal = [w / 100.0 for _, _, w in weighted]
    hhi = float(sum(w * w for w in weights_decimal))
    effective_n = (1.0 / hhi) if hhi else None

    null_reasons: dict[str, str] = {}
    if top_share_pct is None:
        null_reasons["top_n_share_pct"] = "no_market_value"
    if effective_n is None:
        null_reasons["effective_n"] = "zero_hhi"

    rows = [
        {"symbol": sym, "market_value": mv, "weight_pct": w}
        for sym, mv, w in top
    ]
    return {
        "top_n": top_n,
        "total_positions": len(weighted),
        "total_market_value": float(total_mv),
        "top_n_share_pct": top_share_pct,
        "single_largest_pct": largest_pct,
        "hhi": hhi,
        "effective_n": effective_n,
        "rows": rows,
        "null_reasons": null_reasons,
    }


# ────────────────────────── sector allocation ──────────────────────────


_ALLOC_DIMENSIONS = ("sector", "asset_class", "currency", "region", "account")

# Symbol-attribute dimensions (each maps a held symbol to a bucket value via a
# fixed literal query). 'account' is special and handled separately.
_ATTRIBUTE_DIMENSIONS = frozenset({"sector", "asset_class", "currency", "region"})


def allocation_by(
    dimension: str = "sector", *, cutoff: Cutoff | None = None
) -> dict[str, Any]:
    """Portfolio market-value weight bucketed by one dimension.

    dimension:
      'sector'      → instruments.sector (fd_company_facts.sector fallback)
      'asset_class' → instruments.asset_type
      'currency'    → instruments.currency
      'region'      → instruments.country
      'account'     → broker account (per-account market value)

    Positions with no value for the dimension roll up under 'Unknown'.
    Returns {dimension, total_market_value, rows:[{key, market_value,
    weight_pct, symbols}]} sorted by market value desc.
    """
    if dimension not in _ALLOC_DIMENSIONS:
        raise ValueError(f"dimension must be one of {_ALLOC_DIMENSIONS}")

    if dimension == "account":
        return _allocation_by_account(cutoff)

    positions = positions_service.current_positions("fifo", held_only=True, cutoff=cutoff)
    if not positions:
        return {"dimension": dimension, "total_market_value": 0.0, "rows": []}

    syms = [p["symbol"] for p in positions]
    attr = _attribute_map(syms, dimension)

    buckets: dict[str, float] = defaultdict(float)
    symbols_in_bucket: dict[str, list[str]] = defaultdict(list)
    total_mv = 0.0
    for p in positions:
        mv = float(p.get("market_value") or 0.0)
        if mv <= 0:
            continue
        key = attr.get(p["symbol"]) or "Unknown"
        buckets[key] += mv
        symbols_in_bucket[key].append(p["symbol"])
        total_mv += mv

    return _alloc_rows(dimension, buckets, symbols_in_bucket, total_mv)


def sector_allocation(*, cutoff: Cutoff | None = None) -> dict[str, Any]:
    """Weight by sector. Backward-compatible wrapper over allocation_by('sector').

    Kept so existing callers (and the get_sector_allocation tool) get the same
    {total_market_value, rows:[{sector, ...}]} shape they always had.
    """
    res = allocation_by("sector", cutoff=cutoff)
    return {
        "total_market_value": res["total_market_value"],
        "rows": [
            {
                "sector": r["key"],
                "market_value": r["market_value"],
                "weight_pct": r["weight_pct"],
                "symbols": r["symbols"],
            }
            for r in res["rows"]
        ],
    }


def _alloc_rows(
    dimension: str,
    buckets: dict[str, float],
    symbols_in_bucket: dict[str, list[str]],
    total_mv: float,
) -> dict[str, Any]:
    rows = []
    for key in sorted(buckets, key=lambda k: buckets[k], reverse=True):
        rows.append({
            "key": key,
            "market_value": buckets[key],
            "weight_pct": (buckets[key] / total_mv * 100.0) if total_mv else 0.0,
            "symbols": sorted(symbols_in_bucket[key]),
        })
    return {"dimension": dimension, "total_market_value": float(total_mv), "rows": rows}


def _attribute_map(symbols: list[str], dimension: str) -> dict[str, str | None]:
    """Per-symbol bucket value for a symbol-attribute dimension (one query).

    Each branch uses a fixed *literal* query — no string building or
    .format() — so there is no SQL-injection surface; only the symbol list is
    bound, via %s.
    """
    if not symbols or dimension not in _ATTRIBUTE_DIMENSIONS:
        return {}
    syms = [s.upper() for s in symbols]
    with get_conn() as conn:
        with conn.cursor() as cur:
            if dimension == "sector":
                # instruments.sector is the populated single source (enriched by
                # enrich_instruments.py); fd_company_facts.sector is the fallback
                # for any un-enriched equity.
                cur.execute(
                    """
                    SELECT i.symbol, COALESCE(i.sector, f.sector) AS sector
                    FROM instruments i
                    LEFT JOIN fd_company_facts f USING (symbol)
                    WHERE i.symbol = ANY(%s)
                    """,
                    (syms,),
                )
            elif dimension == "asset_class":
                cur.execute(
                    "SELECT symbol, asset_type FROM instruments WHERE symbol = ANY(%s)",
                    (syms,),
                )
            elif dimension == "currency":
                cur.execute(
                    "SELECT symbol, currency FROM instruments WHERE symbol = ANY(%s)",
                    (syms,),
                )
            else:  # region
                cur.execute(
                    "SELECT symbol, country FROM instruments WHERE symbol = ANY(%s)",
                    (syms,),
                )
            return {sym: val for sym, val in cur.fetchall()}


def _allocation_by_account(cutoff: Cutoff | None = None) -> dict[str, Any]:
    """Market value per broker account (open positions only).

    Reuses positions_summary per distinct account so the numbers match the
    per-account view elsewhere. Accounts with no current market value drop out.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT account FROM lots")
            accounts = [r[0] for r in cur.fetchall()]

    buckets: dict[str, float] = defaultdict(float)
    total_mv = 0.0
    for acct in accounts:
        summary = positions_service.positions_summary("fifo", account=acct, cutoff=cutoff)
        mv = float(summary.get("market_value") or 0.0)
        if mv <= 0:
            continue
        buckets[acct or "(none)"] += mv
        total_mv += mv

    rows = [
        {
            "key": k,
            "market_value": v,
            "weight_pct": (v / total_mv * 100.0) if total_mv else 0.0,
            "symbols": [],
        }
        for k, v in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {"dimension": "account", "total_market_value": float(total_mv), "rows": rows}


# ────────────────────────── correlation matrix ──────────────────────────


def correlation_matrix(
    symbols: list[str] | None = None,
    window: str = "3m",
    *,
    min_observations: int = 20,
    resample: str = "daily",
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Pairwise correlations of daily returns over a window.

    Uses our own price_snapshots — no yfinance round-trip. Compared to the
    archived corr_pairs.py this gives consistent numbers with what the user
    actually owns and sees.

    Args:
        symbols: subset to correlate. None = currently-held positions.
        window: '1m' | '3m' | '6m' | '1y' | 'all'.
        min_observations: skip pairs with fewer than this many overlapping days.
        resample: 'daily' (default, one observation per reporting-timezone day)
            or 'raw' (every snapshot — usually too granular to be useful).
    """
    if window not in ("1m", "3m", "6m", "1y", "all"):
        raise ValueError("window must be '1m', '3m', '6m', '1y', or 'all'")
    if resample not in ("daily", "raw"):
        raise ValueError("resample must be 'daily' or 'raw'")

    syms = symbols
    if syms is None:
        syms = [p["symbol"] for p in positions_service.current_positions("fifo", held_only=True)]
    syms = sorted({s.upper() for s in syms})
    if len(syms) < 2:
        return {
            "symbols": syms, "window": window, "observations": 0,
            "matrix": {}, "pairs": [], "diversifiers": [], "clusters": {},
        }

    prices_df = _daily_price_frame(syms, window, resample=resample, cutoff=cutoff)
    if prices_df.empty or len(prices_df) < 2:
        return {
            "symbols": syms, "window": window, "observations": 0,
            "matrix": {}, "pairs": [], "diversifiers": [], "clusters": {},
        }

    returns = prices_df.pct_change().dropna(how="all")
    corr = returns.corr(min_periods=min_observations)

    # Build pairs list (upper triangle only).
    pairs: list[dict[str, Any]] = []
    for i, s1 in enumerate(syms):
        for s2 in syms[i + 1:]:
            if s1 not in corr.columns or s2 not in corr.columns:
                continue
            c = corr.loc[s1, s2]
            if pd.isna(c):
                continue
            pairs.append({"a": s1, "b": s2, "correlation": float(c)})
    pairs.sort(key=lambda r: abs(r["correlation"]), reverse=True)

    diversifiers = sorted(pairs, key=lambda r: r["correlation"])[:5]

    # Cluster: any symbol correlated > 0.70 with any other.
    clusters: dict[str, list[str]] = defaultdict(list)
    for p in pairs:
        if p["correlation"] > 0.70:
            clusters[p["a"]].append(p["b"])
            clusters[p["b"]].append(p["a"])
    cluster_out = {k: sorted(set(v)) for k, v in clusters.items()}

    # Matrix as nested dicts so JSON serialization is straightforward.
    matrix_out: dict[str, dict[str, float | None]] = {}
    for s1 in syms:
        row: dict[str, float | None] = {}
        for s2 in syms:
            v = corr.loc[s1, s2] if (s1 in corr.index and s2 in corr.columns) else None
            row[s2] = (None if (v is None or pd.isna(v)) else float(v))
        matrix_out[s1] = row

    return {
        "symbols": syms,
        "window": window,
        "observations": int(len(returns)),
        "min_observations": min_observations,
        "matrix": matrix_out,
        "pairs": pairs[:25],          # cap so payload stays reasonable
        "diversifiers": diversifiers,
        "clusters": cluster_out,
    }


def _daily_price_frame(
    symbols: list[str], window: str, resample: str = "daily",
    cutoff: Cutoff | None = None,
) -> pd.DataFrame:
    """Wide DataFrame of last_price per symbol per day, from price_snapshots.

    Split-adjusted: an unadjusted split inside the window would enter the
    returns matrix as a one-day ±50%-scale move and dominate every correlation
    that symbol takes part in.
    """
    since = _window_since(window)
    as_of_ts = cutoff.ts if cutoff else None
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            if resample == "daily":
                cur.execute(
                    """
                    SELECT DISTINCT ON (symbol, date_trunc('day', ts AT TIME ZONE %s))
                        symbol,
                        date_trunc('day', ts AT TIME ZONE %s)::date AS day_local,
                        last_price
                    FROM price_snapshots
                    WHERE symbol = ANY(%s)
                      AND (%s::timestamptz IS NULL OR ts >= %s)
                      AND (%s::timestamptz IS NULL OR ts <= %s)
                    ORDER BY symbol,
                             date_trunc('day', ts AT TIME ZONE %s),
                             ts DESC
                    """,
                    (REPORTING_TZ, REPORTING_TZ, symbols, since, since, as_of_ts, as_of_ts, REPORTING_TZ),
                )
                cols = ["symbol", "day", "last_price"]
            else:
                cur.execute(
                    """
                    SELECT symbol, ts, last_price
                    FROM price_snapshots
                    WHERE symbol = ANY(%s)
                      AND (%s::timestamptz IS NULL OR ts >= %s)
                      AND (%s::timestamptz IS NULL OR ts <= %s)
                    ORDER BY symbol, ts
                    """,
                    (symbols, since, since, as_of_ts, as_of_ts),
                )
                cols = ["symbol", "day", "last_price"]
            rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=symbols)
    # Column order differs from adjust_price_points' (when, symbol, price), so
    # reorder on the way in and back out again.
    adjusted = corporate_actions.adjust_price_points(
        ((day, sym, float(price)) for sym, day, price in rows), actions
    )
    df = pd.DataFrame(
        [(sym, day, price) for day, sym, price in adjusted], columns=cols
    )
    df["last_price"] = pd.to_numeric(df["last_price"], errors="coerce")
    return df.pivot_table(
        index="day", columns="symbol", values="last_price", aggfunc="last"
    ).sort_index()


# Shared with prices — see common.window_start. Module-local name preserved
# (tests monkeypatch analytics._window_since).
_window_since = common.window_start


# ────────────────────────── drawdown ──────────────────────────


def drawdown_stats(
    symbol: str | None = None,
    *,
    since: date | None = None,
    holdings_basis: str = "historical",
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Max-drawdown stats for a symbol or for the whole portfolio.

    symbol=None replays portfolio value over the snapshot history using the
    holdings actually held at each point. Prices are split-adjusted, so a 2:1
    split is no longer recorded as a 50% drawdown.

    holdings_basis: 'historical' (default) or 'current_constant', the previous
    behaviour that held today's quantities constant across all of history —
    retained for comparison only, since it back-projects current positions
    onto a past that did not hold them.

    Returns: {max_drawdown_pct, current_drawdown_pct, peak, peak_ts,
              trough, trough_ts, recovered (bool), holdings_basis}.
    """
    if holdings_basis not in ("historical", "current_constant"):
        raise ValueError("holdings_basis must be 'historical' or 'current_constant'")

    if symbol is None:
        series = _portfolio_value_series(since, holdings_basis=holdings_basis, cutoff=cutoff)
    else:
        series = _symbol_price_series(symbol.upper(), since, cutoff=cutoff)

    if not series:
        return _empty_drawdown(symbol, holdings_basis)

    df = pd.DataFrame(series, columns=["ts", "value"]).set_index("ts")
    df["running_peak"] = df["value"].cummax()
    df["drawdown"] = (df["value"] - df["running_peak"]) / df["running_peak"]

    # Most negative point = the historical max drawdown.
    min_idx = df["drawdown"].idxmin()
    max_dd = float(df["drawdown"].min())  # negative number
    # Peak before that trough.
    peak_idx = df.loc[:min_idx, "running_peak"].idxmax()
    peak_val = float(df.loc[peak_idx, "value"])
    trough_val = float(df.loc[min_idx, "value"])
    # Has the running peak been retaken since the trough?
    after_trough = df.loc[min_idx:]
    recovered = bool((after_trough["value"] >= peak_val).any())

    current_dd = float(df["drawdown"].iloc[-1])

    return {
        "symbol": symbol.upper() if symbol else None,
        "since": since.isoformat() if since else None,
        "holdings_basis": holdings_basis if symbol is None else None,
        "observations": int(len(df)),
        "max_drawdown_pct": max_dd * 100.0,
        "current_drawdown_pct": current_dd * 100.0,
        "peak": peak_val,
        "peak_ts": peak_idx.isoformat() if hasattr(peak_idx, "isoformat") else str(peak_idx),
        "trough": trough_val,
        "trough_ts": min_idx.isoformat() if hasattr(min_idx, "isoformat") else str(min_idx),
        "recovered": recovered,
    }


def _symbol_price_series(
    symbol: str, since: date | None, *, cutoff: Cutoff | None = None
) -> list[tuple[Any, float]]:
    """Split-adjusted price series for one symbol."""
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, last_price
                FROM price_snapshots
                WHERE symbol = %s
                  AND (%s::date IS NULL OR ts >= %s)
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY ts
                """,
                (symbol, since, since,
                 cutoff.ts if cutoff else None, cutoff.ts if cutoff else None),
            )
            raw = [(ts, symbol, float(p)) for ts, p in cur.fetchall()]
    return [
        (ts, price) for ts, _sym, price in corporate_actions.adjust_price_points(raw, actions)
    ]


def _portfolio_value_series(
    since: date | None, *, holdings_basis: str = "historical",
    cutoff: Cutoff | None = None,
) -> list[tuple[Any, float]]:
    """Portfolio value at each snapshot ts, split-adjusted.

    Under the default 'historical' basis the holdings are reconstructed per
    point from the lot ledger, so symbols sold during the window contribute
    only while they were actually held.
    """
    with get_conn() as conn:
        actions = corporate_actions.fetch_actions(conn)
        lot_rows = prices_service._value_lots(conn, actions)
        if not lot_rows:
            return []
        symbols = sorted({r["symbol"] for r in lot_rows})
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, symbol, last_price
                FROM price_snapshots
                WHERE symbol = ANY(%s)
                  AND (%s::date IS NULL OR ts >= %s)
                  AND (%s::timestamptz IS NULL OR ts <= %s)
                ORDER BY ts
                """,
                (symbols, since, since,
                 cutoff.ts if cutoff else None, cutoff.ts if cutoff else None),
            )
            raw = [(ts, sym, float(p)) for ts, sym, p in cur.fetchall()]

    by_ts: dict[Any, dict[str, float]] = defaultdict(dict)
    for ts, sym, price in corporate_actions.adjust_price_points(raw, actions):
        by_ts[ts][sym] = price
    ordered = [(ts, by_ts[ts]) for ts in sorted(by_ts)]

    if holdings_basis == "current_constant":
        qty_map = prices_service._current_quantities()
        return [
            (ts, sum(qty_map.get(s, 0.0) * p for s, p in prices.items()))
            for ts, prices in ordered
        ]
    return holdings_module.value_series(lot_rows, ordered, carry_forward=True)


def _empty_drawdown(symbol: str | None, holdings_basis: str = "historical") -> dict[str, Any]:
    return {
        "symbol": symbol.upper() if symbol else None,
        "holdings_basis": holdings_basis if symbol is None else None,
        "observations": 0,
        "max_drawdown_pct": 0.0,
        "current_drawdown_pct": 0.0,
        "peak": None,
        "peak_ts": None,
        "trough": None,
        "trough_ts": None,
        "recovered": True,
    }


# ────────────────────────── position weights ──────────────────────────


def position_weights(
    method: str = "fifo", *, cutoff: Cutoff | None = None
) -> list[dict[str, Any]]:
    """Per-symbol weight (% of market value). Lightweight — no other fields."""
    rows = positions_service.current_positions(method, held_only=True, cutoff=cutoff)
    return [
        {"symbol": r["symbol"], "weight_pct": float(r.get("weight_pct") or 0.0)}
        for r in rows
    ]


# ────────────────────────── stress scenarios ──────────────────────────

# Labelled defaults, overridable per call. Round numbers on purpose: these are
# sensitivities, and a spuriously precise assumption would imply a forecast.
DEFAULT_SHOCKS = {
    "largest_holding_decline_pct": 30.0,
    "top_n_decline_pct": 20.0,
    "sector_shock_pct": 25.0,
    "cluster_shock_pct": 20.0,
}

# Correlation above which two symbols are treated as one cluster for the
# correlated-shock scenario. Matches the threshold correlation_matrix uses.
CLUSTER_THRESHOLD = 0.70


def stress_scenarios(
    *,
    cutoff: Cutoff | None = None,
    top_n: int = 3,
    largest_holding_decline_pct: float = DEFAULT_SHOCKS["largest_holding_decline_pct"],
    top_n_decline_pct: float = DEFAULT_SHOCKS["top_n_decline_pct"],
    sector_shock_pct: float = DEFAULT_SHOCKS["sector_shock_pct"],
    cluster_shock_pct: float = DEFAULT_SHOCKS["cluster_shock_pct"],
    correlation_window: str = "3m",
    correlation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic what-if arithmetic on current weights.

    These are **analytical sensitivities, not forecasts**: each result is what
    the portfolio would be worth if the named holdings fell by the stated
    percentage, today, with nothing else moving. No probability is attached, no
    distribution is assumed, and no history is consulted except to identify
    correlated clusters. Every payload is labelled `basis: analytical_derived`
    so this cannot be mistaken for a prediction.

    Concentration is the live risk in a book this size, which is why the
    scenarios are weight-driven rather than volatility-driven.
    """
    positions = [
        p for p in positions_service.current_positions(
            "fifo", held_only=True, cutoff=cutoff
        )
        if p.get("market_value")
    ]
    total_mv = sum(float(p["market_value"]) for p in positions)
    if not positions or total_mv <= 0:
        return {
            "basis": "analytical_derived",
            "total_market_value": 0.0,
            "scenarios": [],
            "null_reason": "no_priced_positions",
        }

    ordered = sorted(positions, key=lambda p: float(p["market_value"]), reverse=True)
    scenarios: list[dict[str, Any]] = []

    # 1. The single largest holding.
    largest = ordered[0]
    scenarios.append(_scenario(
        "largest_holding_decline",
        f"{largest['symbol']} falls {largest_holding_decline_pct:g}%",
        [largest["symbol"]], largest_holding_decline_pct, ordered, total_mv,
    ))

    # 2. The top N together.
    top = ordered[:top_n]
    scenarios.append(_scenario(
        "top_n_decline",
        f"Top {len(top)} holdings each fall {top_n_decline_pct:g}%",
        [p["symbol"] for p in top], top_n_decline_pct, ordered, total_mv,
    ))

    # 3. The most concentrated sector.
    sector_map = _attribute_map([p["symbol"] for p in ordered], "sector")
    by_sector: dict[str, list[str]] = defaultdict(list)
    for p in ordered:
        by_sector[sector_map.get(p["symbol"]) or "Unknown"].append(p["symbol"])
    if by_sector:
        weight_of = {p["symbol"]: float(p["market_value"]) for p in ordered}
        worst_sector = max(
            by_sector, key=lambda s: sum(weight_of[x] for x in by_sector[s])
        )
        scenarios.append(_scenario(
            "sector_shock",
            f"{worst_sector} sector falls {sector_shock_pct:g}%",
            by_sector[worst_sector], sector_shock_pct, ordered, total_mv,
            extra={"sector": worst_sector},
        ))

    # 4. The largest correlated cluster, if there is enough history to find one.
    # Reuse a matrix the caller already built rather than paying for it twice
    # — a review needs the same one for its risk section.
    corr = correlation if correlation is not None else correlation_matrix(
        [p["symbol"] for p in ordered], window=correlation_window, cutoff=cutoff
    )
    clusters = corr.get("clusters") or {}
    if clusters:
        seed = max(clusters, key=lambda k: len(clusters[k]))
        members = sorted({seed, *clusters[seed]})
        scenarios.append(_scenario(
            "correlated_cluster_shock",
            f"Cluster around {seed} ({len(members)} symbols correlated "
            f">{CLUSTER_THRESHOLD:g}) falls {cluster_shock_pct:g}%",
            members, cluster_shock_pct, ordered, total_mv,
            extra={"cluster_seed": seed, "correlation_window": correlation_window},
        ))
    else:
        scenarios.append({
            "key": "correlated_cluster_shock",
            "label": "Correlated cluster shock",
            "status": "unavailable",
            "null_reason": (
                "no_cluster_above_threshold"
                if corr.get("observations") else "insufficient_price_history"
            ),
            "observations": corr.get("observations", 0),
        })

    # Currency shock from the plan is deliberately absent: every instrument is
    # USD, so it would shock 100% of the book and duplicate a total-market move.
    return {
        "basis": "analytical_derived",
        "disclaimer": (
            "Deterministic sensitivities on current weights, not forecasts. "
            "No probability, distribution or correlation of the shock itself is "
            "implied — each line is arithmetic on the stated assumption."
        ),
        "total_market_value": total_mv,
        "assumptions": {
            "largest_holding_decline_pct": largest_holding_decline_pct,
            "top_n_decline_pct": top_n_decline_pct,
            "sector_shock_pct": sector_shock_pct,
            "cluster_shock_pct": cluster_shock_pct,
            "top_n": top_n,
            "cluster_threshold": CLUSTER_THRESHOLD,
        },
        "scenarios": scenarios,
    }


def _scenario(
    key: str,
    label: str,
    symbols: list[str],
    decline_pct: float,
    positions: list[dict[str, Any]],
    total_mv: float,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One shock applied to a named set of symbols."""
    affected = set(symbols)
    shocked_value = sum(
        float(p["market_value"]) for p in positions if p["symbol"] in affected
    )
    loss = shocked_value * (decline_pct / 100.0)
    return {
        "key": key,
        "label": label,
        "status": "ok",
        "symbols": sorted(affected),
        "symbols_affected": len(affected),
        "affected_market_value": shocked_value,
        "affected_weight_pct": (shocked_value / total_mv * 100.0) if total_mv else None,
        "decline_pct": decline_pct,
        "portfolio_loss": -loss,
        "portfolio_loss_pct": -(loss / total_mv * 100.0) if total_mv else None,
        "portfolio_value_after": total_mv - loss,
        **(extra or {}),
    }
