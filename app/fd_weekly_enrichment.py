"""Financial Datasets enrichment block for the Saturday weekly portfolio report.

Requires FINANCIAL_DATASETS_API_KEY in the environment:
  python app/fd_weekly_enrichment.py

The script is intentionally cache-first to protect pay-as-you-go credits.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import fd_store
from db import connect, fetch_all, load_config

BASE_URL = "https://api.financialdatasets.ai"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "financialdatasets"

# ETFs / funds do not have company financial statements in the same useful way.
# Single source of truth lives in fd_store; aliased here so section/billing logic
# and the read/display layer never drift apart.
ETF_SYMBOLS = fd_store.ETF_SYMBOLS

# Pricing from https://financialdatasets.ai/pricing pay-as-you-go table.
COSTS = {
    "facts": 0.00,          # not listed in PAYG table; tracked as zero/unknown until provider clarifies
    "financials": 0.10,     # /financials
    "metrics": 0.04,        # /financial-metrics/snapshot
    "earnings": 0.01,       # /earnings
    "filings": 0.02,        # /filings
    "insiders": 0.04,       # /insider-trades
    "ownership": 0.04,      # /institutional-ownership
    "news": 0.04,           # /news
    "prices": 0.02,         # /prices/snapshot if enabled later
}

TTL_HOURS = {
    "facts": 24 * 7,
    "financials": 24 * 14,
    "metrics": 24 * 7,
    "earnings": 24 * 7,
    "filings": 24 * 7,
    "insiders": 24 * 7,
    "ownership": 24 * 30,
    "news": 24,
}

ENDPOINTS = {
    "facts": ("/company/facts", {"ticker": None}),
    "financials": ("/financials", {"ticker": None, "period": "quarterly", "limit": "4"}),
    "metrics": ("/financial-metrics/snapshot", {"ticker": None}),
    "earnings": ("/earnings", {"ticker": None, "limit": "4"}),
    "filings": ("/filings", {"ticker": None, "limit": "5"}),
    "insiders": ("/insider-trades", {"ticker": None, "limit": "10"}),
    "ownership": ("/institutional-ownership", {"ticker": None, "limit": "10"}),
    "news": ("/news", {"ticker": None, "limit": "5"}),
}

DEFAULT_EQUITY_SECTIONS = ["facts", "financials", "metrics", "earnings", "filings", "insiders", "ownership", "news"]
DEFAULT_ETF_SECTIONS = ["news"]
DAILY_LIGHT_EQUITY_SECTIONS = ["earnings", "filings"]
DAILY_LIGHT_TOP_MOVER_SECTIONS = ["news"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_api_key() -> str | None:
    """Return FINANCIAL_DATASETS_API_KEY from the environment.

    Secret-manager wiring (fetching the key from a vault) belongs in the
    caller's launcher, not here — set the env var however you manage secrets.
    """
    return os.getenv("FINANCIAL_DATASETS_API_KEY") or None


def cache_path(symbol: str, section: str) -> Path:
    return CACHE_DIR / symbol.upper() / f"{section}.json"


def read_cache(symbol: str, section: str) -> tuple[dict[str, Any] | None, bool]:
    path = cache_path(symbol, section)
    if not path.exists():
        return None, False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload.get("fetched_at", "").replace("Z", "+00:00"))
        age_hours = (utcnow() - fetched_at).total_seconds() / 3600
        return payload, age_hours <= TTL_HOURS.get(section, 24)
    except Exception:
        return None, False


def write_cache(symbol: str, section: str, data: dict[str, Any]) -> None:
    path = cache_path(symbol, section)
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = {
        "symbol": symbol.upper(),
        "section": section,
        "fetched_at": utcnow().isoformat().replace("+00:00", "Z"),
        "data": data,
    }
    path.write_text(json.dumps(wrapper, indent=2, sort_keys=True), encoding="utf-8")


def active_symbols_from_db() -> list[str]:
    cfg = load_config()
    with connect(cfg) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT symbol
            FROM lots
            GROUP BY symbol
            HAVING SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) > 0.0000001
            ORDER BY symbol
            """,
        )
    return [r["symbol"].upper() for r in rows]


