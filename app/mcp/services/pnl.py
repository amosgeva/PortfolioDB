"""P&L service — realized and unrealized, breakdowns, method comparison.

Realized P&L is computed by re-running the FIFO/avg engine over a filtered
lot stream. We always group lots by (symbol, account) before feeding them
to the engine because the engine's matching is scoped per pair.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from fifo import Lot as FifoLot, run_fifo
from avg_cost import Lot as AvgLot
from psycopg2 import sql

from app.mcp.deps import get_conn
from app.mcp.services import cutoff as cutoff_service
from app.mcp.services import positions as positions_service
from app.mcp.services.common import is_nan
from app.mcp.services.cutoff import Cutoff


# ────────────────────────── realized ──────────────────────────


def realized_pnl(
    method: str = "fifo",
    *,
    since: date | None = None,
    until: date | None = None,
    group_by: str = "symbol",
    account: str | None = None,
) -> dict[str, Any]:
    """Realized P&L from SELL lots that fall in the [since, until] window.

    NOTE: matching always considers the full BUY history — only the SELL
    side is filtered. This mirrors how a tax report works: a SELL in 2026
    might match a BUY from 2022.

    Args:
        group_by: 'none' = totals only, 'symbol', 'account', 'month'.
    """
    if group_by not in ("none", "symbol", "account", "month"):
        raise ValueError("group_by must be 'none', 'symbol', 'account', or 'month'")
    if method not in ("fifo", "avg", "avg_cost"):
        raise ValueError("method must be 'fifo' or 'avg'")

    matches = _all_realized_matches(method=method, account=account)
    if since is not None:
        matches = [m for m in matches if m["sell_date"] >= since]
    if until is not None:
        matches = [m for m in matches if m["sell_date"] <= until]

    if group_by == "none":
        return {
            "method": method,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "total_realized": sum(m["realized_pnl"] for m in matches),
            "match_count": len(matches),
        }

    buckets: dict[Any, dict[str, Any]] = defaultdict(
        lambda: {"realized_pnl": 0.0, "matches": 0, "buy_cost": 0.0, "sell_proceeds": 0.0}
    )
    for m in matches:
        key = _bucket_key(m, group_by)
        b = buckets[key]
        b["realized_pnl"] += m["realized_pnl"]
        b["buy_cost"] += m["buy_cost"]
        b["sell_proceeds"] += m["sell_proceeds"]
        b["matches"] += 1

    out_rows = []
    for k in sorted(buckets.keys(), key=lambda x: (x is None, x)):
        b = buckets[k]
        out_rows.append({
            "bucket": k,
            "realized_pnl": float(b["realized_pnl"]),
            "buy_cost": float(b["buy_cost"]),
            "sell_proceeds": float(b["sell_proceeds"]),
            "matches": b["matches"],
        })

    return {
        "method": method,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "group_by": group_by,
        "total_realized": float(sum(m["realized_pnl"] for m in matches)),
        "match_count": len(matches),
        "rows": out_rows,
    }


def _bucket_key(m: dict[str, Any], group_by: str) -> Any:
    if group_by == "symbol":
        return m["symbol"]
    if group_by == "account":
        return m["account"] or "(none)"
    if group_by == "month":
        return m["sell_date"].strftime("%Y-%m")
    return None


def _all_realized_matches(
    method: str = "fifo",
    *,
    account: str | None = None,
) -> list[dict[str, Any]]:
    """Replay the engine over every (symbol, account) and emit match lines."""
    # Compose from psycopg2.sql fragments rather than concatenating a raw
    # string; the only user value (account) remains bound via %s.
    query = sql.SQL(
        "SELECT id, symbol, account, side, trade_date, quantity, price, fees "
        "FROM lots WHERE 1=1"
    )
    params: list[Any] = []
    if account is not None:
        query += sql.SQL(" AND account = %s")
        params.append(account)
    query += sql.SQL(" ORDER BY symbol, COALESCE(account,''), trade_date, id")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Group by (symbol, account) and run the engine.
    grouped: dict[tuple[str, str | None], list[Any]] = defaultdict(list)
    if method == "fifo":
        ctor = FifoLot
    else:
        ctor = AvgLot
    for r in rows:
        grouped[(r["symbol"], r["account"])].append(
            ctor(
                id=int(r["id"]),
                symbol=r["symbol"],
                account=r["account"],
                side=r["side"],
                trade_date=r["trade_date"],
                quantity=Decimal(str(r["quantity"])),
                price=Decimal(str(r["price"])),
                fees=Decimal(str(r["fees"])),
            )
        )

    matches_out: list[dict[str, Any]] = []
    if method == "fifo":
        for (sym, acct), stream in grouped.items():
            result = run_fifo(stream)
            # Date lookups so each match knows when it was opened and realized.
            # buy_date drives the holding-period buckets.
            sell_dates = {l.id: l.trade_date for l in stream if l.side.upper() == "SELL"}
            buy_dates = {l.id: l.trade_date for l in stream if l.side.upper() == "BUY"}
            for ml in result.matches:
                buy_date = buy_dates.get(ml.buy_lot_id)
                sell_date = sell_dates[ml.sell_lot_id]
                matches_out.append({
                    "symbol": sym,
                    "account": acct,
                    "sell_lot_id": ml.sell_lot_id,
                    "buy_lot_id": ml.buy_lot_id,
                    "buy_date": buy_date,
                    "sell_date": sell_date,
                    "holding_days": (
                        (sell_date - buy_date).days if buy_date is not None else None
                    ),
                    "qty": float(ml.qty),
                    "buy_cost_ps": float(ml.buy_cost_ps),
                    "sell_proceeds_ps": float(ml.sell_proceeds_ps),
                    "buy_cost": float(ml.buy_cost_ps * ml.qty),
                    "sell_proceeds": float(ml.sell_proceeds_ps * ml.qty),
                    "realized_pnl": float(ml.realized_pnl),
                    # Gross is not derivable from the net figures above, which is
                    # why the engine now carries the per-share fees.
                    "gross_realized_pnl": float(ml.gross_realized_pnl),
                    "fees": float(ml.fees),
                })
    else:
        # Avg-cost engine doesn't emit per-match lines. Synthesize per-SELL
        # rows by replaying with running avg cost.
        for (sym, acct), stream in grouped.items():
            qty = Decimal("0")
            avg = Decimal("0")
            # A second average carried on raw price, ignoring fees. Under
            # avg-cost the buy fee is folded into `avg` and cannot be recovered
            # from it afterwards, so the gross figure has to be accumulated in
            # parallel rather than derived. Keeps
            # gross_realized_pnl - fees == realized_pnl true for this engine too.
            avg_gross = Decimal("0")
            stream_sorted = sorted(stream, key=lambda l: (l.trade_date, l.id))
            for lot in stream_sorted:
                side = lot.side.upper()
                if side == "BUY":
                    new_qty = qty + lot.quantity
                    if new_qty == 0:
                        qty = Decimal("0")
                        avg = Decimal("0")
                        avg_gross = Decimal("0")
                    else:
                        avg = ((qty * avg) + (lot.quantity * lot.net_per_share)) / new_qty
                        avg_gross = (
                            (qty * avg_gross) + (lot.quantity * lot.price)
                        ) / new_qty
                        qty = new_qty
                    continue
                # SELL
                sell_qty = min(lot.quantity, qty)
                if sell_qty <= 0:
                    continue
                proceeds_ps = lot.net_per_share
                realized = (proceeds_ps - avg) * sell_qty
                gross_realized = (lot.price - avg_gross) * sell_qty
                matches_out.append({
                    "symbol": sym,
                    "account": acct,
                    "sell_lot_id": lot.id,
                    "buy_lot_id": None,
                    # Avg-cost pools every purchase, so there is no single buy
                    # date to measure a holding period against. Null rather than
                    # an invented one — see the holding_periods null_reason.
                    "buy_date": None,
                    "sell_date": lot.trade_date,
                    "holding_days": None,
                    "qty": float(sell_qty),
                    "buy_cost_ps": float(avg),
                    "sell_proceeds_ps": float(proceeds_ps),
                    "buy_cost": float(avg * sell_qty),
                    "sell_proceeds": float(proceeds_ps * sell_qty),
                    "realized_pnl": float(realized),
                    "gross_realized_pnl": float(gross_realized),
                    "fees": float(gross_realized - realized),
                })
                qty -= sell_qty
                if qty == 0:
                    avg = Decimal("0")
                    avg_gross = Decimal("0")
    return matches_out


# ────────────────────────── unrealized ──────────────────────────


def unrealized_pnl(
    method: str = "fifo",
    *,
    account: str | None = None,
    as_of: date | None = None,
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Per-symbol unrealized P&L + totals, priced at the cutoff.

    ``as_of`` alone filters lots but still prices at the latest snapshot; a
    ``cutoff`` pins both, which is what makes this reconcile with the other
    services in a composed review.
    """
    df = positions_service.positions_dataframe(
        method, account=account, as_of=as_of, cutoff=cutoff
    )
    held = df[df["qty"] > 0]
    rows = []
    for _, r in held.iterrows():
        cost = float(r["open_cost"])
        upnl = None if is_nan(r["unrealized_pnl"]) else float(r["unrealized_pnl"])
        rows.append({
            "symbol": r["symbol"],
            "qty": float(r["qty"]),
            "open_cost": cost,
            "last_price": None if is_nan(r["last_price"]) else float(r["last_price"]),
            "market_value": None if is_nan(r["market_value"]) else float(r["market_value"]),
            "unrealized_pnl": upnl,
            "unrealized_pct": (upnl / cost * 100.0) if (upnl is not None and cost) else None,
        })
    total = positions_service.positions_summary(
        method, account=account, as_of=as_of, cutoff=cutoff
    )
    return {
        "method": method,
        "rows": rows,
        "total_unrealized": total["unrealized_pnl"],
        "total_cost": total["cost_basis"],
        "total_market_value": total["market_value"],
        "total_unrealized_pct": total["unrealized_pct"],
    }


