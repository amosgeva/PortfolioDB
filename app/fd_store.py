"""Financial Datasets persistence + read API.

Persistence parsers consume the raw `data` payload returned by FD API calls (the
inner object — i.e. what `fd_weekly_enrichment.request_json()` returns), upsert
extracted hot columns, and stash the full payload as JSONB.

Read helpers (`latest_*`, `recent_*`) hand back plain dicts ready for use by
`streamlit_app.py` and `report_weekly_db.py` without leaking JSONB shape.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Iterable

import psycopg2.extras
from psycopg2.extras import Json

log = logging.getLogger(__name__)

ETF_SYMBOLS = {"GLD", "IAU", "VOO", "QQQ", "SPY", "XLE", "XLK", "IWM", "TLT", "IBIT", "EZBC", "IEFA", "UFO"}

STATEMENT_KEYS = {
    "income_statement": "income_statements",
    "balance_sheet": "balance_sheets",
    "cash_flow_statement": "cash_flow_statements",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_date(val: Any) -> date | None:
    if val is None or val == "":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).date()
    except Exception:
        return None


def _to_datetime(val: Any) -> datetime | None:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _ensure_instrument(conn, symbol: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT(symbol) DO NOTHING",
            (symbol,),
        )


def _has_error(data: Any) -> bool:
    return isinstance(data, dict) and "_error" in data


# ───────────────────────── persisters ─────────────────────────

def persist_facts(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    if _has_error(data):
        return 0
    facts = (data or {}).get("company_facts") or {}
    if not facts:
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fd_company_facts (
                symbol, name, sector, industry, exchange, category,
                cik, location, website, is_active, raw, fetched_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol) DO UPDATE SET
                name = EXCLUDED.name,
                sector = EXCLUDED.sector,
                industry = EXCLUDED.industry,
                exchange = EXCLUDED.exchange,
                category = EXCLUDED.category,
                cik = EXCLUDED.cik,
                location = EXCLUDED.location,
                website = EXCLUDED.website,
                is_active = EXCLUDED.is_active,
                raw = EXCLUDED.raw,
                fetched_at = EXCLUDED.fetched_at
            """,
            (
                symbol,
                facts.get("name"),
                facts.get("sector"),
                facts.get("industry"),
                facts.get("exchange"),
                facts.get("category"),
                facts.get("cik"),
                facts.get("location"),
                facts.get("website") or facts.get("sec_filings_url"),
                facts.get("is_active"),
                Json(data),
                fetched_at,
            ),
        )
    conn.commit()
    return 1


