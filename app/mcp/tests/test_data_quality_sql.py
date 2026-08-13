"""The SQL-backed detectors, against a real database.

The orchestration is unit-tested in app/mcp/tests/test_data_quality.py with
these helpers stubbed out. What that cannot cover is whether the queries
themselves find anything — and the duplicate-lot query in particular is subtle
enough that its first version produced two false positives on live data.

Skips when Postgres is unreachable, following the pattern in
app/tests/test_dedupe_guards.py. Every row is namespaced to ZZDQ* symbols and
removed afterwards.

Lives in the MCP suite rather than app/tests because it imports app.mcp.* and
so must run from the repo root — running it from app/ would put the local
app/mcp/ package on the path as top-level `mcp`, shadowing the official SDK.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

# Importing deps first puts app/ on sys.path, so the bare `db` module resolves.
from app.mcp.deps import get_conn  # noqa: F401

from db import connect, load_config  # noqa: E402

TEST_SYMBOLS = ("ZZDQA", "ZZDQB", "ZZDQC")
TEST_ACCOUNT = "ZZDQ-ACCT"


def _wipe(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lots WHERE symbol = ANY(%s)", (list(TEST_SYMBOLS),))
        cur.execute(
            "DELETE FROM price_snapshots WHERE symbol = ANY(%s)", (list(TEST_SYMBOLS),)
        )
        cur.execute(
            "DELETE FROM corporate_actions WHERE symbol = ANY(%s)", (list(TEST_SYMBOLS),)
        )
        cur.execute(
            "DELETE FROM instruments WHERE symbol = ANY(%s)", (list(TEST_SYMBOLS),)
        )
    conn.commit()


@pytest.fixture(scope="module")
def conn():
    try:
        cfg = load_config()
    except Exception as e:
        pytest.skip(f"DB config unavailable: {e}")
    try:
        c = connect(cfg)
    except Exception as e:
        pytest.skip(f"DB unreachable: {e}")
    try:
        yield c
    finally:
        try:
            _wipe(c)
        finally:
            c.close()


@pytest.fixture(autouse=True)
def clean(conn):
    _wipe(conn)
    with conn.cursor() as cur:
        for sym in TEST_SYMBOLS:
            cur.execute(
                "INSERT INTO instruments(symbol, sector, country, asset_type) "
                "VALUES (%s, 'Tech', 'US', 'stock') ON CONFLICT DO NOTHING",
                (sym,),
            )
    conn.commit()
    yield
    _wipe(conn)


@pytest.fixture
def dq(conn, monkeypatch):
    """The service, pointed at this test's connection."""
    from contextlib import contextmanager

    from app.mcp.services import data_quality as module

    @contextmanager
    def fake_get_conn():
        yield conn

    monkeypatch.setattr(module, "get_conn", fake_get_conn)
    return module


def add_lot(conn, symbol, side, day, qty, price, account=TEST_ACCOUNT):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lots(symbol, account, side, trade_date, quantity, price) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (symbol, account, side, day, qty, price),
        )
    conn.commit()


def cutoff_at(day: date = date(2026, 12, 31)):
    from app.mcp.services.cutoff import Cutoff

    return Cutoff(
        ts=datetime(2026, 12, 31, tzinfo=timezone.utc),
        trade_date=day,
        coverage_start=date(2025, 9, 22),
    )


# ────────────────────────── orphan sells ──────────────────────────


class TestOrphanSells:
    def test_detects_a_sell_exceeding_open_buys(self, conn, dq):
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 100)
        add_lot(conn, "ZZDQA", "SELL", date(2026, 2, 5), 8, 110)

        found = dq._orphan_sells(cutoff_at())
        assert "ZZDQA" in found
        assert found["ZZDQA"]["shortfall"] == pytest.approx(-3.0)

    def test_ignores_a_balanced_ledger(self, conn, dq):
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 100)
        add_lot(conn, "ZZDQA", "SELL", date(2026, 2, 5), 5, 110)
        assert "ZZDQA" not in dq._orphan_sells(cutoff_at())

    def test_scoped_per_account(self, conn, dq):
        """Matching is per (symbol, account); buying in one account does not
        cover a sell in another."""
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 100, account="ZZDQ-OTHER")
        add_lot(conn, "ZZDQA", "SELL", date(2026, 2, 5), 5, 110, account=TEST_ACCOUNT)
        assert "ZZDQA" in dq._orphan_sells(cutoff_at())
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lots WHERE account = 'ZZDQ-OTHER'")
        conn.commit()

    def test_sell_before_buy_in_date_order_is_orphaned(self, conn, dq):
        add_lot(conn, "ZZDQA", "SELL", date(2026, 1, 5), 5, 110)
        add_lot(conn, "ZZDQA", "BUY", date(2026, 2, 5), 5, 100)
        assert "ZZDQA" in dq._orphan_sells(cutoff_at())

    def test_respects_the_cutoff(self, conn, dq):
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 100)
        add_lot(conn, "ZZDQA", "SELL", date(2026, 6, 5), 8, 110)
        assert "ZZDQA" not in dq._orphan_sells(cutoff_at(date(2026, 3, 1)))


# ────────────────────────── duplicate lots ──────────────────────────


