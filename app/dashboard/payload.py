"""Dashboard payload composition — pure functions over an open connection.

`build_payload_data` assembles the one JSON payload the embedded front-end
(static/app.js) consumes; `build_fundamentals` builds the Fundamentals view's
per-symbol enrichment. Callers own the connection lifecycle and caching
(streamlit_app.py wraps both in st.cache_data).
"""

from __future__ import annotations

import base64
import logging
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import fd_store
import market_overview
import market_window
import period_stats
import twr
from portfolio import compute_fifo_merged

from . import queries

log = logging.getLogger(__name__)

_LOGO_DIR = Path(__file__).resolve().parent / "static" / "logos"


def _logo_data_uris(symbols) -> dict[str, str]:
    """Self-hosted ticker logos (cached by fetch_ticker_logos.py) as data
    URIs, so the dashboard makes zero third-party requests for symbols we
    have. Missing symbols simply aren't in the map — the browser falls back
    to the CDN."""
    out: dict[str, str] = {}
    for sym in symbols:
        p = _LOGO_DIR / f"{sym}.png"
        if p.is_file():
            out[sym] = "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    return out

TZ_NAME = queries.TZ_NAME
MAX_SERIES_POINTS = 180   # downsample PV ranges to keep the payload small
SPARK_POINTS = 40         # trailing snapshots per symbol for the 30-day spark


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════


def _format_age(fetched_at) -> str:
    if fetched_at is None:
        return "—"
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs / 60)}m ago"
    if secs < 86400 * 2:
        return f"{secs / 3600:.1f}h ago"
    return f"{int(secs / 86400)}d ago"


def _in_snapshot_window(now_local) -> bool:
    """Whether the collector should be running now.

    Delegates to market_window so the freshness warning below can never
    disagree with the guard the collector actually applies — this used to
    restate the 15:15–23:15 rule and drifted from it by construction.
    """
    return market_window.is_open(now_local)