def persist_metrics(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    if _has_error(data):
        return 0
    snap = (data or {}).get("snapshot") or {}
    if not snap:
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fd_financial_metrics (
                symbol, market_cap, enterprise_value, pe_ratio, ps_ratio, pb_ratio,
                ev_ebitda, ev_revenue, peg_ratio, earnings_per_share, book_value_per_share,
                gross_margin, operating_margin, net_margin,
                return_on_equity, return_on_assets, return_on_invested_capital,
                debt_to_equity, debt_to_assets, current_ratio, quick_ratio,
                free_cash_flow_yield, payout_ratio, revenue_growth, earnings_growth,
                raw, fetched_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,
                %s,%s,%s,
                %s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,%s,%s,
                %s,%s
            )
            ON CONFLICT (symbol) DO UPDATE SET
                market_cap = EXCLUDED.market_cap,
                enterprise_value = EXCLUDED.enterprise_value,
                pe_ratio = EXCLUDED.pe_ratio,
                ps_ratio = EXCLUDED.ps_ratio,
                pb_ratio = EXCLUDED.pb_ratio,
                ev_ebitda = EXCLUDED.ev_ebitda,
                ev_revenue = EXCLUDED.ev_revenue,
                peg_ratio = EXCLUDED.peg_ratio,
                earnings_per_share = EXCLUDED.earnings_per_share,
                book_value_per_share = EXCLUDED.book_value_per_share,
                gross_margin = EXCLUDED.gross_margin,
                operating_margin = EXCLUDED.operating_margin,
                net_margin = EXCLUDED.net_margin,
                return_on_equity = EXCLUDED.return_on_equity,
                return_on_assets = EXCLUDED.return_on_assets,
                return_on_invested_capital = EXCLUDED.return_on_invested_capital,
                debt_to_equity = EXCLUDED.debt_to_equity,
                debt_to_assets = EXCLUDED.debt_to_assets,
                current_ratio = EXCLUDED.current_ratio,
                quick_ratio = EXCLUDED.quick_ratio,
                free_cash_flow_yield = EXCLUDED.free_cash_flow_yield,
                payout_ratio = EXCLUDED.payout_ratio,
                revenue_growth = EXCLUDED.revenue_growth,
                earnings_growth = EXCLUDED.earnings_growth,
                raw = EXCLUDED.raw,
                fetched_at = EXCLUDED.fetched_at
            """,
            (
                symbol,
                snap.get("market_cap"),
                snap.get("enterprise_value"),
                snap.get("price_to_earnings_ratio"),
                snap.get("price_to_sales_ratio"),
                snap.get("price_to_book_ratio"),
                snap.get("enterprise_value_to_ebitda_ratio"),
                snap.get("enterprise_value_to_revenue_ratio"),
                snap.get("peg_ratio"),
                snap.get("earnings_per_share"),
                snap.get("book_value_per_share"),
                snap.get("gross_margin"),
                snap.get("operating_margin"),
                snap.get("net_margin"),
                snap.get("return_on_equity"),
                snap.get("return_on_assets"),
                snap.get("return_on_invested_capital"),
                snap.get("debt_to_equity"),
                snap.get("debt_to_assets"),
                snap.get("current_ratio"),
                snap.get("quick_ratio"),
                snap.get("free_cash_flow_yield"),
                snap.get("payout_ratio"),
                snap.get("revenue_growth"),
                snap.get("earnings_growth"),
                Json(data),
                fetched_at,
            ),
        )
    conn.commit()
    return 1


def persist_financials(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    """Persist all three statement types in a single call. Returns total rows upserted."""
    if _has_error(data):
        return 0
    fin = (data or {}).get("financials") or {}
    if not isinstance(fin, dict):
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    inserted = 0
    with conn.cursor() as cur:
        for statement_type, list_key in STATEMENT_KEYS.items():
            for row in fin.get(list_key) or []:
                accession = row.get("accession_number")
                if not accession:
                    continue
                cur.execute(
                    """
                    INSERT INTO fd_financial_statements (
                        symbol, statement_type, accession_number, period, report_period,
                        fiscal_period, currency, revenue, net_income, free_cash_flow,
                        total_assets, shareholders_equity, raw, fetched_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, statement_type, accession_number) DO UPDATE SET
                        period = EXCLUDED.period,
                        report_period = EXCLUDED.report_period,
                        fiscal_period = EXCLUDED.fiscal_period,
                        currency = EXCLUDED.currency,
                        revenue = EXCLUDED.revenue,
                        net_income = EXCLUDED.net_income,
                        free_cash_flow = EXCLUDED.free_cash_flow,
                        total_assets = EXCLUDED.total_assets,
                        shareholders_equity = EXCLUDED.shareholders_equity,
                        raw = EXCLUDED.raw,
                        fetched_at = EXCLUDED.fetched_at
                    """,
                    (
                        symbol,
                        statement_type,
                        accession,
                        row.get("period"),
                        _to_date(row.get("report_period")),
                        row.get("fiscal_period"),
                        row.get("currency"),
                        row.get("revenue"),
                        row.get("net_income"),
                        row.get("free_cash_flow"),
                        row.get("total_assets"),
                        row.get("shareholders_equity"),
                        Json(row),
                        fetched_at,
                    ),
                )
                inserted += 1
    conn.commit()
    return inserted


def persist_earnings(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    if _has_error(data):
        return 0
    items = (data or {}).get("earnings") or []
    if not items:
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    inserted = 0
    with conn.cursor() as cur:
        for row in items:
            accession = row.get("accession_number")
            if not accession:
                continue
            q = row.get("quarterly") or row.get("annual") or {}
            cur.execute(
                """
                INSERT INTO fd_earnings (
                    symbol, accession_number, fiscal_period, report_period, filing_date,
                    filing_datetime, source_type, currency,
                    eps_actual, eps_estimate, eps_surprise, eps_surprise_pct,
                    revenue_actual, revenue_estimate, revenue_surprise, revenue_surprise_pct,
                    raw, fetched_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, accession_number) DO UPDATE SET
                    fiscal_period = EXCLUDED.fiscal_period,
                    report_period = EXCLUDED.report_period,
                    filing_date = EXCLUDED.filing_date,
                    filing_datetime = EXCLUDED.filing_datetime,
                    source_type = EXCLUDED.source_type,
                    currency = EXCLUDED.currency,
                    eps_actual = EXCLUDED.eps_actual,
                    eps_estimate = EXCLUDED.eps_estimate,
                    eps_surprise = EXCLUDED.eps_surprise,
                    eps_surprise_pct = EXCLUDED.eps_surprise_pct,
                    revenue_actual = EXCLUDED.revenue_actual,
                    revenue_estimate = EXCLUDED.revenue_estimate,
                    revenue_surprise = EXCLUDED.revenue_surprise,
                    revenue_surprise_pct = EXCLUDED.revenue_surprise_pct,
                    raw = EXCLUDED.raw,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (
                    symbol,
                    accession,
                    row.get("fiscal_period"),
                    _to_date(row.get("report_period")),
                    _to_date(row.get("filing_date")),
                    _to_datetime(row.get("filing_datetime")),
                    row.get("source_type"),
                    row.get("currency"),
                    q.get("earnings_per_share"),
                    q.get("estimated_earnings_per_share"),
                    q.get("eps_surprise"),
                    q.get("eps_surprise_pct"),
                    q.get("revenue"),
                    q.get("estimated_revenue"),
                    q.get("revenue_surprise"),
                    q.get("revenue_surprise_pct"),
                    Json(row),
                    fetched_at,
                ),
            )
            inserted += 1
    conn.commit()
    return inserted


