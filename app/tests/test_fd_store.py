"""Tests for fd_store persistence + read API.

Integration tests against a real Postgres (the project's docker-compose). Uses a
sentinel symbol "TST_FD" and cleans up between tests so it never collides with
production data.

Skips the whole module if PORTFOLIODB_PASSWORD isn't set or the DB isn't
reachable, so the unit-test suite (test_fifo) still runs in isolation — unless
PORTFOLIODB_TESTS_REQUIRE_DB is set, as it is in the CI job that has a
database, in which case that is a failure (see db.connect_for_tests).

The payloads come from ``fixtures/fd/``: small synthetic sections in the
vendor's shape (one fictional issuer, example.invalid URLs). They used to be
read from ``cache/financialdatasets/AAPL/``, which is gitignored, so a clean
clone skipped most of these round trips even with a database (re-audit N06).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from psycopg2 import sql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fd_store  # noqa: E402
from db import TESTS_REQUIRE_DB_ENV, connect_for_tests  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "fd"
TEST_SYMBOL = "TST_FD"

FD_TABLES = (
    "fd_company_facts",
    "fd_financial_metrics",
    "fd_financial_statements",
    "fd_earnings",
    "fd_filings",
    "fd_insider_trades",
    "fd_institutional_ownership",
    "fd_news",
)


SECTION_TABLE = {
    "facts": "fd_company_facts",
    "metrics": "fd_financial_metrics",
    "financials": "fd_financial_statements",
    "earnings": "fd_earnings",
    "filings": "fd_filings",
    "insiders": "fd_insider_trades",
    "ownership": "fd_institutional_ownership",
    "news": "fd_news",
}


def _required() -> bool:
    return (os.getenv(TESTS_REQUIRE_DB_ENV) or "").strip().lower() in ("1", "true", "yes")


def _read_fixture(section: str) -> dict | list | None:
    """Committed fixture payload, or None if the file is missing."""
    path = FIXTURE_DIR / f"{section}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["data"]


def _load(section: str) -> dict:
    data = _read_fixture(section)
    if data is None:
        (pytest.fail if _required() else pytest.skip)(f"fixture missing: {FIXTURE_DIR / (section + '.json')}")
    return data


def _wipe_test_symbol(conn) -> None:
    with conn.cursor() as cur:
        for tbl in FD_TABLES:
            cur.execute(
                sql.SQL("DELETE FROM ")
                + sql.Identifier(tbl)
                + sql.SQL(" WHERE symbol = %s"),
                (TEST_SYMBOL,),
            )
        cur.execute("DELETE FROM instruments WHERE symbol = %s", (TEST_SYMBOL,))
    conn.commit()


def _counts(conn) -> dict[str, int]:
    out = {}
    with conn.cursor() as cur:
        for t in FD_TABLES:
            cur.execute(
                sql.SQL("SELECT count(*) FROM ")
                + sql.Identifier(t)
                + sql.SQL(" WHERE symbol = %s"),
                (TEST_SYMBOL,),
            )
            out[t] = cur.fetchone()[0]
    return out


@pytest.fixture(scope="module")
def conn():
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        yield c
    finally:
        try:
            _wipe_test_symbol(c)
        finally:
            c.close()


@pytest.fixture(autouse=True)
def _clean_between_tests(conn):
    _wipe_test_symbol(conn)
    yield


# ───────────────────────── per-section roundtrips ─────────────────────────

def test_facts_roundtrip(conn):
    n = fd_store.persist_facts(conn, TEST_SYMBOL, _load("facts"))
    assert n == 1
    row = fd_store.latest_facts(conn, TEST_SYMBOL)
    assert row is not None
    assert row["name"] == "Test Fixtures Inc"
    assert row["sector"] == "Information Technology"
    assert row["exchange"] == "NASDAQ"
    assert row["raw"], "raw JSONB should round-trip"


def test_metrics_roundtrip(conn):
    n = fd_store.persist_metrics(conn, TEST_SYMBOL, _load("metrics"))
    assert n == 1
    row = fd_store.latest_metrics(conn, TEST_SYMBOL)
    assert row is not None
    assert row["pe_ratio"] is not None and float(row["pe_ratio"]) > 0
    assert row["ev_ebitda"] is not None
    assert row["return_on_equity"] is not None


def test_financials_roundtrip(conn):
    n = fd_store.persist_financials(conn, TEST_SYMBOL, _load("financials"))
    # The fixture has 4 quarters × 3 statement types
    assert n == 12
    inc = fd_store.recent_financials(conn, TEST_SYMBOL, "income_statement", limit=4)
    assert len(inc) == 4
    assert all(r["revenue"] is not None for r in inc)
    periods = [r["report_period"] for r in inc]
    assert periods == sorted(periods, reverse=True), "should be ordered desc"


def test_earnings_roundtrip(conn):
    n = fd_store.persist_earnings(conn, TEST_SYMBOL, _load("earnings"))
    assert n >= 1
    rows = fd_store.recent_earnings(conn, TEST_SYMBOL, limit=10)
    assert len(rows) >= 1
    assert any(r["eps_actual"] is not None for r in rows)
    assert any(r["eps_surprise"] in ("BEAT", "MISS", "INLINE") for r in rows)


def test_filings_roundtrip(conn):
    n = fd_store.persist_filings(conn, TEST_SYMBOL, _load("filings"))
    assert n >= 1
    rows = fd_store.recent_filings(conn, TEST_SYMBOL, limit=10)
    assert len(rows) == n
    assert all(r["url"] for r in rows)


def test_insiders_roundtrip(conn):
    n = fd_store.persist_insiders(conn, TEST_SYMBOL, _load("insiders"))
    assert n >= 1
    rows = fd_store.recent_insiders(conn, TEST_SYMBOL, limit=20)
    assert len(rows) == n
    dates = [r["transaction_date"] for r in rows if r["transaction_date"]]
    assert dates == sorted(dates, reverse=True)


def test_ownership_roundtrip(conn):
    n = fd_store.persist_ownership(conn, TEST_SYMBOL, _load("ownership"))
    assert n >= 1
    rows = fd_store.top_holders(conn, TEST_SYMBOL, limit=5)
    assert 0 < len(rows) <= 5
    vals = [r["market_value"] for r in rows]
    assert vals == sorted(vals, reverse=True)


def test_news_roundtrip(conn):
    n = fd_store.persist_news(conn, TEST_SYMBOL, _load("news"))
    assert n >= 1
    rows = fd_store.recent_news(conn, TEST_SYMBOL, limit=10)
    assert len(rows) == n
    assert all(r["url"] for r in rows)


# ───────────────────────── dispatcher + edge cases ─────────────────────────

def test_persist_section_dispatcher(conn):
    n = fd_store.persist_section(conn, TEST_SYMBOL, "facts", _load("facts"))
    assert n == 1
    assert fd_store.persist_section(conn, TEST_SYMBOL, "unknown_section", {}) == 0


def test_persist_skips_error_payload(conn):
    n = fd_store.persist_facts(conn, TEST_SYMBOL, {"_error": "HTTP 429"})
    assert n == 0
    assert fd_store.latest_facts(conn, TEST_SYMBOL) is None


def test_persist_skips_empty_payload(conn):
    assert fd_store.persist_news(conn, TEST_SYMBOL, {}) == 0
    assert fd_store.persist_financials(conn, TEST_SYMBOL, {"financials": {}}) == 0


# ───────────────────────── idempotency ─────────────────────────

def test_idempotent_upsert(conn):
    sections = [s for s in SECTION_TABLE if _read_fixture(s) is not None]
    assert sections == list(SECTION_TABLE), "every section has a committed fixture"

    for sec in sections:
        fd_store.persist_section(conn, TEST_SYMBOL, sec, _load(sec))
    counts1 = _counts(conn)

    for sec in sections:
        fd_store.persist_section(conn, TEST_SYMBOL, sec, _load(sec))
    counts2 = _counts(conn)

    assert counts1 == counts2, f"persist twice produced different counts: {counts1} vs {counts2}"
    # And every persisted section's table has at least one row for the symbol.
    for sec in sections:
        assert counts1[SECTION_TABLE[sec]] > 0, (sec, counts1)
