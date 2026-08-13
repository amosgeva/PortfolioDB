"""Fundamentals service tests — covers fd_store passthrough + ETF handling."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal


def test_is_etf_recognizes_known_etfs():
    from app.mcp.services import fundamentals
    assert fundamentals.is_etf("GLD")
    assert fundamentals.is_etf("voo")  # case-insensitive
    assert not fundamentals.is_etf("NVDA")


def test_company_facts_returns_none_for_missing(env_token, fake_db, monkeypatch):
    from app.mcp.services import fundamentals
    import fd_store
    monkeypatch.setattr(fd_store, "latest_facts", lambda conn, sym: None)
    assert fundamentals.company_facts("NOPE") is None


def test_company_facts_scrubs_dates_and_decimals(env_token, fake_db, monkeypatch):
    from app.mcp.services import fundamentals
    import fd_store
    payload = {
        "symbol": "NVDA",
        "name": "Nvidia",
        "sector": "Information Technology",
        "fetched_at": datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        "raw": {"any": "json"},
    }
    monkeypatch.setattr(fd_store, "latest_facts", lambda conn, sym: payload)
    out = fundamentals.company_facts("nvda")
    assert out["symbol"] == "NVDA"
    assert isinstance(out["fetched_at"], str)
    assert out["raw"] == {"any": "json"}


def test_financial_statements_rejects_bad_type(env_token):
    import pytest
    from app.mcp.services import fundamentals
    with pytest.raises(ValueError):
        fundamentals.financial_statements("NVDA", "owner_earnings")


def test_news_with_empty_universe_returns_empty(env_token, monkeypatch):
    from app.mcp.services import fundamentals
    # No symbols → no DB hit, no rows.
    monkeypatch.setattr(fundamentals, "_default_news_universe", lambda: [])
    assert fundamentals.news() == []


def test_freshness_includes_all_fd_sections(env_token, fake_db):
    from app.mcp.services import fundamentals
    fake_db(
        responses=[(["max", "count"], [(datetime(2026, 5, 18, tzinfo=timezone.utc), 12)]) for _ in range(8)],
    )
    out = fundamentals.freshness()
    assert out["symbol"] is None
    assert set(out["sections"].keys()) == {
        "facts", "metrics", "statements", "earnings",
        "filings", "insiders", "ownership", "news",
    }
    facts = out["sections"]["facts"]
    assert facts["row_count"] == 12


def test_freshness_scopes_to_symbol(env_token, fake_db):
    from app.mcp.services import fundamentals
    fake_db(
        responses=[(["max", "count"], [(None, 0)]) for _ in range(8)],
    )
    out = fundamentals.freshness("AAPL")
    assert out["symbol"] == "AAPL"
    for s in out["sections"].values():
        assert s.get("row_count", 0) == 0
