"""MCP follow-ups to the 1.8.0 ledger-core changes (W2a).

* Zero quotes are no price (audit F-15): every MCP read of price_snapshots
  filters ``last_price > 0``, so an old zero row cannot become "the latest
  price" and value a position at nothing.
* The holdings walk behind value history gets ``id`` and ``account``, so it
  matches per (symbol, account) as the engines do.
* A recorded split awaiting review is reported as pending, not as an
  unrecorded split (W2a: unreviewed collector-recorded splits adjust nothing
  until approved; corporate_actions.fetch_recorded_actions is the register).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.mcp.services.cutoff import Cutoff

NOW = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)


def cutoff() -> Cutoff:
    return Cutoff(ts=NOW, trade_date=NOW.date())


@pytest.mark.parametrize("call", [
    lambda p: p.latest_price("AAA"),
    lambda p: p.latest_prices(),
    lambda p: p.second_latest_price_map(),
    lambda p: p.prev_day_eod_price_map(),
    lambda p: p.price_history("AAA", date(2026, 1, 1), date(2026, 1, 2)),
    lambda p: p.price_history("AAA", date(2026, 1, 1), date(2026, 1, 2), resample="daily"),
])
def test_f15_zero_quotes_are_never_read_as_prices(fake_db, call):
    from app.mcp.services import prices

    fake_db(responses=[([], [])], cycle=True)
    call(prices)
    quote_reads = [s for s, _ in fake_db.executed() if "price_snapshots" in s]
    assert quote_reads and all("last_price > 0" in s for s in quote_reads), quote_reads


def test_f15_cutoff_resolution_skips_zero_quotes(fake_db):
    from app.mcp.services import cutoff as cutoff_service

    fake_db(responses=[([], [])], cycle=True)
    cutoff_service.resolve(NOW)
    reads = [s for s, _ in fake_db.executed() if "FROM price_snapshots" in s]
    assert reads and all("last_price > 0" in s for s in reads)


def test_value_lots_carry_id_and_account(fake_db):
    from app.mcp.services import prices

    cols = ["id", "symbol", "account", "side", "trade_date", "quantity", "price", "fees"]
    fake_db(responses=[(cols, [
        (1, "AAA", "A", "BUY", date(2026, 1, 5), Decimal(10), Decimal(1), Decimal(0)),
        (2, "AAA", "B", "SELL", date(2026, 1, 6), Decimal(10), Decimal(1), Decimal(0)),
    ], ("FROM lots", "ORDER BY"))])
    with __import__("app.mcp.deps", fromlist=["get_conn"]).get_conn() as conn:
        rows = prices._value_lots(conn, [])
    assert [(r["id"], r["account"]) for r in rows] == [(1, "A"), (2, "B")]


@dataclass(frozen=True)
class _Action:
    symbol: str
    kind: str
    ex_date: date
    ratio: Decimal
    pending_review: bool = False


def test_pending_split_is_reported_as_pending(fake_db, monkeypatch):
    import corporate_actions

    from app.mcp.services import data_quality

    pending = _Action("AAA", "SPLIT", date(2026, 7, 1), Decimal(2), pending_review=True)
    reviewed = _Action("BBB", "SPLIT", date(2026, 6, 1), Decimal(2))
    future = _Action("CCC", "SPLIT", date(2026, 9, 1), Decimal(2), pending_review=True)
    monkeypatch.setattr(corporate_actions, "fetch_recorded_actions",
                        lambda conn: [pending, reviewed, future], raising=False)
    found = data_quality._pending_splits(cutoff())
    assert set(found) == {"AAA"}
    assert found["AAA"]["ex_date"] == "2026-07-01"
    assert "split_pending_review" in data_quality.CORRECTNESS_CODES


def test_suspected_split_scan_knows_pending_records(fake_db, monkeypatch):
    """The step a pending split explains is not reported as unrecorded."""
    import corporate_actions

    from app.mcp.services import data_quality

    pending = _Action("AAA", "SPLIT", date(2026, 7, 1), Decimal(2), pending_review=True)
    monkeypatch.setattr(corporate_actions, "fetch_recorded_actions",
                        lambda conn: [pending], raising=False)
    seen = {}

    def detect(series, known=None):
        seen["known"] = list(known or [])
        return []

    monkeypatch.setattr(corporate_actions, "detect_suspected_splits", detect)
    fake_db(responses=[(["symbol", "day_local", "last_price"], [], "last_price > 0")])
    data_quality._suspected_splits(cutoff())
    assert seen["known"] == [pending]