def pnl_by_symbol(
    method: str = "fifo", *, cutoff: Cutoff | None = None
) -> list[dict[str, Any]]:
    """Combined realized + unrealized per symbol."""
    df = positions_service.positions_dataframe(method, cutoff=cutoff)
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        realized = float(r["realized_pnl"])
        unrealized = (
            0.0 if is_nan(r["unrealized_pnl"]) else float(r["unrealized_pnl"])
        )
        cost = float(r["open_cost"])
        rows.append({
            "symbol": r["symbol"],
            "qty": float(r["qty"]),
            "open_cost": cost,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": realized + unrealized,
            # None, not 0.0: a fully-closed position has no remaining cost to
            # return on, which is a different fact from having broken even.
            "total_return_pct": ((realized + unrealized) / cost * 100.0) if cost else None,
        })
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows


def pnl_summary(
    method: str = "fifo", *, cutoff: Cutoff | None = None
) -> dict[str, Any]:
    s = positions_service.positions_summary(method, cutoff=cutoff)
    return {
        "method": method,
        "market_value": s["market_value"],
        "cost_basis": s["cost_basis"],
        "realized_pnl": s["realized_pnl"],
        "unrealized_pnl": s["unrealized_pnl"],
        "unrealized_pct": s["unrealized_pct"],
        "total_return_pct": s["total_return_pct"],
        "active_symbols": s["active_symbols"],
    }