def _clean(v):
    """Recursively coerce DB values (Decimal / date / datetime) to JSON-safe."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _downsample_pairs(pairs: list, cap: int = MAX_SERIES_POINTS) -> list:
    if len(pairs) <= cap:
        return pairs
    step = len(pairs) / cap
    out = [pairs[int(i * step)] for i in range(cap)]
    out[-1] = pairs[-1]
    return out


def _build_risk(price_by_day: dict, qty_map: dict, stocks: dict, held_syms: list) -> dict:
    """Risk & analytics block: beta / vol / Sharpe / max drawdown / correlation.

    Works off the same daily-close buckets the TWR engine uses (last snapshot
    per reporting-local day, forward-filled across gaps). Like the PV chart, the
    portfolio series applies CURRENT quantities to historical prices — a
    price-risk view, not a full backtest. Needs ~20 trading days to activate.
    """
    from math import sqrt

    ANN = sqrt(252)
    MIN_OBS = 20
    syms = [s for s in held_syms if s in stocks and qty_map.get(s)]
    days = sorted(price_by_day.keys())[-380:]
    if not syms or len(days) < 3:
        return {"ok": False, "days": len(days)}

    track = syms + (["SPY"] if "SPY" not in syms else [])
    filled: dict[str, list] = {s: [] for s in track}
    last: dict[str, float | None] = {s: None for s in track}
    for d in days:
        bucket = price_by_day.get(d) or {}
        for s in track:
            if bucket.get(s) is not None:
                last[s] = bucket[s]
            filled[s].append(last[s])

    rets: dict[str, list] = {}
    for s in track:
        seq = filled[s]
        rets[s] = [None] + [
            (seq[i] / seq[i - 1] - 1) if (seq[i] is not None and seq[i - 1]) else None
            for i in range(1, len(seq))
        ]

    def _mean(v):
        return sum(v) / len(v)

    def _cov(xs, ys):
        mx, my = _mean(xs), _mean(ys)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)

    def _pair(a_r, b_r):
        return [(x, y) for x, y in zip(a_r, b_r) if x is not None and y is not None]

    def _vol_sharpe(r_series):
        v = [x for x in r_series if x is not None]
        if len(v) < MIN_OBS:
            return None, None
        sd = sqrt(_cov(v, v))
        if sd == 0:
            return 0.0, None
        return sd * ANN * 100, _mean(v) / sd * ANN

    def _beta(r_series):
        p = _pair(r_series, rets.get("SPY", []))
        if len(p) < MIN_OBS:
            return None
        xs = [a for a, _ in p]
        ys = [b for _, b in p]
        vy = _cov(ys, ys)
        return _cov(xs, ys) / vy if vy else None

    # portfolio value series on the aligned grid (current qty × filled price)
    pvals = [
        sum(qty_map[s] * filled[s][i] for s in syms)
        if all(filled[s][i] is not None for s in syms) else None
        for i in range(len(days))
    ]
    prets = [None] + [
        (pvals[i] / pvals[i - 1] - 1) if (pvals[i] is not None and pvals[i - 1]) else None
        for i in range(1, len(pvals))
    ]
    p_vol, p_sharpe = _vol_sharpe(prets)
    if p_vol is None:
        return {"ok": False, "days": sum(1 for r in prets if r is not None)}

    peak, max_dd = None, 0.0
    for v in pvals:
        if v is None:
            continue
        peak = v if peak is None or v > peak else peak
        if peak:
            max_dd = max(max_dd, (peak - v) / peak)

    # concentration over the invested book (cash excluded)
    mv = {s: qty_map[s] * stocks[s]["price"] for s in syms}
    book = sum(mv.values())
    by_weight = sorted(syms, key=lambda s: mv[s], reverse=True)
    weights = {s: (mv[s] / book * 100 if book else 0.0) for s in syms}

    per_symbol = []
    for s in by_weight:
        s_vol, s_sharpe = _vol_sharpe(rets[s])
        s_beta = _beta(rets[s])
        per_symbol.append({
            "sym": s,
            "weight": round(weights[s], 2),
            "beta": round(s_beta, 2) if s_beta is not None else None,
            "vol": round(s_vol, 1) if s_vol is not None else None,
            "sharpe": round(s_sharpe, 2) if s_sharpe is not None else None,
        })

    corr_syms = by_weight[:8]
    matrix = []
    for a in corr_syms:
        row = []
        for b in corr_syms:
            if a == b:
                row.append(1.0)
                continue
            p = _pair(rets[a], rets[b])
            if len(p) < MIN_OBS:
                row.append(None)
                continue
            xs = [x for x, _ in p]
            ys = [y for _, y in p]
            va, vb = _cov(xs, xs), _cov(ys, ys)
            row.append(round(_cov(xs, ys) / sqrt(va * vb), 2) if va and vb else None)
        matrix.append(row)

    p_beta = _beta(prets)
    return {
        "ok": True,
        "days": sum(1 for r in prets if r is not None),
        "portfolio": {
            "beta": round(p_beta, 2) if p_beta is not None else None,
            "vol": round(p_vol, 1),
            "sharpe": round(p_sharpe, 2) if p_sharpe is not None else None,
            "maxDD": round(max_dd * 100, 1),
        },
        "perSymbol": per_symbol,
        "concentration": {
            "top1": {"sym": by_weight[0], "pct": round(weights[by_weight[0]], 1)} if by_weight else None,
            "top3Pct": round(sum(weights[s] for s in by_weight[:3]), 1),
            "positions": len(syms),
        },
        "corr": {"syms": corr_syms, "m": matrix},
    }


def _safe_url(u) -> str:
    """Only allow http(s) links through — blocks javascript:/data: scheme XSS
    in externally-sourced news URLs before they reach an href."""
    if isinstance(u, str):
        s = u.strip()
        if s[:7].lower() == "http://" or s[:8].lower() == "https://":
            return s
    return ""


# ═══════════════════════════════════════════════════════════
# Main payload
# ═══════════════════════════════════════════════════════════


def build_payload_data(conn, fundamentals_loader) -> dict:
    """Assemble the live PDB-shaped feed consumed by the embedded design.

    `fundamentals_loader` is called with a tuple of symbols and must return
    the fundamentals dict — the Streamlit shell passes its cached
    load_fundamentals so the 900s TTL is preserved.
    """
    latest_rows = queries.latest_prices(conn)
    prev_rows = queries.prev_close(conn)
    lot_rows = queries.all_lots(conn)
    hist_rows = queries.price_history(conn)
    fact_rows = queries.company_facts(conn)
    watch_rows = queries.watchlist_symbols(conn)
    cash_rows = queries.latest_cash_per_account(conn)
    income_rows = queries.income_rows(conn)
    instr_attr_rows = queries.instrument_attrs(conn)
    second_rows = queries.second_latest_prices(conn)
    hist_lot_rows = queries.recent_lots(conn)
    snaplog_rows = queries.snapshot_log(conn)
    snaprun_rows = queries.last_snapshot_run(conn)

    fifo = compute_fifo_merged(lot_rows)

    facts = {r["symbol"]: r for r in fact_rows}
    prev_map = {r["symbol"]: float(r["last_price"]) for r in prev_rows if r.get("last_price") is not None}

    # latest cash balance per account, summed
    cash_latest: dict[str, float] = {}
    for r in cash_rows:
        acct = r["account"] or "(merged)"
        if acct not in cash_latest:
            cash_latest[acct] = float(r["cash"])
    cash = sum(cash_latest.values()) if cash_latest else 0.0

    # trailing price history per symbol (for sparklines)
    sym_hist: dict[str, list[float]] = defaultdict(list)
    for r in hist_rows:
        if r.get("last_price") is not None:
            sym_hist[r["symbol"]].append(float(r["last_price"]))
    sym_hist = {s: v[-SPARK_POINTS:] for s, v in sym_hist.items()}

    # held quantities + average cost
    qty_map: dict[str, float] = {}
    avgcost_map: dict[str, float] = {}
    if not fifo.empty:
        for _, row in fifo.iterrows():
            q = float(row["qty"])
            if q > 0:
                qty_map[row["symbol"]] = q
                avgcost_map[row["symbol"]] = float(row["avg_cost"])

    def sector_for(sym: str) -> str:
        if sym in fd_store.ETF_SYMBOLS:
            return "ETF"
        sec = (facts.get(sym) or {}).get("sector")
        return sec or "Other"

    def name_for(sym: str) -> str:
        nm = (facts.get(sym) or {}).get("name")
        return nm or sym

    # --- universe of stocks (held + watchlist = the actively-snapshotted
    # set). Old price_snapshots rows persist for sold symbols, but those are
    # stale (no recent quote → 0% day change), so we exclude them from
    # movers / breadth / heatmap / ticker. ----------------------------------
    active_syms = set(qty_map) | {r["symbol"] for r in watch_rows}
    stocks: dict[str, dict] = {}
    for r in latest_rows:
        sym = r["symbol"]
        if sym not in active_syms:
            continue
        price = float(r["last_price"]) if r.get("last_price") is not None else None
        if price is None:
            continue
        # No prior-session price (brand-new symbol) → day change is unknown:
        # emit nulls so the UI shows "n/a" instead of a fake flat 0.00%.
        prev = prev_map.get(sym)
        if prev:
            day = round(price - prev, 4)
            day_pct = round(day / prev * 100, 2)
        else:
            prev = None
            day = None
            day_pct = None
        hist = sym_hist.get(sym) or [prev if prev is not None else price, price]
        stocks[sym] = {
            "sym": sym,
            "name": name_for(sym),
            "sector": sector_for(sym),
            "prev": round(prev, 4) if prev is not None else None,
            "price": round(price, 4),
            "day": day,
            "dayPct": day_pct,
            "hist": [round(h, 4) for h in hist],
        }

    # --- holdings (qty > 0, priced) ------------------------------------
    holdings = []
    for sym, qty in qty_map.items():
        if sym in stocks:
            holdings.append({"sym": sym, "qty": qty, "avgCost": round(avgcost_map.get(sym, 0.0), 4)})

    # --- portfolio-value series per range (current qty × hist price) ---
    ts_prices: "OrderedDict[object, dict[str, float]]" = OrderedDict()
    for r in hist_rows:
        if r.get("last_price") is None:
            continue
        ts_prices.setdefault(r["ts"], {})[r["symbol"]] = float(r["last_price"])

    full_series = []  # (ts, value)
    for ts, prices in ts_prices.items():
        val = sum(qty_map.get(s, 0.0) * p for s, p in prices.items())
        if val > 0:
            full_series.append((ts, val))

    jer = ZoneInfo(TZ_NAME)
    today_jer = datetime.now(jer).date()
    now_utc = datetime.now(timezone.utc)

    def series_since(cutoff_days=None, today_only=False) -> list:
        # [epoch_ms, value] pairs so the chart can show date/value on hover.
        out = []
        for ts, val in full_series:
            tsa = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if today_only:
                if tsa.astimezone(jer).date() == today_jer:
                    out.append([int(tsa.timestamp() * 1000), round(val, 2)])
            elif cutoff_days is None or tsa >= now_utc - timedelta(days=cutoff_days):
                out.append([int(tsa.timestamp() * 1000), round(val, 2)])
        return out

    pv = {
        "1D": _downsample_pairs(series_since(today_only=True)),
        "1W": _downsample_pairs(series_since(7)),
        "1M": _downsample_pairs(series_since(30)),
        "1Y": _downsample_pairs(series_since(365)),
    }
    # A narrow window is left EMPTY when it has no points, never backfilled from a
    # wider one. Substituting silently made the chart label lie: before the day's
    # first snapshot — every morning, and all weekend, when the gap is ~64h — "1D"
    # was drawing up to a year of history and reporting its change as the day's.
    # The client picks the narrowest range that actually has data.
    # 1Y is the one exception: it stands in for "everything", so an install whose
    # history predates the window still has a chart to show.
    if len(pv["1Y"]) < 2:
        pv["1Y"] = _downsample_pairs(series_since(None))

    # --- ticker tape + rail watchlist symbols --------------------------
    held_syms = [h["sym"] for h in sorted(
        holdings, key=lambda h: qty_map.get(h["sym"], 0) * stocks[h["sym"]]["price"], reverse=True
    )]
    watch_syms = [r["symbol"] for r in watch_rows if r["symbol"] in stocks]
    tape_syms = []
    for s in held_syms + watch_syms + list(stocks.keys()):
        if s not in tape_syms:
            tape_syms.append(s)
    tape_syms = tape_syms[:14]
    rail_watch = (watch_syms or held_syms)[:8]

    # --- market overview (benchmarks) ----------------------------------
    # Read through its own module and its own query, deliberately isolated from
    # `stocks`/`holdings` above: benchmarks must not be able to reach allocation,
    # movers, risk or the news universe. They have no lots either, so the P&L
    # engines cannot see them at all.
    try:
        markets = market_overview.overview(conn)
    except Exception:
        # A context strip is never worth failing the whole dashboard for.
        log.exception("market overview unavailable")
        markets = []

    # --- news → alerts/news feed ---------------------------------------
    news_universe = list(dict.fromkeys(held_syms + watch_syms)) or list(stocks.keys())
    news_rows = fd_store.recent_news(conn, news_universe, limit=24) if news_universe else []
    news = []
    for r in news_rows:
        pub = r.get("published_at")
        body = r.get("summary") or r.get("description") or ""
        news.append({
            "sym": r.get("symbol") or "",
            "title": r.get("title") or "—",
            "body": body[:280] if isinstance(body, str) else "",
            "src": r.get("source") or "News",
            "time": _format_age(pub),
            "url": _safe_url(r.get("url")),
        })

    # --- KPI roll-ups (portfolio-level, computed server-side) ----------
    second_latest = {r["symbol"]: float(r["last_price"]) for r in second_rows if r.get("last_price") is not None}
    total_market = sum(qty_map[s] * stocks[s]["price"] for s in qty_map if s in stocks)
    cost_basis = sum(avgcost_map.get(s, 0.0) * qty_map[s] for s in qty_map)
    unrealized = total_market - cost_basis
    realized = float(fifo["realized_pnl"].sum()) if not fifo.empty else 0.0
    # Day-change roll-ups only over symbols with a known prior close.
    prev_value = sum(
        qty_map[s] * stocks[s]["prev"]
        for s in qty_map if s in stocks and stocks[s]["prev"] is not None
    )
    day_change = sum(
        qty_map[s] * (stocks[s]["price"] - stocks[s]["prev"])
        for s in qty_map if s in stocks and stocks[s]["prev"] is not None
    )
    delta_last = sum(
        qty_map[s] * (stocks[s]["price"] - second_latest[s])
        for s in qty_map if s in stocks and s in second_latest
    )
    # Total fees paid across the whole ledger (already folded into basis /
    # proceeds — this is a reporting figure, not a P&L adjustment).
    total_fees = sum(float(r["fees"]) for r in lot_rows if r.get("fees") is not None)
    # Income (dividends/interest) — a SEPARATE additive return term; the
    # existing total-return % stays unchanged.
    total_income = sum(float(r["amount"]) for r in income_rows if r.get("amount") is not None)
    ttm_cut = today_jer - timedelta(days=365)
    ttm_income = sum(
        float(r["amount"]) for r in income_rows
        if r.get("amount") is not None and r.get("pay_date") and r["pay_date"] >= ttm_cut
    )
    kpi = {
        "totalValue": round(total_market + cash, 2),
        "marketValue": round(total_market, 2),
        "cash": round(cash, 2),
        "costBasis": round(cost_basis, 2),
        "dayChange": round(day_change, 2),
        "dayChangePct": round(day_change / prev_value * 100, 2) if prev_value else 0.0,
        "unrealized": round(unrealized, 2),
        "unrealizedPct": round(unrealized / cost_basis * 100, 2) if cost_basis else 0.0,
        "realized": round(realized, 2),
        "totalReturnPct": round((realized + unrealized) / cost_basis * 100, 2) if cost_basis else 0.0,
        "deltaLast": round(delta_last, 2),
        "activeCount": len(holdings),
        "watchlistCount": len(watch_rows),
        "totalFees": round(total_fees, 2),
        "feeDragPct": round(total_fees / cost_basis * 100, 2) if cost_basis else 0.0,
        "dividends": round(total_income, 2),
        "dividendsTtm": round(ttm_income, 2),
        "yieldOnCostPct": round(ttm_income / cost_basis * 100, 2) if cost_basis else 0.0,
        "totalReturnWithIncomePct": round(
            (realized + unrealized + total_income) / cost_basis * 100, 2
        ) if cost_basis else 0.0,
    }

    # --- multi-period TIME-WEIGHTED returns + SPY benchmark ------------
    # TWR reconstructs historical holdings and neutralises deposits/trade
    # timing, so a contribution is not mistaken for a gain. See app/twr.py.
    price_by_day: dict = {}
    for _ts, _pr in ts_prices.items():
        _tsa = _ts if _ts.tzinfo else _ts.replace(tzinfo=timezone.utc)
        _bucket = price_by_day.setdefault(_tsa.astimezone(jer).date(), {})
        for _s, _p in _pr.items():
            _bucket[_s] = _p

    _twr_lots = [
        {"symbol": r["symbol"], "side": r["side"], "trade_date": r["trade_date"],
         "quantity": float(r["quantity"]), "price": float(r["price"]),
         "fees": float(r["fees"] or 0)}
        for r in lot_rows
    ]
    _twr_divs = [
        {"pay_date": r["pay_date"], "amount": float(r["amount"])}
        for r in income_rows if r.get("pay_date") and r.get("amount") is not None
    ]
    _twr_records = twr.build_daily_records(_twr_lots, price_by_day, _twr_divs)
    pv_ret = twr.period_returns(_twr_records, today_jer)
    # Period statistics ride the same growth curve as the returns strip above,
    # so "best month" and the MTD figure can never disagree.
    stats_block = period_stats.build(twr.growth_curve(_twr_records), today=today_jer)
    spy_ret = twr.period_returns(twr.benchmark_records(price_by_day, "SPY"), today_jer)
    returns_strip = {
        "basis": "Time-weighted return (neutralizes deposits & trade timing).",
        "benchmark": "SPY (price return, excl. its dividends)",
        "periods": [
            {"period": p, "portfolio": pv_ret.get(p), "benchmark": spy_ret.get(p)}
            for p in ("1D", "WTD", "MTD", "YTD", "1Y", "MAX")
        ],
    }

    # --- allocation by dimension (market-value weights) ----------------
    # All three dims read off `instruments` (enriched by enrich_instruments.py);
    # fd_company_facts.sector is only a fallback for un-enriched equities.
    attr_map = {r["symbol"]: r for r in instr_attr_rows}
    fd_sector_map = {r["symbol"]: r.get("sector") for r in fact_rows}

    def _sector_of(sym: str) -> str | None:
        return (attr_map.get(sym) or {}).get("sector") or fd_sector_map.get(sym) or sector_for(sym)

    def _alloc(dim_fn):
        buckets: dict[str, float] = {}
        for sym, q in qty_map.items():
            if sym not in stocks:
                continue
            mv = q * stocks[sym]["price"]
            if mv <= 0:
                continue
            key = dim_fn(sym) or "Unknown"
            buckets[key] = buckets.get(key, 0.0) + mv
        total = sum(buckets.values())
        return [
            {"key": k, "value": round(v, 2),
             "pct": round(v / total * 100, 2) if total else 0.0}
            for k, v in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)
        ]

    alloc = {
        "sector": _alloc(_sector_of),
        "asset_class": _alloc(lambda s: (attr_map.get(s) or {}).get("asset_type")),
        "currency": _alloc(lambda s: (attr_map.get(s) or {}).get("currency")),
        "region": _alloc(lambda s: (attr_map.get(s) or {}).get("country")),
    }

    # --- risk & analytics (beta / vol / Sharpe / drawdown / correlation)
    risk = _build_risk(price_by_day, qty_map, stocks, held_syms)

    # --- latest prices table -------------------------------------------
    latest_prices = []
    for r in latest_rows:
        tsa = r["ts"]
        latest_prices.append({
            "symbol": r["symbol"],
            "last": float(r["last_price"]) if r.get("last_price") is not None else None,
            "bid": float(r["bid"]) if r.get("bid") is not None else None,
            "ask": float(r["ask"]) if r.get("ask") is not None else None,
            "source": r.get("source") or "—",
            "ts": tsa.astimezone(jer).strftime("%Y-%m-%d %H:%M") if tsa else "—",
        })

    # --- per-symbol price history + trade markers (for the rich chart) -
    chart_syms = set(qty_map) | set(watch_syms) | {"SPY"}
    sym_series: dict[str, list] = defaultdict(list)
    for r in hist_rows:
        if r["symbol"] in chart_syms and r.get("last_price") is not None:
            tsa = r["ts"] if r["ts"].tzinfo else r["ts"].replace(tzinfo=timezone.utc)
            sym_series[r["symbol"]].append([int(tsa.timestamp() * 1000), round(float(r["last_price"]), 4)])
    price_hist = {s: _downsample_pairs(v) for s, v in sym_series.items()}

    sym_lots: dict[str, list] = defaultdict(list)
    for r in lot_rows:
        td = r.get("trade_date")
        sym_lots[r["symbol"]].append({
            "side": r["side"],
            "ts": int(datetime(td.year, td.month, td.day, tzinfo=jer).timestamp() * 1000) if td else None,
            "qty": float(r["quantity"]),
            "price": float(r["price"]),
        })

    chart_sym_options = [s for s in held_syms if s in price_hist] + \
                        [s for s in watch_syms if s in price_hist and s not in qty_map]

    # --- history tables ------------------------------------------------
    hist_lots = [{
        "id": r["id"], "symbol": r["symbol"], "account": r["account"] or "(none)",
        "side": r["side"], "date": r["trade_date"].isoformat() if r.get("trade_date") else "—",
        "qty": float(r["quantity"]), "price": float(r["price"]),
        "fees": float(r["fees"]) if r.get("fees") is not None else 0.0,
        "notes": r.get("notes") or "",
    } for r in hist_lot_rows]
    snap_log = [{
        "ts": r["ts"].astimezone(jer).strftime("%Y-%m-%d %H:%M") if r["ts"] else "—",
        "symbols": int(r["symbols"]), "source": r.get("source") or "—",
    } for r in snaplog_rows]

    # --- last snapshot-run status (ports the old sidebar widget) -------
    snapshot_status = {"text": "No snapshots recorded yet", "level": "none"}
    if snaprun_rows:
        run = snaprun_rows[0]
        now_jer = datetime.now(jer)
        ts_end = run.get("ts_end")
        ts_end_jer = ts_end.astimezone(jer) if ts_end else None
        when = ts_end_jer.strftime("%Y-%m-%d %H:%M") if ts_end_jer else "in progress"
        status = run.get("status")
        if status == "running":
            snapshot_status = {"text": "Snapshot in progress", "level": "info"}
        elif status == "failed":
            snapshot_status = {"text": f"Snapshot failed · {when}", "level": "error"}
        elif status == "partial":
            snapshot_status = {
                "text": f"Partial · {run.get('symbols_ok')}/{run.get('symbols_total')} · {when}",
                "level": "error",
            }
        else:
            level = "ok"
            if ts_end_jer and _in_snapshot_window(now_jer) and (now_jer - ts_end_jer).total_seconds() / 60 > 60:
                level = "warn"
            snapshot_status = {"text": f"Last snapshot {when} · {run.get('symbols_ok') or 0} symbols", "level": level}

    # --- fundamentals (FD enrichment, cached separately) ---------------
    fd_universe = sorted(set(facts.keys()) | set(qty_map.keys()) | set(watch_syms))
    fundamentals = fundamentals_loader(tuple(fd_universe)) if fd_universe else {}
    fd_default = held_syms[0] if (held_syms and held_syms[0] in fundamentals) else (fd_universe[0] if fd_universe else None)

    return {
        "stocks": stocks,
        "reportingTz": TZ_NAME,
        "logos": _logo_data_uris(stocks.keys()),
        "holdings": holdings,
        "cash": round(cash, 2),
        "pv": pv,
        "returns": returns_strip,
        "stats": stats_block,
        "alloc": alloc,
        "risk": risk,
        "markets": markets,
        "news": news,
        "tapeSyms": tape_syms,
        "watchSyms": rail_watch,
        # The engine every number on the shell was computed with. Labelled in
        # the UI because Data Health offers a FIFO/Average selector, and two
        # screens showing a different avg cost with neither saying which is
        # the failure this names away. Sourced here, next to the call.
        "engine": "FIFO",
        "kpi": kpi,
        "latestPrices": latest_prices,
        "priceHist": price_hist,
        "symLots": sym_lots,
        "chartSyms": chart_sym_options,
        "histLots": hist_lots,
        "snapLog": snap_log,
        "snapshot": snapshot_status,
        "fundamentals": fundamentals,
        "fdUniverse": fd_universe,
        "fdDefault": fd_default,
        "updatedAt": now_utc.isoformat(),
        "asOf": datetime.now(jer).strftime("%Y-%m-%d %H:%M"),
    }


# ═══════════════════════════════════════════════════════════
# Fundamentals view
# ═══════════════════════════════════════════════════════════


def build_fundamentals(conn, universe: tuple[str, ...]) -> dict:
    """Per-symbol Financial Datasets enrichment for the Fundamentals view.

    ETFs carry news only (no fundamentals) and are flagged accordingly.
    """
    out: dict[str, dict] = {}
    for sym in universe:
        is_etf = sym in fd_store.ETF_SYMBOLS
        facts = fd_store.latest_facts(conn, sym) or {}
        entry = {
            "name": facts.get("name") or sym,
            "sector": facts.get("sector") or ("ETF" if is_etf else "—"),
            "industry": facts.get("industry") or "—",
            "exchange": facts.get("exchange") or "—",
            "isEtf": is_etf,
            "metrics": None, "trend": [], "earnings": [],
            "filings": [], "insiders": [], "holders": [],
        }
        if is_etf:
            out[sym] = entry
            continue

        metrics = fd_store.latest_metrics(conn, sym)
        if metrics:
            entry["metrics"] = _clean({k: metrics.get(k) for k in (
                "pe_ratio", "ps_ratio", "ev_ebitda", "return_on_equity", "gross_margin",
                "operating_margin", "net_margin", "debt_to_equity", "current_ratio",
                "revenue_growth", "earnings_growth", "free_cash_flow_yield", "market_cap",
            )})

        income = fd_store.recent_financials(conn, sym, "income_statement", limit=8)
        cashflow = fd_store.recent_financials(conn, sym, "cash_flow_statement", limit=8)
        trend: dict[str, dict] = {}
        for r in income:
            p = r.get("report_period")
            if p is None:
                continue
            key = p.isoformat() if hasattr(p, "isoformat") else str(p)
            trend.setdefault(key, {"period": key})
            trend[key]["revenue"] = _clean(r.get("revenue"))
            trend[key]["net_income"] = _clean(r.get("net_income"))
        for r in cashflow:
            p = r.get("report_period")
            if p is None:
                continue
            key = p.isoformat() if hasattr(p, "isoformat") else str(p)
            trend.setdefault(key, {"period": key})
            trend[key]["fcf"] = _clean(r.get("free_cash_flow"))
        entry["trend"] = sorted(trend.values(), key=lambda d: d["period"])[-8:]

        entry["earnings"] = [_clean({
            "period": r.get("fiscal_period") or r.get("report_period"),
            "filing": r.get("filing_date"),
            "eps_actual": r.get("eps_actual"), "eps_estimate": r.get("eps_estimate"),
            "eps_surprise": r.get("eps_surprise"),
            "revenue_actual": r.get("revenue_actual"), "revenue_surprise": r.get("revenue_surprise"),
        }) for r in fd_store.recent_earnings(conn, sym, limit=8)]

        entry["filings"] = [_clean({
            "date": r.get("filing_date"), "type": r.get("filing_type"), "url": _safe_url(r.get("url")),
        }) for r in fd_store.recent_filings(conn, sym, limit=8)]

        entry["insiders"] = [_clean({
            "date": r.get("transaction_date"), "name": r.get("name"),
            "title": r.get("title") or ("Director" if r.get("is_board_director") else "—"),
            "type": r.get("transaction_type"),
            "shares": r.get("transaction_shares"), "price": r.get("transaction_price_per_share"),
            "value": r.get("transaction_value"),
        }) for r in fd_store.recent_insiders(conn, sym, limit=15)]

        entry["holders"] = [_clean({
            "investor": r.get("investor"), "period": r.get("report_period"),
            "shares": r.get("shares"), "value": r.get("market_value"), "price": r.get("price"),
        }) for r in fd_store.top_holders(conn, sym, limit=10)]

        out[sym] = entry
    return out
