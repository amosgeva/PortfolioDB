"""The MCP mirrors the 1.8.0 KPI definitions (docs/methodology.md §12, §14).

The dashboard, the Advisor and the reports compute their totals through
app/portfolio_kpis.py; the MCP must answer with the same numbers:

* F-12 / Q-2  an unpriced position is not a loss: unrealized P&L and % over
  priced rows and priced cost; unpriced cost and symbols reported apart.
* F-5 / Q-4   total return (and the review's return_on_cost_pct, and fee drag)
  over peak net invested capital, not the cost still open.
* F-11 / Q-8  income and cash in another currency are excluded and listed.
* U-3         the benchmark symbol through app/benchmark.py.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest

from app.mcp.services.cutoff import Cutoff

NOW = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
LOT_COLS = ["id", "symbol", "account", "side", "trade_date", "quantity", "price", "fees"]


def cutoff() -> Cutoff:
    return Cutoff(ts=NOW, trade_date=NOW.date())


def _frame(rows, invested):
    df = pd.DataFrame(rows)
    df.attrs["invested_capital"] = Decimal(str(invested))
    return df


def test_q2_unpriced_position_is_not_a_loss(fake_db, monkeypatch):
    """Priced: cost 100, value 120. Unpriced: cost 100. The dashboard used to
    say -80; the MCP now says +20 on 100 of priced cost, as the dashboard does."""
    from app.mcp.services import income, positions

    df = _frame([
        {"symbol": "AAA", "qty": 1.0, "open_cost": 100.0, "last_price": 120.0,
         "market_value": 120.0, "unrealized_pnl": 20.0, "realized_pnl": 0.0},
        {"symbol": "BBB", "qty": 1.0, "open_cost": 100.0, "last_price": float("nan"),
         "market_value": float("nan"), "unrealized_pnl": float("nan"), "realized_pnl": 0.0},
    ], invested=200)
    monkeypatch.setattr(positions, "positions_dataframe", lambda *a, **kw: df)
    monkeypatch.setattr(income, "usd_income_total", lambda **kw: (0.0, []))
    s = positions.positions_summary("fifo", cutoff=cutoff())
    assert s["market_value"] == pytest.approx(120.0)
    assert s["unrealized_pnl"] == pytest.approx(20.0)
    assert s["unrealized_pct"] == pytest.approx(20.0)
    assert s["priced_cost_basis"] == pytest.approx(100.0)
    assert s["unpriced_cost_basis"] == pytest.approx(100.0)
    assert s["unpriced_symbols"] == ["BBB"]
    assert s["cost_basis"] == pytest.approx(200.0)
    assert s["total_return_partial"] is True


def test_q4_total_return_survives_a_large_sale(fake_db, monkeypatch):
    """1,000 invested, sold 900 of it for a 900 profit, 100 left open (flat).
    The old ratio read 900%; over peak net invested it is 90%."""
    from app.mcp.services import income, positions

    df = _frame([
        {"symbol": "AAA", "qty": 1.0, "open_cost": 100.0, "last_price": 100.0,
         "market_value": 100.0, "unrealized_pnl": 0.0, "realized_pnl": 900.0},
    ], invested=1000)
    monkeypatch.setattr(positions, "positions_dataframe", lambda *a, **kw: df)
    monkeypatch.setattr(income, "usd_income_total", lambda **kw: (50.0, []))
    s = positions.positions_summary("fifo", cutoff=cutoff())
    assert s["total_return_pct"] == pytest.approx(90.0)
    assert s["total_return_with_income_pct"] == pytest.approx(95.0)
    assert s["invested_capital"] == pytest.approx(1000.0)


def test_q4_invested_capital_comes_from_the_same_filtered_lots(fake_db, monkeypatch):
    """positions_dataframe attaches peak net invested of exactly the lots it
    valued — here one account's — through portfolio_kpis.peak_net_invested."""
    import portfolio_kpis

    from app.mcp.services import positions, prices

    lots = [
        (1, "AAA", "A", "BUY", date(2026, 1, 5), Decimal(10), Decimal(100), Decimal(1)),
        (2, "AAA", "A", "SELL", date(2026, 2, 5), Decimal(5), Decimal(150), Decimal(1)),
    ]
    fake_db(responses=[(LOT_COLS, lots, ("FROM lots", "account = %s"))])
    monkeypatch.setattr(prices, "latest_price_map_with_ts", lambda **kw: {})
    df = positions.positions_dataframe("fifo", account="A", cutoff=cutoff())
    expected = portfolio_kpis.peak_net_invested(
        [dict(zip(LOT_COLS, r)) for r in lots]
    )
    assert df.attrs["invested_capital"] == expected == Decimal("1001")
    # And it survives the frame cache's copy.
    again = positions.positions_dataframe("fifo", account="A", cutoff=cutoff())
    assert again.attrs["invested_capital"] == expected