def compare_methods(
    *, since: date | None = None, until: date | None = None
) -> dict[str, Any]:
    """Side-by-side FIFO vs avg-cost totals."""
    fifo = realized_pnl("fifo", since=since, until=until, group_by="symbol")
    avg = realized_pnl("avg", since=since, until=until, group_by="symbol")
    fifo_by_sym = {r["bucket"]: r["realized_pnl"] for r in fifo["rows"]}
    avg_by_sym = {r["bucket"]: r["realized_pnl"] for r in avg["rows"]}
    symbols = sorted(set(fifo_by_sym) | set(avg_by_sym))
    rows = [
        {
            "symbol": s,
            "fifo_realized": fifo_by_sym.get(s, 0.0),
            "avg_realized": avg_by_sym.get(s, 0.0),
            "diff": fifo_by_sym.get(s, 0.0) - avg_by_sym.get(s, 0.0),
        }
        for s in symbols
    ]
    return {
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "rows": rows,
        "fifo_total": fifo["total_realized"],
        "avg_total": avg["total_realized"],
        "diff_total": fifo["total_realized"] - avg["total_realized"],
    }


# ────────────────────────── trade quality ──────────────────────────

# Holding-period buckets, in days. Boundaries are inclusive-lower,
# exclusive-upper; the last bucket is open-ended.
HOLDING_BUCKETS = (
    ("<1w", 0, 7),
    ("1w-1m", 7, 30),
    ("1m-3m", 30, 90),
    ("3m-1y", 90, 365),
    (">1y", 365, None),
)

TRADE_GROUP_BY = ("none", "symbol", "account", "month", "holding_bucket")


def _holding_bucket(days: int | None) -> str | None:
    if days is None:
        return None
    for label, lo, hi in HOLDING_BUCKETS:
        if days >= lo and (hi is None or days < hi):
            return label
    return None


