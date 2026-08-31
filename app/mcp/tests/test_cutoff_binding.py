"""A cutoff must actually bind — every read pinned to the same instant.

Resolving a cutoff is worthless if services keep reading "latest" underneath.
These tests assert the binding rather than the arithmetic: which instant each
underlying reader was handed.

The failure being prevented is silent. The KPI payload reads three price maps —
the cutoff price, the previous snapshot, the previous day's close. If they are
fetched independently and the collector writes between two of them (it runs
every five minutes), market_value comes from the new snapshot while
daily_change is measured against the old one. Every field looks plausible and
the response contains nothing to show it is internally inconsistent.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from app.mcp.services.cutoff import Cutoff

CUTOFF_TS = datetime(2026, 8, 12, 13, 5, tzinfo=timezone.utc)


def make_cutoff() -> Cutoff:
    return Cutoff(
        ts=CUTOFF_TS,
        trade_date=date(2026, 8, 12),
        price_ts_by_symbol={"AAA": CUTOFF_TS},
        coverage_start=date(2025, 9, 22),
        coverage_end=date(2026, 8, 12),
    )


class TestPositionsBinding:
    def test_cutoff_pins_the_price_read(self, env_token, fake_db, monkeypatch):
        from app.mcp.services import positions, prices as prices_service

        captured = {}

        def fake_map(*, as_of_ts=None):
            captured["as_of_ts"] = as_of_ts
            return {}

        monkeypatch.setattr(prices_service, "latest_price_map_with_ts", fake_map)
        monkeypatch.setattr(positions, "_fetch_lots", lambda *a, **kw: [])

        positions.positions_dataframe("fifo", cutoff=make_cutoff())
        assert captured["as_of_ts"] == CUTOFF_TS

    def test_cutoff_pins_the_lot_filter(self, env_token, fake_db, monkeypatch):
        from app.mcp.services import positions, prices as prices_service

        captured = {}

        def fake_fetch(conn, *, account=None, symbol=None, as_of=None):
            captured["as_of"] = as_of
            return []

        monkeypatch.setattr(positions, "_fetch_lots", fake_fetch)
        monkeypatch.setattr(
            prices_service, "latest_price_map_with_ts", lambda **_kw: {}
        )

        positions.positions_dataframe("fifo", cutoff=make_cutoff())
        assert captured["as_of"] == date(2026, 8, 12)

    def test_no_cutoff_leaves_both_unpinned(self, env_token, fake_db, monkeypatch):
        """Existing callers keep the previous read-latest behaviour."""
        from app.mcp.services import positions, prices as prices_service

        captured = {}
        monkeypatch.setattr(
            positions, "_fetch_lots",
            lambda conn, **kw: captured.update(as_of=kw.get("as_of")) or [],
        )
        monkeypatch.setattr(
            prices_service, "latest_price_map_with_ts",
            lambda **kw: captured.update(as_of_ts=kw.get("as_of_ts")) or {},
        )

        positions.positions_dataframe("fifo")
        assert captured["as_of"] is None
        assert captured["as_of_ts"] is None

    def test_explicit_as_of_still_wins_over_the_cutoff(
        self, env_token, fake_db, monkeypatch
    ):
        from app.mcp.services import positions, prices as prices_service

        captured = {}
        monkeypatch.setattr(
            positions, "_fetch_lots",
            lambda conn, **kw: captured.update(as_of=kw.get("as_of")) or [],
        )
        monkeypatch.setattr(
            prices_service, "latest_price_map_with_ts", lambda **_kw: {}
        )

        positions.positions_dataframe(
            "fifo", as_of=date(2026, 1, 1), cutoff=make_cutoff()
        )
        assert captured["as_of"] == date(2026, 1, 1)


class TestKpiBinding:
    """The three price maps behind the KPI tiles must share one instant."""

    def test_all_three_price_maps_get_the_same_instant(
        self, env_token, fake_db, monkeypatch
    ):
        from app.mcp.services import kpis, positions as positions_service
        from app.mcp.services import prices as prices_service

        seen: dict[str, datetime | None] = {}

        def record(name):
            def _fn(*, as_of_ts=None):
                seen[name] = as_of_ts
                return {}
            return _fn

        monkeypatch.setattr(
            prices_service, "prev_day_eod_price_map", record("prev_day")
        )
        monkeypatch.setattr(
            prices_service, "second_latest_price_map", record("second_latest")
        )

        def fake_df(*a, **kw):
            seen["positions_cutoff"] = kw["cutoff"].ts
            return pd.DataFrame([{
                "symbol": "AAA", "qty": 1.0, "open_cost": 10.0, "market_value": 12.0,
                "unrealized_pnl": 2.0, "realized_pnl": 0.0, "last_price": 12.0,
            }])

        monkeypatch.setattr(positions_service, "positions_dataframe", fake_df)
        monkeypatch.setattr(kpis, "_cash_totals", lambda *_a, **_kw: (0.0, []))
        monkeypatch.setattr(kpis, "_watchlist_count", lambda: 0)
        monkeypatch.setattr(kpis, "_income_total", lambda *_a, **_kw: 0.0)

        kpis.portfolio_kpis("fifo", cutoff=make_cutoff())

        assert seen["positions_cutoff"] == CUTOFF_TS
        assert seen["prev_day"] == CUTOFF_TS
        assert seen["second_latest"] == CUTOFF_TS
        # The property, stated directly: one instant everywhere.
        assert len(set(seen.values())) == 1

    def test_payload_reports_the_cutoff_not_the_wall_clock(
        self, env_token, fake_db, monkeypatch
    ):
        from app.mcp.services import kpis, positions as positions_service
        from app.mcp.services import prices as prices_service

        monkeypatch.setattr(
            positions_service, "positions_dataframe", lambda *a, **kw: pd.DataFrame()
        )
        monkeypatch.setattr(
            prices_service, "prev_day_eod_price_map", lambda **_kw: {}
        )
        monkeypatch.setattr(
            prices_service, "second_latest_price_map", lambda **_kw: {}
        )
        monkeypatch.setattr(kpis, "_cash_totals", lambda *_a, **_kw: (0.0, []))
        monkeypatch.setattr(kpis, "_watchlist_count", lambda: 0)
        monkeypatch.setattr(kpis, "_income_total", lambda *_a, **_kw: 0.0)

        out = kpis.portfolio_kpis("fifo", cutoff=make_cutoff())

        assert out["as_of"] == CUTOFF_TS.isoformat()
        assert out["meta"]["as_of"] == CUTOFF_TS.isoformat()
        assert out["meta"]["cost_basis_method"] == "fifo"
        assert out["meta"]["coverage_end"] == "2026-08-12"

    def test_repeated_calls_with_one_cutoff_are_identical(
        self, env_token, fake_db, monkeypatch
    ):
        """Determinism: nothing below the cutoff may read the clock."""
        from app.mcp.services import kpis, positions as positions_service
        from app.mcp.services import prices as prices_service

        monkeypatch.setattr(
            positions_service, "positions_dataframe", lambda *a, **kw: pd.DataFrame()
        )
        monkeypatch.setattr(prices_service, "prev_day_eod_price_map", lambda **_kw: {})
        monkeypatch.setattr(prices_service, "second_latest_price_map", lambda **_kw: {})
        monkeypatch.setattr(kpis, "_cash_totals", lambda *_a, **_kw: (0.0, []))
        monkeypatch.setattr(kpis, "_watchlist_count", lambda: 0)
        monkeypatch.setattr(kpis, "_income_total", lambda *_a, **_kw: 0.0)

        c = make_cutoff()
        # Two separate calls, named, so it is visible that the point is
        # determinism — the same cutoff must produce the same snapshot twice.
        # Written inline it reads as a tautology, and a static analyser calls
        # it one (S5863: same actual and expected expression).
        first = kpis.portfolio_kpis("fifo", cutoff=c)
        second = kpis.portfolio_kpis("fifo", cutoff=c)
        assert first == second


class TestAnalyticsBinding:
    def test_concentration_passes_the_cutoff_through(
        self, env_token, fake_db, monkeypatch
    ):
        from app.mcp.services import analytics, positions as positions_service

        captured = {}

        def fake_positions(*a, **kw):
            captured["cutoff"] = kw.get("cutoff")
            return []

        monkeypatch.setattr(positions_service, "current_positions", fake_positions)
        c = make_cutoff()
        analytics.concentration(10, cutoff=c)
        assert captured["cutoff"] is c

    def test_allocation_passes_the_cutoff_through(
        self, env_token, fake_db, monkeypatch
    ):
        from app.mcp.services import analytics, positions as positions_service

        captured = {}

        def fake_positions(*a, **kw):
            captured["cutoff"] = kw.get("cutoff")
            return []

        monkeypatch.setattr(positions_service, "current_positions", fake_positions)
        c = make_cutoff()
        analytics.allocation_by("sector", cutoff=c)
        assert captured["cutoff"] is c