def top_movers_from_db(limit: int = 3) -> list[str]:
    """Return biggest absolute movers between the last two snapshot timestamps."""
    cfg = load_config()
    with connect(cfg) as conn:
        ts_rows = fetch_all(
            conn,
            """
            SELECT DISTINCT ts
            FROM price_snapshots
            ORDER BY ts DESC
            LIMIT 2
            """,
        )
        if len(ts_rows) < 2:
            return []
        latest_ts = ts_rows[0]["ts"]
        prev_ts = ts_rows[1]["ts"]
        rows = fetch_all(
            conn,
            """
            WITH active AS (
                SELECT symbol, SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS qty
                FROM lots
                GROUP BY symbol
                HAVING SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) > 0.0000001
            ), latest AS (
                SELECT symbol, last_price FROM price_snapshots WHERE ts=%s
            ), prev AS (
                SELECT symbol, last_price FROM price_snapshots WHERE ts=%s
            )
            SELECT active.symbol,
                   active.qty * (latest.last_price - prev.last_price) AS dollar_move,
                   CASE WHEN prev.last_price > 0
                        THEN ((latest.last_price / prev.last_price) - 1) * 100
                        ELSE 0 END AS pct_move
            FROM active
            JOIN latest ON latest.symbol = active.symbol
            JOIN prev ON prev.symbol = active.symbol
            ORDER BY ABS(active.qty * (latest.last_price - prev.last_price)) DESC
            LIMIT %s
            """,
            (latest_ts, prev_ts, limit),
        )
    return [r["symbol"].upper() for r in rows]


# Transient failures worth retrying: rate limit + server-side errors.
# Other 4xx (bad symbol, auth) fail fast — retrying won't change the answer.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_FETCH_ATTEMPTS = 3


def request_json(api_key: str, section: str, symbol: str) -> dict[str, Any]:
    path, params = ENDPOINTS[section]
    qp = {k: (symbol.upper() if v is None else v) for k, v in params.items()}
    url = f"{BASE_URL}{path}?{urlencode(qp)}"
    req = Request(url, headers={"X-API-KEY": api_key, "User-Agent": "PortfolioDB-weekly-enrichment/1.0"})
    last_err: dict[str, Any] = {"_error": "unreachable"}
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            last_err = {"_error": f"HTTP {e.code}", "body": body}
            if e.code not in _RETRYABLE_STATUS:
                return last_err
        except URLError as e:
            last_err = {"_error": f"URL error: {e.reason}"}
        if attempt < _FETCH_ATTEMPTS:
            time.sleep(2 ** attempt)  # 2s, 4s
    return last_err


def sections_for(symbol: str, profile: str = "weekly", top_movers: set[str] | None = None) -> list[str]:
    symbol = symbol.upper()
    if profile == "daily-light":
        sections: list[str] = [] if symbol in ETF_SYMBOLS else list(DAILY_LIGHT_EQUITY_SECTIONS)
        if top_movers and symbol in top_movers:
            sections.extend(DAILY_LIGHT_TOP_MOVER_SECTIONS)
        return sections
    return DEFAULT_ETF_SECTIONS if symbol in ETF_SYMBOLS else DEFAULT_EQUITY_SECTIONS


def build_plan(
    symbols: list[str],
    profile: str = "weekly",
    force_refresh: bool = False,
    max_cost: float | None = None,
    top_movers: set[str] | None = None,
) -> tuple[float, list[tuple[str, str, float, str]]]:
    rows = []
    total = 0.0
    for sym in symbols:
        for section in sections_for(sym, profile=profile, top_movers=top_movers):
            _payload, fresh = read_cache(sym, section)
            status = "cached" if fresh and not force_refresh else "billable"
            cost = 0.0 if status == "cached" else COSTS.get(section, 0.0)
            if max_cost is not None and cost > 0 and total + cost > max_cost:
                rows.append((sym, section, 0.0, "skipped-budget"))
                continue
            total += cost
            rows.append((sym, section, cost, status))
    return total, rows


def estimate_cost(symbols: list[str], force_refresh: bool = False) -> tuple[float, list[tuple[str, str, float, str]]]:
    return build_plan(symbols, profile="weekly", force_refresh=force_refresh)


def safe_num(x: Any) -> str:
    if x is None:
        return "n/a"
    try:
        n = float(x)
        if abs(n) >= 1_000_000_000:
            return f"${n/1_000_000_000:.1f}B"
        if abs(n) >= 1_000_000:
            return f"${n/1_000_000:.1f}M"
        return f"{n:.2f}"
    except Exception:
        return str(x)


def latest_financial_summary(data: dict[str, Any]) -> str | None:
    fin = data.get("financials") if isinstance(data, dict) else None
    if not isinstance(fin, dict):
        return None
    inc = (fin.get("income_statements") or [{}])[0] or {}
    cf = (fin.get("cash_flow_statements") or [{}])[0] or {}
    if not inc:
        return None
    return (
        f"rev {safe_num(inc.get('revenue'))}, "
        f"net income {safe_num(inc.get('net_income'))}, "
        f"FCF {safe_num(cf.get('free_cash_flow'))}, "
        f"period {inc.get('report_period', 'n/a')}"
    )