def trade_quality(
    method: str = "fifo",
    *,
    since: date | None = None,
    until: date | None = None,
    account: str | None = None,
    group_by: str = "none",
    cutoff: Cutoff | None = None,
) -> dict[str, Any]:
    """Realized-trade quality after costs: win rate, payoff, profit factor.

    A **trade** here is one closing transaction — a SELL lot — not a parcel.
    FIFO can split one SELL across several BUY lots, and those parcels can land
    on opposite sides of breakeven; counting them separately would report a win
    rate for events the user never experienced as separate decisions. Parcels
    are still reported as ``match_count``, and holding periods are bucketed per
    parcel because that is the level at which a holding period exists.

    **Fees are not double-counted.** ``realized_pnl`` from this and every other
    endpoint is *already net* of fees: the engines fold BUY fees into cost basis
    and net SELL fees out of proceeds. ``fees`` here is therefore a
    decomposition of that same number, not an additional cost to subtract. The
    identity ``gross_realized_pnl - fees == net_realized_pnl`` is asserted in
    the tests. See docs/methodology.md.

    Args:
        group_by: 'none' | 'symbol' | 'account' | 'month' | 'holding_bucket'.
        since/until: filter by SELL date. As in realized_pnl, only the sell side
            is filtered — the BUY it matches may be far older, which is how a
            tax report reads.
        cutoff: bounds `until` at the cutoff's trade date when `until` is unset.
    """
    if group_by not in TRADE_GROUP_BY:
        raise ValueError(f"group_by must be one of {TRADE_GROUP_BY}")
    if method not in ("fifo", "avg", "avg_cost"):
        raise ValueError("method must be 'fifo' or 'avg'")

    effective_until = until if until is not None else (
        cutoff.trade_date if cutoff else None
    )

    matches = _all_realized_matches(method=method, account=account)
    if since is not None:
        matches = [m for m in matches if m["sell_date"] >= since]
    if effective_until is not None:
        matches = [m for m in matches if m["sell_date"] <= effective_until]

    trades = _aggregate_trades(matches)
    notional = _traded_notional(
        since=since, until=effective_until, account=account
    )

    out: dict[str, Any] = {
        "method": method,
        "since": since.isoformat() if since else None,
        "until": effective_until.isoformat() if effective_until else None,
        "account": account,
        "trade_definition": "one closing SELL lot; parcels reported as match_count",
        **_quality_metrics(trades, matches, notional),
    }
    if cutoff is not None:
        out["meta"] = cutoff_service.meta(cutoff, method=method)

    if group_by != "none":
        out["group_by"] = group_by
        out["rows"] = _grouped_quality(matches, group_by)
    return out