def persist_filings(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    if _has_error(data):
        return 0
    items = (data or {}).get("filings") or []
    if not items:
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    inserted = 0
    with conn.cursor() as cur:
        for row in items:
            accession = row.get("accession_number")
            if not accession:
                continue
            cur.execute(
                """
                INSERT INTO fd_filings (
                    symbol, accession_number, filing_type, filing_date,
                    report_date, url, raw, fetched_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, accession_number) DO UPDATE SET
                    filing_type = EXCLUDED.filing_type,
                    filing_date = EXCLUDED.filing_date,
                    report_date = EXCLUDED.report_date,
                    url = EXCLUDED.url,
                    raw = EXCLUDED.raw,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (
                    symbol,
                    accession,
                    row.get("filing_type"),
                    _to_date(row.get("filing_date")),
                    _to_date(row.get("report_date")),
                    row.get("url"),
                    Json(row),
                    fetched_at,
                ),
            )
            inserted += 1
    conn.commit()
    return inserted


def _insider_hash(symbol: str, row: dict[str, Any]) -> str:
    # Stable hash over identifying fields. Tied to symbol so the same trade reported
    # for two tickers (rare) gets distinct keys.
    payload = {
        "symbol": symbol,
        "transaction_date": row.get("transaction_date"),
        "filing_date": row.get("filing_date"),
        "name": row.get("name"),
        "security_title": row.get("security_title"),
        "transaction_type": row.get("transaction_type"),
        "transaction_shares": row.get("transaction_shares"),
        "transaction_price_per_share": row.get("transaction_price_per_share"),
        "shares_owned_after_transaction": row.get("shares_owned_after_transaction"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def persist_insiders(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    if _has_error(data):
        return 0
    items = (data or {}).get("insider_trades") or []
    if not items:
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    inserted = 0
    with conn.cursor() as cur:
        for row in items:
            payload_hash = _insider_hash(symbol, row)
            cur.execute(
                """
                INSERT INTO fd_insider_trades (
                    symbol, payload_hash, filing_date, transaction_date, name, title,
                    is_board_director, security_title, transaction_type,
                    transaction_shares, transaction_price_per_share, transaction_value,
                    shares_owned_after_transaction, raw, fetched_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, payload_hash) DO UPDATE SET
                    filing_date = EXCLUDED.filing_date,
                    transaction_date = EXCLUDED.transaction_date,
                    name = EXCLUDED.name,
                    title = EXCLUDED.title,
                    is_board_director = EXCLUDED.is_board_director,
                    security_title = EXCLUDED.security_title,
                    transaction_type = EXCLUDED.transaction_type,
                    transaction_shares = EXCLUDED.transaction_shares,
                    transaction_price_per_share = EXCLUDED.transaction_price_per_share,
                    transaction_value = EXCLUDED.transaction_value,
                    shares_owned_after_transaction = EXCLUDED.shares_owned_after_transaction,
                    raw = EXCLUDED.raw,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (
                    symbol,
                    payload_hash,
                    _to_date(row.get("filing_date")),
                    _to_date(row.get("transaction_date")),
                    row.get("name"),
                    row.get("title"),
                    row.get("is_board_director"),
                    row.get("security_title"),
                    row.get("transaction_type"),
                    row.get("transaction_shares"),
                    row.get("transaction_price_per_share"),
                    row.get("transaction_value"),
                    row.get("shares_owned_after_transaction"),
                    Json(row),
                    fetched_at,
                ),
            )
            inserted += 1
    conn.commit()
    return inserted


def persist_ownership(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    if _has_error(data):
        return 0
    items = (data or {}).get("institutional_ownership") or (data or {}).get("holdings") or []
    if not items:
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    inserted = 0
    with conn.cursor() as cur:
        for row in items:
            investor = row.get("investor") or row.get("holder")
            report_period = _to_date(row.get("report_period"))
            if not investor or not report_period:
                continue
            cur.execute(
                """
                INSERT INTO fd_institutional_ownership (
                    symbol, investor, report_period, security_type,
                    shares, market_value, price, raw, fetched_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, investor, report_period, security_type) DO UPDATE SET
                    shares = EXCLUDED.shares,
                    market_value = EXCLUDED.market_value,
                    price = EXCLUDED.price,
                    raw = EXCLUDED.raw,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (
                    symbol,
                    investor,
                    report_period,
                    row.get("security_type") or "common_stock",
                    row.get("shares"),
                    row.get("market_value"),
                    row.get("price"),
                    Json(row),
                    fetched_at,
                ),
            )
            inserted += 1
    conn.commit()
    return inserted


def persist_news(conn, symbol: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    if _has_error(data):
        return 0
    items = (data or {}).get("news") or (data or {}).get("articles") or []
    if not items:
        return 0
    fetched_at = fetched_at or _utcnow()
    _ensure_instrument(conn, symbol)
    inserted = 0
    with conn.cursor() as cur:
        for row in items:
            url = row.get("url")
            if not url:
                continue
            cur.execute(
                """
                INSERT INTO fd_news (
                    symbol, url, published_at, source, title, sentiment, raw, fetched_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, url) DO UPDATE SET
                    published_at = EXCLUDED.published_at,
                    source = EXCLUDED.source,
                    title = EXCLUDED.title,
                    sentiment = EXCLUDED.sentiment,
                    raw = EXCLUDED.raw,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (
                    symbol,
                    url,
                    _to_datetime(row.get("date") or row.get("published_at")),
                    row.get("source"),
                    row.get("title"),
                    row.get("sentiment"),
                    Json(row),
                    fetched_at,
                ),
            )
            inserted += 1
    conn.commit()
    return inserted


_PERSISTERS = {
    "facts": persist_facts,
    "metrics": persist_metrics,
    "financials": persist_financials,
    "earnings": persist_earnings,
    "filings": persist_filings,
    "insiders": persist_insiders,
    "ownership": persist_ownership,
    "news": persist_news,
}


def persist_section(conn, symbol: str, section: str, data: dict[str, Any], fetched_at: datetime | None = None) -> int:
    fn = _PERSISTERS.get(section)
    if fn is None:
        return 0
    try:
        return fn(conn, symbol.upper(), data, fetched_at)
    except Exception:
        log.exception("persist failed: symbol=%s section=%s", symbol, section)
        conn.rollback()
        return 0


# ───────────────────────── read API ─────────────────────────

def _fetch_one(conn, sql: str, params=None) -> dict[str, Any] | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return dict(row) if row else None


def _fetch_all(conn, sql: str, params=None) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]


def latest_facts(conn, symbol: str) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        "SELECT * FROM fd_company_facts WHERE symbol = %s",
        (symbol.upper(),),
    )


