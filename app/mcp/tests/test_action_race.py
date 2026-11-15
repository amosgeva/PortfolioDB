"""One action set per request: lots and prices restated with the same actions.

QA of PR #126, round 2: ``prices._value_lots`` re-read the corporate actions
(via ``ledger_inputs.load``) while its callers restated the prices with the
set they had read first. Under READ COMMITTED a split approved between the two
reads valued 20 post-split shares at an unadjusted pre-split price — 2,000
instead of 1,000. Here the "approval between reads" is made deterministic by a
``fetch_actions`` that answers differently on its second call; the live
counterpart (a real commit mid-request) is in test_live_surface.py.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

LOT_COLS = ["id", "symbol", "account", "side", "trade_date", "quantity", "price", "fees"]
LOTS = [(1, "AAA", "A", "BUY", date(2026, 1, 5), Decimal(10), Decimal(100), Decimal(0))]
PRE_SPLIT_TS = datetime(2026, 1, 10, 15, tzinfo=timezone.utc)
PRICES = [(PRE_SPLIT_TS, "AAA", Decimal(100))]


@pytest.fixture
def split_appears_on_second_read(monkeypatch):
    import corporate_actions

    split = corporate_actions.CorporateAction(
        symbol="AAA", kind="SPLIT", ex_date=date(2026, 1, 20), ratio=Decimal(2),
    )
    calls = []

    def fetch(conn):
        calls.append(1)
        return [] if len(calls) == 1 else [split]

    monkeypatch.setattr(corporate_actions, "fetch_actions", fetch)
    return calls


def test_value_history_uses_one_action_set(fake_db, split_appears_on_second_read):
    from app.mcp.services import prices

    fake_db(responses=[
        (LOT_COLS, LOTS, "FROM lots"),
        (["ts", "symbol", "last_price"], PRICES, "price_snapshots"),
    ])
    series = prices.portfolio_value_history(date(2026, 1, 1), date(2026, 1, 31))
    assert [p["market_value"] for p in series] == [pytest.approx(1000.0)]
    assert len(split_appears_on_second_read) == 1, "the actions were read twice"


def test_portfolio_value_series_uses_one_action_set(fake_db, split_appears_on_second_read):
    from app.mcp.services import analytics

    fake_db(responses=[
        (LOT_COLS, LOTS, "FROM lots"),
        (["ts", "symbol", "last_price"], PRICES, "price_snapshots"),
    ])
    series = analytics._portfolio_value_series(date(2026, 1, 1))
    assert [v for _, v in series] == [pytest.approx(1000.0)]
    assert len(split_appears_on_second_read) == 1, "the actions were read twice"


def test_value_lots_restates_with_the_callers_actions(fake_db, monkeypatch):
    """Handed a split, the lots come back restated by it — no second read."""
    import corporate_actions

    from app.mcp.services import prices

    monkeypatch.setattr(corporate_actions, "fetch_actions",
                        lambda conn: pytest.fail("_value_lots must not re-read actions"))
    split = corporate_actions.CorporateAction(
        symbol="AAA", kind="SPLIT", ex_date=date(2026, 1, 20), ratio=Decimal(2),
    )
    fake_db(responses=[(LOT_COLS, LOTS, ("FROM lots", "ORDER BY"))])
    from app.mcp import deps

    with deps.get_conn() as conn:
        rows = prices._value_lots(conn, [split])
    assert rows[0]["quantity"] == pytest.approx(20.0)


def test_positions_frame_reads_the_actions_once(fake_db, split_appears_on_second_read, monkeypatch):
    from app.mcp.services import positions, prices

    monkeypatch.setattr(prices, "latest_price_map_with_ts", lambda **kw: {})
    fake_db(responses=[(LOT_COLS, LOTS, "FROM lots")])
    df = positions.positions_dataframe("fifo")
    assert float(df.loc[df["symbol"] == "AAA", "qty"].iloc[0]) == pytest.approx(10.0)
    assert len(split_appears_on_second_read) == 1


@pytest.mark.parametrize("name", [
    "app.mcp.services.prices:portfolio_value_history",
    "app.mcp.services.analytics:drawdown_stats",
    "app.mcp.services.returns:period_returns",
    "app.mcp.services.returns:volatility",
    "app.mcp.services.returns:benchmark_comparison",
])
def test_multi_read_services_run_in_one_snapshot(name):
    """Each reads actions, lots and prices in separate steps (some on separate
    connections); deps.consistent_read puts them in one REPEATABLE READ state."""
    import importlib

    module, func = name.split(":")
    fn = getattr(importlib.import_module(module), func)
    assert getattr(fn, "__wrapped__", None) is not None, f"{name} is not @consistent_read"
