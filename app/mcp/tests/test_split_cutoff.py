"""Historical MCP positions are stated in the units of the observation date.

The 1.7.1 re-audit (F03, case A): `_fetch_lots(as_of=…)` filtered lots by date
but applied every recorded split, so "positions the day before a 2:1" came out
as twenty shares against the pre-split quote — twice the value that existed.
Now `prepare(as_of=…)` leaves out actions dated after the observation date,
and a stale quote observed before an action the cutoff is past is restated
into the cutoff's units.

The real loader and positions_dataframe run; only the database cursor, the
actions table and the latest-quote query are replaced.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

D_BUY, D_PRE, D_EX, D_POST = (date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5))
COLS = ["id", "symbol", "account", "side", "trade_date", "quantity", "price", "fees"]
LOT_ROWS = [(1, "AAA", "IBKR", "BUY", D_BUY, Decimal("10"), Decimal("100"), Decimal("0"))]


def _ts(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 20, tzinfo=timezone.utc)


@pytest.fixture
def split_world(monkeypatch, env_token, fake_db):
    """Ten shares bought at 100; 2:1 split ex D_EX; quotes 100 before, 50 after."""
    from corporate_actions import CorporateAction
    from app.mcp.services import positions as positions_service
    from app.mcp.services import prices as prices_service
    import corporate_actions

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, params=None): pass
        def fetchall(self): return list(LOT_ROWS)
        @property
        def description(self): return [(c,) for c in COLS]

    class Conn:
        def cursor(self, *a, **k): return Cur()
        def rollback(self): pass

    @contextmanager
    def fake_conn():
        yield Conn()

    monkeypatch.setattr(positions_service, "get_conn", fake_conn)
    monkeypatch.setattr(
        corporate_actions, "fetch_actions",
        lambda conn: [CorporateAction(symbol="AAA", kind="SPLIT", ex_date=D_EX, ratio=Decimal("2"))],
    )
    quotes = {}

    def set_quote(when: date, price: float):
        quotes.clear()
        quotes["AAA"] = {"last_price": price, "ts": _ts(when)}

    monkeypatch.setattr(prices_service, "latest_price_map_with_ts", lambda **kw: dict(quotes))
    positions_service.clear_frame_cache()
    return set_quote


def _row(as_of):
    from app.mcp.services import positions
    df = positions.positions_dataframe("fifo", as_of=as_of)
    return df.set_index("symbol").loc["AAA"]


def test_before_the_split_the_position_is_ten_shares_at_the_old_quote(split_world):
    split_world(D_PRE, 100.0)
    r = _row(D_PRE)
    assert r["qty"] == pytest.approx(10.0)
    assert r["last_price"] == pytest.approx(100.0)
    assert r["market_value"] == pytest.approx(1000.0)


def test_after_the_split_the_position_is_twenty_shares_at_the_new_quote(split_world):
    split_world(D_POST, 50.0)
    r = _row(D_POST)
    assert r["qty"] == pytest.approx(20.0)
    assert r["market_value"] == pytest.approx(1000.0)


def test_on_the_ex_date_the_split_applies(split_world):
    split_world(D_EX, 50.0)
    r = _row(D_EX)
    assert r["qty"] == pytest.approx(20.0)
    assert r["market_value"] == pytest.approx(1000.0)


def test_a_stale_pre_split_quote_is_restated_for_a_post_split_cutoff(split_world):
    """Last quote is from before the ex-date, cutoff is after it: twenty
    restated shares must not be multiplied by the old 100."""
    split_world(D_PRE, 100.0)
    r = _row(D_POST)
    assert r["qty"] == pytest.approx(20.0)
    assert r["last_price"] == pytest.approx(50.0)
    assert r["market_value"] == pytest.approx(1000.0)


def test_a_pure_split_never_changes_value_across_cutoffs(split_world):
    values = []
    for when, quote in ((D_PRE, 100.0), (D_EX, 50.0), (D_POST, 50.0)):
        split_world(when, quote)
        values.append(_row(when)["market_value"])
    assert values == pytest.approx([1000.0, 1000.0, 1000.0])
