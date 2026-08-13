"""Tests for the activity service — filters, groupings, cash."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest


def test_lots_with_no_filters_returns_all(env_token, fake_db):
    from app.mcp.services import activity
    cols = ["id", "symbol", "account", "side", "trade_date",
            "quantity", "price", "fees", "notes", "created_at"]
    rows = [
        (1, "NVDA", "IBKR", "BUY", date(2025, 1, 10),
         Decimal("5"), Decimal("120.00"), Decimal("1.00"), None,
         datetime(2025, 1, 10, tzinfo=timezone.utc)),
        (2, "AAPL", "IBKR", "BUY", date(2025, 2, 15),
         Decimal("10"), Decimal("180.00"), Decimal("1.00"), "intro buy",
         datetime(2025, 2, 15, tzinfo=timezone.utc)),
    ]
    fake_db(responses=[(cols, rows)])

    out = activity.lots()
    assert len(out) == 2
    assert out[0]["symbol"] in {"NVDA", "AAPL"}
    assert out[0]["quantity"] in (5.0, 10.0)


def test_trading_activity_rejects_bad_group_by(env_token):
    from app.mcp.services import activity
    with pytest.raises(ValueError):
        activity.trading_activity("decade")


def test_trading_activity_by_symbol(env_token, fake_db):
    from app.mcp.services import activity
    cols = ["bucket", "trades", "buys", "sells",
            "buy_notional", "sell_notional", "total_fees",
            "first_trade", "last_trade"]
    rows = [
        ("AAPL", 2, 1, 1, Decimal("1800.00"), Decimal("2000.00"), Decimal("2.00"),
         date(2025, 2, 15), date(2025, 9, 1)),
        ("NVDA", 1, 1, 0, Decimal("600.00"), Decimal("0.00"), Decimal("1.00"),
         date(2025, 1, 10), date(2025, 1, 10)),
    ]
    fake_db(responses=[(cols, rows)])

    out = activity.trading_activity("symbol")
    assert len(out) == 2
    by_sym = {r["bucket"]: r for r in out}
    assert by_sym["AAPL"]["buys"] == 1
    assert by_sym["AAPL"]["sells"] == 1
    assert by_sym["AAPL"]["buy_notional"] == 1800.0
    assert by_sym["NVDA"]["sells"] == 0


def test_cash_balance_merged_total(env_token, fake_db):
    from app.mcp.services import activity
    # cash_snapshots ORDER BY ts DESC — first per account wins
    fake_db(responses=[(
        ["account", "cash", "ts"],
        [
            ("IBKR",  Decimal("500.00"), datetime(2026, 5, 18, tzinfo=timezone.utc)),
            ("Blink", Decimal("150.00"), datetime(2026, 5, 17, tzinfo=timezone.utc)),
            # Older IBKR row that must be ignored
            ("IBKR",  Decimal("200.00"), datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ],
    )])

    out = activity.cash_balance()
    assert out["account"] is None
    assert out["total_cash"] == 650.0
    accounts = sorted(r["account"] for r in out["by_account"])
    assert accounts == ["Blink", "IBKR"]


def test_cash_balance_specific_account(env_token, fake_db):
    from app.mcp.services import activity
    fake_db(responses=[(
        ["account", "cash", "ts"],
        [
            ("IBKR",  Decimal("500.00"), datetime(2026, 5, 18, tzinfo=timezone.utc)),
            ("Blink", Decimal("150.00"), datetime(2026, 5, 17, tzinfo=timezone.utc)),
        ],
    )])
    out = activity.cash_balance("Blink")
    assert out == {
        "account": "Blink",
        "cash": 150.0,
        "ts": datetime(2026, 5, 17, tzinfo=timezone.utc).isoformat(),
    }


def test_cash_balance_unknown_account_returns_zero(env_token, fake_db):
    from app.mcp.services import activity
    fake_db(responses=[(
        ["account", "cash", "ts"],
        [("IBKR", Decimal("500.00"), datetime(2026, 5, 18, tzinfo=timezone.utc))],
    )])
    out = activity.cash_balance("Nope")
    assert out == {"account": "Nope", "cash": 0.0, "ts": None}