class TestDuplicateLots:
    def test_flags_near_identical_prices(self, conn, dq):
        """The re-import case: same trade entered twice, price off by a step."""
        add_lot(conn, "ZZDQA", "BUY", date(2026, 3, 19), 0.747, 604.99)
        add_lot(conn, "ZZDQA", "BUY", date(2026, 3, 19), 0.747, 605.01)

        found = dq._suspect_duplicate_lots(cutoff_at())
        assert "ZZDQA" in found
        assert found["ZZDQA"]["count"] == 2

    def test_ignores_two_fills_at_different_prices(self, conn, dq):
        """Real case from the live ledger: SPCX sold 1 share at 209.06 and 1 at
        198.54 on the same day. An ordinary two-fill exit, not a duplicate."""
        add_lot(conn, "ZZDQB", "SELL", date(2026, 6, 16), 1, 209.06)
        add_lot(conn, "ZZDQB", "SELL", date(2026, 6, 16), 1, 198.54)

        assert "ZZDQB" not in dq._suspect_duplicate_lots(cutoff_at())

    def test_ignores_a_reversal_pair(self, conn, dq):
        """Real case from the live ledger: VOO on 2026-03-19 has BUY 0.747 @
        605.01 offset by SELL 0.747 @ 605.01. `lots` is append-only, so an
        equal-and-opposite entry is the only way to correct a mistake — and the
        rows can never be deleted, so flagging it would be permanent noise."""
        add_lot(conn, "ZZDQC", "BUY", date(2026, 3, 19), 0.747, 604.99)
        add_lot(conn, "ZZDQC", "BUY", date(2026, 3, 19), 0.747, 605.01)
        add_lot(conn, "ZZDQC", "SELL", date(2026, 3, 19), 0.747, 605.01)

        assert "ZZDQC" not in dq._suspect_duplicate_lots(cutoff_at())

    def test_different_days_are_not_duplicates(self, conn, dq):
        add_lot(conn, "ZZDQA", "BUY", date(2026, 3, 19), 1, 100.00)
        add_lot(conn, "ZZDQA", "BUY", date(2026, 3, 20), 1, 100.00)
        assert "ZZDQA" not in dq._suspect_duplicate_lots(cutoff_at())


# ────────────────────────── impossible values ──────────────────────────


class TestImpossibleValues:
    def test_flags_a_zero_price_buy(self, conn, dq):
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 0)
        found = dq._impossible_values(cutoff_at())
        assert found["ZZDQA"]["count"] == 1

    def test_zero_price_sell_is_not_flagged(self, conn, dq):
        """A worthless-disposal SELL at zero is unusual but representable; a
        zero-cost BUY silently understates basis, which is the real risk."""
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 100)
        add_lot(conn, "ZZDQA", "SELL", date(2026, 2, 5), 5, 0)
        assert "ZZDQA" not in dq._impossible_values(cutoff_at())


# ────────────────────────── targeting + staleness ──────────────────────────


class TestTargeting:
    def test_held_symbols_are_targeted(self, conn, dq):
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 100)
        assert "ZZDQA" in dq._targeted_symbols(cutoff_at())

    def test_closed_positions_are_not_targeted(self, conn, dq):
        """The collector stops snapshotting them, so their stale prices are
        intentional. Reporting them would bury every real finding under 23
        instruments whose prices are up to 295 days old by design."""
        add_lot(conn, "ZZDQA", "BUY", date(2026, 1, 5), 5, 100)
        add_lot(conn, "ZZDQA", "SELL", date(2026, 2, 5), 5, 110)
        assert "ZZDQA" not in dq._targeted_symbols(cutoff_at())

    def test_watchlisted_symbols_are_targeted_without_holdings(self, conn, dq):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE instruments SET watchlist = TRUE WHERE symbol = %s", ("ZZDQB",)
            )
        conn.commit()
        assert "ZZDQB" in dq._targeted_symbols(cutoff_at())


class TestSymbolsPricedAt:
    def test_matches_a_run_timestamp_exactly(self, conn, dq):
        """The collector stamps every row of a run with the run's start time,
        so membership is an exact timestamp test, not a window."""
        run_ts = datetime(2026, 8, 12, 13, 55, tzinfo=timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO price_snapshots(ts, symbol, last_price) VALUES (%s,%s,%s)",
                (run_ts, "ZZDQA", 100),
            )
            cur.execute(
                "INSERT INTO price_snapshots(ts, symbol, last_price) VALUES (%s,%s,%s)",
                (run_ts - timedelta(minutes=5), "ZZDQB", 50),
            )
        conn.commit()

        priced = dq._symbols_priced_at(run_ts)
        assert "ZZDQA" in priced
        assert "ZZDQB" not in priced  # got a price, but not in *this* run


class TestClassificationGaps:
    def test_reports_each_missing_field(self, conn, dq):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE instruments SET sector = NULL, country = NULL WHERE symbol = %s",
                ("ZZDQA",),
            )
        conn.commit()
        gaps = dq._classification_gaps(["ZZDQA"])
        assert set(gaps["ZZDQA"]) == {"sector", "country"}

    def test_fully_classified_symbol_is_absent(self, conn, dq):
        assert "ZZDQA" not in dq._classification_gaps(["ZZDQA"])