def _aggregate_trades(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse parcels into one row per closing SELL lot."""
    by_sell: dict[Any, dict[str, Any]] = {}
    for m in matches:
        t = by_sell.setdefault(m["sell_lot_id"], {
            "sell_lot_id": m["sell_lot_id"],
            "symbol": m["symbol"],
            "account": m["account"],
            "sell_date": m["sell_date"],
            "qty": 0.0,
            "net": 0.0,
            "gross": 0.0,
            "fees": 0.0,
            "parcels": 0,
        })
        t["qty"] += m["qty"]
        t["net"] += m["realized_pnl"]
        t["gross"] += m["gross_realized_pnl"]
        t["fees"] += m["fees"]
        t["parcels"] += 1
    return list(by_sell.values())


def _quality_metrics(
    trades: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    notional: dict[str, float],
) -> dict[str, Any]:
    """Headline metrics, with nulls where a metric is undefined."""
    gross = sum(t["gross"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    net = sum(t["net"] for t in trades)

    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [t["net"] for t in trades if t["net"] < 0]
    breakeven = [t for t in trades if t["net"] == 0]

    decided = len(wins) + len(losses)
    null_reasons: dict[str, str] = {}

    win_rate = (len(wins) / decided * 100.0) if decided else None
    if win_rate is None:
        null_reasons["win_rate_pct"] = "no_decided_trades"

    average_gain = (sum(wins) / len(wins)) if wins else None
    if average_gain is None:
        null_reasons["average_gain"] = "no_winning_trades"
    # Signed, so the convention matches realized_pnl everywhere else.
    average_loss = (sum(losses) / len(losses)) if losses else None
    if average_loss is None:
        null_reasons["average_loss"] = "no_losing_trades"

    payoff_ratio = (
        (average_gain / abs(average_loss))
        if (average_gain is not None and average_loss)
        else None
    )
    if payoff_ratio is None:
        null_reasons["payoff_ratio"] = (
            "no_losing_trades" if average_gain is not None else "no_winning_trades"
        )

    gross_loss = abs(sum(losses))
    profit_factor = (sum(wins) / gross_loss) if gross_loss else None
    if profit_factor is None:
        null_reasons["profit_factor"] = "no_losing_trades"

    traded = notional["traded_notional"]
    fee_to_notional = (fees / traded * 100.0) if traded else None
    if fee_to_notional is None:
        null_reasons["fee_to_traded_notional_pct"] = "no_traded_notional"

    # Only meaningful against a profit: divided by a gross loss the ratio
    # inverts sign and reads as nonsense, so it is null with a reason instead.
    fee_to_gross = (fees / gross * 100.0) if gross > 0 else None
    if fee_to_gross is None:
        null_reasons["fee_to_gross_profit_pct"] = (
            "no_gross_profit" if trades else "no_trades"
        )

    return {
        "gross_realized_pnl": gross,
        "fees": fees,
        "net_realized_pnl": net,
        "trade_count": len(trades),
        "match_count": len(matches),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(breakeven),
        "win_rate_pct": win_rate,
        "average_gain": average_gain,
        "average_loss": average_loss,
        "payoff_ratio": payoff_ratio,
        "profit_factor": profit_factor,
        "buy_notional": notional["buy_notional"],
        "sell_notional": notional["sell_notional"],
        "traded_notional": notional["traded_notional"],
        "fee_to_traded_notional_pct": fee_to_notional,
        "fee_to_gross_profit_pct": fee_to_gross,
        "holding_periods": _bucket_counts(matches),
        "null_reasons": null_reasons,
    }


def _bucket_counts(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Parcels per holding-period bucket.

    Bucketed per parcel, not per trade: a holding period is the gap between one
    BUY and one SELL, which only exists at parcel level. Avg-cost pools every
    purchase, so it has no buy date and reports unavailable rather than zero.
    """
    if matches and all(m["holding_days"] is None for m in matches):
        return {
            "available": False,
            "null_reason": "avg_cost_pools_purchases_so_no_buy_date",
            "rows": [],
        }

    counts: dict[str, dict[str, Any]] = {
        label: {"bucket": label, "trades": 0, "net_realized_pnl": 0.0, "qty": 0.0}
        for label, _lo, _hi in HOLDING_BUCKETS
    }
    unknown = 0
    for m in matches:
        label = _holding_bucket(m["holding_days"])
        if label is None:
            unknown += 1
            continue
        row = counts[label]
        row["trades"] += 1
        row["net_realized_pnl"] += m["realized_pnl"]
        row["qty"] += m["qty"]

    return {
        "available": True,
        "rows": [counts[label] for label, _lo, _hi in HOLDING_BUCKETS],
        "unknown_holding_period": unknown,
    }


def _grouped_quality(
    matches: list[dict[str, Any]], group_by: str
) -> list[dict[str, Any]]:
    """Per-bucket metrics. Notional is a portfolio-level figure and is not
    apportioned across groups, so the notional fields are dropped from rows
    rather than reported as zero."""
    buckets: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for m in matches:
        if group_by == "holding_bucket":
            key = _holding_bucket(m["holding_days"]) or "unknown"
        else:
            key = _bucket_key(m, group_by)
        buckets[key].append(m)

    zero_notional = {"buy_notional": 0.0, "sell_notional": 0.0, "traded_notional": 0.0}
    rows: list[dict[str, Any]] = []
    for key in sorted(buckets, key=lambda k: (k is None, str(k))):
        group_matches = buckets[key]
        metrics = _quality_metrics(
            _aggregate_trades(group_matches), group_matches, zero_notional
        )
        for field in (
            "buy_notional", "sell_notional", "traded_notional",
            "fee_to_traded_notional_pct", "holding_periods",
        ):
            metrics.pop(field, None)
        metrics["null_reasons"].pop("fee_to_traded_notional_pct", None)
        rows.append({"bucket": key, **metrics})
    rows.sort(key=lambda r: r["net_realized_pnl"], reverse=True)
    return rows


def _traded_notional(
    *, since: date | None, until: date | None, account: str | None
) -> dict[str, float]:
    """Gross notional transacted in the window, both sides.

    Fees are excluded — notional is what changed hands at the traded price, so
    including them would put the fee on both sides of the fee ratio.
    """
    query = sql.SQL("SELECT side, SUM(quantity * price) FROM lots WHERE 1=1")
    params: list[Any] = []
    if since is not None:
        query += sql.SQL(" AND trade_date >= %s")
        params.append(since)
    if until is not None:
        query += sql.SQL(" AND trade_date <= %s")
        params.append(until)
    if account is not None:
        query += sql.SQL(" AND account = %s")
        params.append(account)
    query += sql.SQL(" GROUP BY side")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            by_side = {r[0].upper(): float(r[1] or 0) for r in cur.fetchall()}

    buy = by_side.get("BUY", 0.0)
    sell = by_side.get("SELL", 0.0)
    return {
        "buy_notional": buy,
        "sell_notional": sell,
        "traded_notional": buy + sell,
    }