def test_q4_review_return_on_cost_is_over_invested_capital(fake_db, monkeypatch):
    """The review's return_on_cost_pct (with income) and total_return_pct use
    the same denominator as the dashboard: 900 realized + 50 income on 1,000
    ever invested is 95%, whatever is still open."""
    from app.mcp.services import returns, review

    monkeypatch.setattr(returns, "period_returns", lambda **kw: {
        "periods": {}, "basis": "twr", "observations": 0, "coverage": {}})
    monkeypatch.setattr(review, "_xirr", lambda c, s, a: {})
    monkeypatch.setattr(review, "_coverage_quality", lambda c, t, a=None: {})
    summary = {"cost_basis": 100.0, "realized_pnl": 900.0, "unrealized_pnl": 0.0,
               "income_total": 50.0, "invested_capital": 1000.0, "unpriced_symbols": []}
    out = review._returns(cutoff(), summary, "fifo", None)
    assert out["return_on_cost_pct"] == pytest.approx(95.0)
    assert out["total_return_with_income_pct"] == pytest.approx(95.0)
    assert out["total_return_pct"] == pytest.approx(90.0)
    assert "peak net invested" in out["methodology"]["return_on_cost"]


def test_q8_non_usd_income_is_excluded_and_listed(fake_db):
    from app.mcp.services import income

    fake_db(responses=[(
        ["symbol", "account", "kind", "pay_date", "amount", "tax_withheld", "currency"],
        [("AAA", "A", "DIVIDEND", date(2026, 3, 1), 10.0, 0.0, "USD"),
         ("BBB", "A", "DIVIDEND", date(2026, 3, 2), 7.0, 0.0, "EUR"),
         ("CCC", "A", "DIVIDEND", date(2026, 3, 3), 2.0, 0.0, None)],
        "currency",
    )])
    out = income.income_summary(group_by="none")
    assert out["total_income"] == pytest.approx(12.0)  # USD + missing (= USD)
    assert out["count"] == 2
    [w] = out["excluded_non_usd"]
    assert w["currency"] == "EUR" and w["treatment"] == "excluded" and w["symbols"] == ["BBB"]


def test_q8_twr_and_xirr_dividends_are_usd_only(fake_db):
    from app.mcp.services import returns

    fake_db(responses=[(
        ["pay_date", "amount", "account", "currency"],
        [(date(2026, 3, 1), 10.0, "A", "USD"), (date(2026, 3, 2), 7.0, "A", "EUR")],
        ("FROM income", "currency"),
    )])
    assert [d["amount"] for d in returns._fetch_dividends(cutoff())] == [10.0]


def test_q8_non_usd_cash_is_listed_not_added(fake_db):
    from app.mcp.services import cash

    fake_db(responses=[(
        ["account", "cash", "ts", "currency"],
        [("IBKR", 500.0, NOW, "USD"), ("EU", 300.0, NOW, "EUR")],
        "cash_snapshots",
    )])
    info = cash.latest_cash(NOW)
    assert info["total"] == pytest.approx(500.0)
    eu = next(r for r in info["by_account"] if r["account"] == "EU")
    assert eu["included"] is False
    [w] = info["excluded_non_usd"]
    assert w["kind"] == "cash" and w["account"] == "EU"


def test_q8_data_quality_flags_non_usd_income_and_cash(fake_db, monkeypatch):
    from app.mcp.services import cash, data_quality

    fake_db(responses=[(
        ["symbol", "account", "amount", "currency"],
        [("BBB", "A", 7.0, "EUR")],
        ("FROM income", "upper(currency)"),
    )])
    monkeypatch.setattr(cash, "latest_cash", lambda ts, **kw: {
        "total": 0.0, "excluded_non_usd": [],
        "by_account": [{"account": "EU", "cash": 300.0, "currency": "EUR", "included": False}],
    })
    issues = data_quality._foreign_money(cutoff())
    codes = sorted(i["code"] for i in issues)
    assert codes == ["foreign_currency_cash", "foreign_currency_income"]
    assert all(i["code"] in data_quality.CORRECTNESS_CODES for i in issues)


def test_u3_benchmark_symbol_is_the_shared_resolver(fake_db, monkeypatch):
    from app.mcp.services import returns

    monkeypatch.setenv("PORTFOLIODB_BENCHMARK_SYMBOL", " qqq ")
    monkeypatch.setattr(returns, "_price_by_day", lambda cutoff=None: {})
    monkeypatch.setattr(returns, "_records", lambda c, a, p: [])
    out = returns.benchmark_comparison("YTD", today=date(2026, 8, 12))
    assert out["benchmark_symbol"] == "QQQ"