def metric_summary(data: dict[str, Any]) -> str | None:
    snap = data.get("snapshot") if isinstance(data, dict) else None
    if not isinstance(snap, dict):
        return None
    pe = snap.get("price_to_earnings_ratio")
    ps = snap.get("price_to_sales_ratio")
    ev_ebitda = snap.get("enterprise_value_to_ebitda_ratio")
    roe = snap.get("return_on_equity")
    parts = []
    if pe is not None: parts.append(f"P/E {safe_num(pe)}")
    if ps is not None: parts.append(f"P/S {safe_num(ps)}")
    if ev_ebitda is not None: parts.append(f"EV/EBITDA {safe_num(ev_ebitda)}")
    if roe is not None: parts.append(f"ROE {safe_num(roe)}")
    return ", ".join(parts) if parts else None


def count_list(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        val = data.get(key) if isinstance(data, dict) else None
        if isinstance(val, list):
            return len(val)
    return 0


def format_report(symbols: list[str], fetched: dict[str, dict[str, Any]], total_cost: float, billable: int, cached: int) -> str:
    lines = []
    lines.append("🧠 FINANCIAL DATASETS ENRICHMENT")
    lines.append(f"Symbols checked: {len(symbols)} | billable calls this run: {billable} | cache hits: {cached} | est. cost: ${total_cost:.2f}")
    lines.append("")
    for sym in symbols:
        lines.append(f"• {sym}")
        symdata = fetched.get(sym, {})
        facts = symdata.get("facts", {}).get("company_facts") or symdata.get("facts", {}).get("data", {}).get("company_facts")
        if isinstance(facts, dict):
            name = facts.get("name") or facts.get("company_name")
            sector = facts.get("sector")
            if name or sector:
                lines.append(f"  - Profile: {name or sym} | {sector or 'n/a'}")
        fin = latest_financial_summary(symdata.get("financials", {}))
        if fin:
            lines.append(f"  - Latest financials: {fin}")
        met = metric_summary(symdata.get("metrics", {}))
        if met:
            lines.append(f"  - Valuation/quality: {met}")
        filings = count_list(symdata.get("filings", {}), "filings")
        earnings = count_list(symdata.get("earnings", {}), "earnings")
        insiders = count_list(symdata.get("insiders", {}), "insider_trades")
        owners = count_list(symdata.get("ownership", {}), "institutional_ownership", "holdings")
        news = count_list(symdata.get("news", {}), "news", "articles")
        counts = []
        if filings: counts.append(f"{filings} filings")
        if earnings: counts.append(f"{earnings} earnings rows")
        if insiders: counts.append(f"{insiders} insider trades")
        if owners: counts.append(f"{owners} 13F rows")
        if news: counts.append(f"{news} news items")
        if counts:
            lines.append("  - Fresh context: " + ", ".join(counts))
        errors = [f"{k}: {v.get('_error')}" for k, v in symdata.items() if isinstance(v, dict) and v.get("_error")]
        if errors:
            lines.append("  - API notes: " + "; ".join(errors[:2]))
    lines.append("")
    lines.append("Cost control: cache-first; ETFs/funds get news-only enrichment by default.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Financial Datasets portfolio enrichment")
    parser.add_argument("--symbols", help="Comma-separated symbols. Defaults to active PortfolioDB symbols.")
    parser.add_argument("--profile", choices=["weekly", "daily-light"], default="weekly", help="Enrichment profile.")
    parser.add_argument("--dry-run-cost", action="store_true", help="Only estimate billable calls/cost; no API calls.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cache TTL and refresh all selected sections.")
    parser.add_argument("--max-symbols", type=int, default=0, help="Optional safety limit for tests.")
    parser.add_argument("--max-cost", type=float, default=None, help="Hard billable cost cap for this run, e.g. 0.40.")
    parser.add_argument("--top-movers", type=int, default=3, help="Daily-light: add news for N largest recent movers.")
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between API calls.")
    parser.add_argument("--no-persist", action="store_true", help="Skip writing parsed sections into the fd_* DB tables.")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else active_symbols_from_db()
    symbols = [s for s in symbols if s]
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    top_movers = set(top_movers_from_db(args.top_movers)) if args.profile == "daily-light" and args.top_movers else set()
    estimated, cost_rows = build_plan(
        symbols,
        profile=args.profile,
        force_refresh=args.force_refresh,
        max_cost=args.max_cost,
        top_movers=top_movers,
    )
    if args.dry_run_cost:
        billable = sum(1 for _s, _sec, c, status in cost_rows if status == "billable")
        cached = sum(1 for _s, _sec, c, status in cost_rows if status == "cached")
        print("🧠 FINANCIAL DATASETS COST ESTIMATE")
        print(f"Profile: {args.profile} | Symbols: {', '.join(symbols)}")
        if top_movers:
            print(f"Top movers getting news context: {', '.join(sorted(top_movers))}")
        print(f"Billable calls: {billable} | cache hits: {cached} | estimated cost: ${estimated:.2f}")
        by_sym = {}
        for sym, sec, cost, status in cost_rows:
            by_sym.setdefault(sym, []).append(f"{sec}={'$'+format(cost,'.2f') if cost else status}")
        for sym, parts in by_sym.items():
            print(f"{sym}: " + ", ".join(parts))
        return 0

    api_key = _resolve_api_key()
    if not api_key:
        # Exit 0, not an error: fundamentals enrichment is optional, and this
        # runs on a schedule. A non-zero exit would report a failed job every
        # week on every install that simply never configured the key.
        print(
            "Skipped: FINANCIAL_DATASETS_API_KEY is not set, so fundamentals "
            "enrichment is disabled. No API calls made."
        )
        return 0

    fetched: dict[str, dict[str, Any]] = {}
    billable = 0
    cached = 0
    persisted = 0
    total_cost = 0.0

    db_conn = None
    persistence_failed = False
    if not args.no_persist:
        try:
            db_conn = connect(load_config())
        except Exception as exc:
            print(f"⚠️ fd_store DB connect failed, continuing without persistence: {exc}")
            db_conn = None
            persistence_failed = True

    def _persist(sym: str, section: str, data: dict[str, Any], fetched_at: datetime | None) -> None:
        nonlocal persisted
        if db_conn is None:
            return
        n = fd_store.persist_section(db_conn, sym, section, data, fetched_at=fetched_at)
        persisted += n

    planned = {(sym, sec): (cost, status) for sym, sec, cost, status in cost_rows}
    attempted = 0
    failed = 0
    try:
        for sym in symbols:
            fetched[sym] = {}
            for section in sections_for(sym, profile=args.profile, top_movers=top_movers):
                planned_cost, planned_status = planned.get((sym, section), (0.0, "skipped-budget"))
                if planned_status == "skipped-budget":
                    # A deliberate cap, not a failure — excluded from the counts
                    # the exit code is derived from.
                    fetched[sym][section] = {"_error": "skipped by daily cost cap"}
                    continue
                cached_payload, fresh = read_cache(sym, section)
                if fresh and cached_payload and not args.force_refresh:
                    data = cached_payload.get("data", {})
                    fetched[sym][section] = data
                    cached += 1
                    cached_fetched_at = None
                    try:
                        cached_fetched_at = datetime.fromisoformat(
                            (cached_payload.get("fetched_at") or "").replace("Z", "+00:00")
                        )
                    except Exception:
                        cached_fetched_at = None
                    _persist(sym, section, data, cached_fetched_at)
                    continue
                attempted += 1
                data = request_json(api_key, section, sym)
                fetched[sym][section] = data
                if isinstance(data, dict) and data.get("_error"):
                    # request_json never raises — it retries, then hands back an
                    # {_error: ...} payload. Without counting them here a total
                    # API outage looks exactly like a clean run. Counting only;
                    # the caching and cost accounting below are left as they were.
                    failed += 1
                write_cache(sym, section, data)
                billable += 1
                total_cost += planned_cost
                _persist(sym, section, data, utcnow())
                time.sleep(args.sleep)
    finally:
        if db_conn is not None:
            db_conn.close()

    print(format_report(symbols, fetched, total_cost, billable, cached))
    if not args.no_persist:
        print(f"DB persistence: {persisted} row(s) upserted across fd_* tables.")

    # Exit code contract. This runs on a schedule, so it has to distinguish "the
    # job is broken" from "one symbol was flaky" — a run that goes red on every
    # transient API hiccup gets muted, and then a real outage goes unnoticed too.
    # Non-zero only when the run could not do the thing it was asked to do:
    #   * persistence was requested and the database was unreachable, so nothing
    #     was written and the fd_* tables are silently stale;
    #   * every single fetch failed, which means the key, the network or the
    #     vendor is down rather than one endpoint being unhappy.
    # Partial failures stay 0 and are visible in the report above; the deliberate
    # skips (--dry-run-cost, no API key, cost cap) return 0 further up.
    problems = []
    if persistence_failed:
        problems.append("database unreachable, nothing persisted")
    if attempted and failed == attempted:
        problems.append(f"all {attempted} API call(s) failed")
    if problems:
        print("❌ Enrichment failed: " + "; ".join(problems))
        return 1
    if failed:
        print(f"⚠️ Completed with {failed} of {attempted} API call(s) failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