def latest_metrics(conn, symbol: str) -> dict[str, Any] | None:
    return _fetch_one(
        conn,
        "SELECT * FROM fd_financial_metrics WHERE symbol = %s",
        (symbol.upper(),),
    )


def recent_financials(conn, symbol: str, statement_type: str, limit: int = 4) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT DISTINCT ON (report_period) *
        FROM fd_financial_statements
        WHERE symbol = %s AND statement_type = %s AND report_period IS NOT NULL
        ORDER BY report_period DESC, fetched_at DESC
        LIMIT %s
        """,
        (symbol.upper(), statement_type, limit),
    )


def recent_earnings(conn, symbol: str, limit: int = 4) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT DISTINCT ON (report_period) *
        FROM fd_earnings
        WHERE symbol = %s AND report_period IS NOT NULL
        ORDER BY report_period DESC, filing_date DESC NULLS LAST
        LIMIT %s
        """,
        (symbol.upper(), limit),
    )


def recent_filings(conn, symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT * FROM fd_filings
        WHERE symbol = %s
        ORDER BY filing_date DESC NULLS LAST, accession_number DESC
        LIMIT %s
        """,
        (symbol.upper(), limit),
    )


def recent_insiders(conn, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        """
        SELECT * FROM fd_insider_trades
        WHERE symbol = %s
        ORDER BY transaction_date DESC NULLS LAST, filing_date DESC NULLS LAST
        LIMIT %s
        """,
        (symbol.upper(), limit),
    )


def top_holders(conn, symbol: str, limit: int = 10, report_period: date | None = None) -> list[dict[str, Any]]:
    if report_period is None:
        latest = _fetch_one(
            conn,
            "SELECT MAX(report_period) AS rp FROM fd_institutional_ownership WHERE symbol = %s",
            (symbol.upper(),),
        )
        if not latest or not latest.get("rp"):
            return []
        report_period = latest["rp"]
    return _fetch_all(
        conn,
        """
        SELECT * FROM fd_institutional_ownership
        WHERE symbol = %s AND report_period = %s
        ORDER BY market_value DESC NULLS LAST
        LIMIT %s
        """,
        (symbol.upper(), report_period, limit),
    )


def recent_news(conn, symbols: Iterable[str] | str, limit: int = 20) -> list[dict[str, Any]]:
    if isinstance(symbols, str):
        syms = [symbols.upper()]
    else:
        syms = [s.upper() for s in symbols]
    if not syms:
        return []
    return _fetch_all(
        conn,
        """
        SELECT * FROM fd_news
        WHERE symbol = ANY(%s)
        ORDER BY published_at DESC NULLS LAST
        LIMIT %s
        """,
        (syms, limit),
    )
