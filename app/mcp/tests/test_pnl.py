"""Tests for the P&L service — FIFO match math + groupings + method comparison."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

LOTS = [
    # Same lot stream the dashboard would see — covers BUY/SELL/multi-account.
    (1, "VOO",  "IBKR", "BUY",  date(2024, 12, 1), "10", "440.00", "0.00"),
    (2, "VOO",  "IBKR", "SELL", date(2026, 3, 20), "3",  "480.00", "0.50"),
    (3, "AAPL", "IBKR", "BUY",  date(2025, 2, 15), "10", "180.00", "1.00"),
    (4, "AAPL", "IBKR", "SELL", date(2025, 9, 1),  "10", "200.00", "1.00"),
]


@pytest.fixture
def patched(monkeypatch, env_token, fake_db):
    """Patch lot fetch + minimum price plumbing used downstream."""
    from app.mcp.services import positions as positions_service
    from app.mcp.services import prices as prices_service

    def fake_fetch_lots(conn, *, account=None, symbol=None, as_of=None):
        return [
            {"id": r[0], "symbol": r[1], "account": r[2], "side": r[3],
             "trade_date": r[4], "quantity": Decimal(r[5]),
             "price": Decimal(r[6]), "fees": Decimal(r[7])}
            for r in LOTS
            if (symbol is None or r[1] == symbol.upper())
            and (account is None or r[2] == account)
            and (as_of is None or r[4] <= as_of)
        ]
    monkeypatch.setattr(positions_service, "_fetch_lots", fake_fetch_lots)

    # pnl_service._all_realized_matches has its own DB query — patch through
    # the fake_db cursor by injecting a response that matches its SELECT shape.
    cols = ["id", "symbol", "account", "side", "trade_date", "quantity", "price", "fees"]
    rows = [
        (r[0], r[1], r[2], r[3], r[4], Decimal(r[5]), Decimal(r[6]), Decimal(r[7]))
        for r in LOTS
    ]
    fake_db(responses=[(cols, rows)], cycle=True)

    # Prices for unrealized calls — not exercised by tests below, but the
    # service imports it transitively.
    monkeypatch.setattr(
        prices_service, "latest_price_map_with_ts",
        lambda **_kw: {"VOO": {"last_price": 500.0, "ts": "2026-05-20T19:00:00+00:00"}},
    )


def test_realized_pnl_grouped_by_symbol(patched):
    from app.mcp.services import pnl
    result = pnl.realized_pnl("fifo", group_by="symbol")
    by_sym = {r["bucket"]: r["realized_pnl"] for r in result["rows"]}
    # AAPL: bought 10 @ $180 + $1 fees → ps cost = 180.10
    #       sold   10 @ $200 − $1 fees → ps proceeds = 199.90
    #       realized = (199.90 − 180.10) × 10 = 198.00
    expected_aapl = ((200 * 10 - 1) / 10 - (180 * 10 + 1) / 10) * 10
    assert by_sym["AAPL"] == pytest.approx(expected_aapl)
    # VOO: bought 10 @ $440 (no fees) → ps cost = 440.00
    #      sold   3  @ $480 − $0.50 fees → ps proceeds = (1440 − 0.5)/3 ≈ 479.833
    #      realized = (479.833 − 440.0) × 3 ≈ 119.50
    expected_voo = (((480 * 3) - 0.5) / 3 - 440.0) * 3
    assert by_sym["VOO"] == pytest.approx(expected_voo)
    assert result["total_realized"] == pytest.approx(expected_aapl + expected_voo)


def test_realized_pnl_filtered_by_date(patched):
    from app.mcp.services import pnl
    # Only AAPL was sold in 2025
    result = pnl.realized_pnl(
        "fifo", since=date(2025, 1, 1), until=date(2025, 12, 31), group_by="symbol",
    )
    syms = [r["bucket"] for r in result["rows"]]
    assert syms == ["AAPL"]


def test_realized_pnl_grouped_by_month(patched):
    from app.mcp.services import pnl
    result = pnl.realized_pnl("fifo", group_by="month")
    buckets = sorted(r["bucket"] for r in result["rows"])
    assert buckets == ["2025-09", "2026-03"]


def test_pnl_summary_dispatches_to_positions(patched):
    from app.mcp.services import pnl
    out = pnl.pnl_summary("fifo")
    assert "market_value" in out
    assert out["method"] == "fifo"


def test_compare_methods_returns_diff_total(patched):
    from app.mcp.services import pnl
    out = pnl.compare_methods()
    assert "fifo_total" in out
    assert "avg_total" in out
    # For these simple single-buy/single-sell streams FIFO == avg, so diff is 0.
    assert out["diff_total"] == pytest.approx(0.0, abs=1e-6)
