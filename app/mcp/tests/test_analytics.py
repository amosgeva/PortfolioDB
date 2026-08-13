"""Analytics service tests — concentration math, sector grouping, correlation edges."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ────────────────────────── concentration ──────────────────────────


def test_concentration_empty_portfolio(env_token, monkeypatch):
    """With nothing priced, concentration is undefined — not zero.

    HHI 0.0 would read as perfect diversification, which is the opposite of
    "we cannot tell". Every undefined metric is null and says why.
    """
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(positions_service, "current_positions", lambda *a, **kw: [])
    out = analytics.concentration(top_n=10)
    assert out["total_positions"] == 0
    assert out["rows"] == []
    for field in ("hhi", "effective_n", "single_largest_pct", "top_n_share_pct"):
        assert out[field] is None
        assert out["null_reasons"][field] == "no_priced_positions"


def test_concentration_hhi_and_effective_n(env_token, monkeypatch):
    from app.mcp.services import analytics, positions as positions_service

    # 4 equally-weighted positions → HHI should be 0.25, effective_n = 4.
    fake_positions = [
        {"symbol": "A", "market_value": 100.0, "weight_pct": 25.0},
        {"symbol": "B", "market_value": 100.0, "weight_pct": 25.0},
        {"symbol": "C", "market_value": 100.0, "weight_pct": 25.0},
        {"symbol": "D", "market_value": 100.0, "weight_pct": 25.0},
    ]
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: fake_positions,
    )
    out = analytics.concentration(top_n=10)
    assert out["total_positions"] == 4
    assert out["hhi"] == pytest.approx(0.25)
    assert out["effective_n"] == pytest.approx(4.0)
    assert out["single_largest_pct"] == 25.0
    assert out["top_n_share_pct"] == pytest.approx(100.0)


def test_concentration_top_n_limit(env_token, monkeypatch):
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: [
            {"symbol": chr(65 + i), "market_value": 100 - i, "weight_pct": 10 - i}
            for i in range(10)
        ],
    )
    out = analytics.concentration(top_n=3)
    assert len(out["rows"]) == 3
    # Sorted by market_value desc
    assert [r["symbol"] for r in out["rows"]] == ["A", "B", "C"]


# ────────────────────────── sector allocation ──────────────────────────


def test_sector_allocation_buckets_by_sector(env_token, monkeypatch, fake_db):
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: [
            {"symbol": "NVDA", "market_value": 500.0},
            {"symbol": "AAPL", "market_value": 300.0},
            {"symbol": "GLD",  "market_value": 200.0},  # ETF / unknown
        ],
    )
    # _sector_map issues one SELECT; fake_db returns sector tuples.
    fake_db(responses=[(
        ["symbol", "sector"],
        [("NVDA", "Information Technology"),
         ("AAPL", "Information Technology")],
    )])

    out = analytics.sector_allocation()
    assert out["total_market_value"] == 1000.0
    sectors = {r["sector"]: r for r in out["rows"]}
    assert sectors["Information Technology"]["market_value"] == 800.0
    assert sectors["Information Technology"]["weight_pct"] == 80.0
    assert sectors["Unknown"]["market_value"] == 200.0
    assert sectors["Unknown"]["symbols"] == ["GLD"]


def test_sector_allocation_empty(env_token, monkeypatch):
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(positions_service, "current_positions", lambda *a, **kw: [])
    out = analytics.sector_allocation()
    assert out == {"total_market_value": 0.0, "rows": []}


def test_sector_allocation_wrapper_stays_sector_keyed(env_token, monkeypatch, fake_db):
    """The backward-compat wrapper must still emit 'sector' keys, not 'key'."""
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: [{"symbol": "NVDA", "market_value": 100.0}],
    )
    fake_db(responses=[(["symbol", "sector"], [("NVDA", "Information Technology")])])
    out = analytics.sector_allocation()
    assert out["rows"][0]["sector"] == "Information Technology"
    assert "key" not in out["rows"][0]


# ────────────────────────── generic allocation ──────────────────────────


def test_allocation_rejects_bad_dimension(env_token):
    from app.mcp.services import analytics
    with pytest.raises(ValueError):
        analytics.allocation_by("planet")


def test_allocation_by_asset_class(env_token, monkeypatch, fake_db):
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: [
            {"symbol": "NVDA", "market_value": 600.0},
            {"symbol": "VOO", "market_value": 400.0},
        ],
    )
    fake_db(responses=[(["symbol", "asset_type"], [("NVDA", "stock"), ("VOO", "etf")])])
    out = analytics.allocation_by("asset_class")
    assert out["dimension"] == "asset_class"
    assert out["total_market_value"] == 1000.0
    byk = {r["key"]: r for r in out["rows"]}
    assert byk["stock"]["market_value"] == 600.0
    assert byk["stock"]["weight_pct"] == pytest.approx(60.0)
    assert byk["etf"]["weight_pct"] == pytest.approx(40.0)


def test_allocation_region_unknown_bucket(env_token, monkeypatch, fake_db):
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: [
            {"symbol": "NVDA", "market_value": 700.0},
            {"symbol": "XYZ", "market_value": 300.0},   # no country row → Unknown
        ],
    )
    fake_db(responses=[(["symbol", "country"], [("NVDA", "USA")])])
    out = analytics.allocation_by("region")
    byk = {r["key"]: r for r in out["rows"]}
    assert byk["USA"]["market_value"] == 700.0
    assert byk["Unknown"]["market_value"] == 300.0
    assert byk["Unknown"]["symbols"] == ["XYZ"]


def test_allocation_by_account(env_token, monkeypatch, fake_db):
    from app.mcp.services import analytics, positions as positions_service
    # SELECT DISTINCT account FROM lots → two accounts
    fake_db(responses=[(["account"], [("IBKR",), ("ROTH",)])])
    summaries = {"IBKR": {"market_value": 800.0}, "ROTH": {"market_value": 200.0}}
    monkeypatch.setattr(
        positions_service, "positions_summary",
        lambda method="fifo", account=None, **kw: summaries[account],
    )
    out = analytics.allocation_by("account")
    assert out["dimension"] == "account"
    assert out["total_market_value"] == 1000.0
    byk = {r["key"]: r for r in out["rows"]}
    assert byk["IBKR"]["weight_pct"] == pytest.approx(80.0)
    assert byk["ROTH"]["weight_pct"] == pytest.approx(20.0)


# ────────────────────────── correlation ──────────────────────────


def test_correlation_rejects_bad_window(env_token):
    from app.mcp.services import analytics
    with pytest.raises(ValueError):
        analytics.correlation_matrix(window="forever")


def test_correlation_rejects_bad_resample(env_token):
    from app.mcp.services import analytics
    with pytest.raises(ValueError):
        analytics.correlation_matrix(resample="monthly")


def test_correlation_returns_empty_for_single_symbol(env_token, monkeypatch):
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: [{"symbol": "NVDA"}],
    )
    out = analytics.correlation_matrix(window="3m")
    assert out["symbols"] == ["NVDA"]
    assert out["matrix"] == {}
    assert out["pairs"] == []


def test_correlation_perfect_correlation_pair(env_token, monkeypatch):
    """Two symbols moving in lockstep should correlate at +1.0."""
    from datetime import date
    import pandas as pd
    from app.mcp.services import analytics

    monkeypatch.setattr(analytics, "_window_since", lambda w: None)

    days = [date(2026, 5, d) for d in range(1, 21)]
    df_rows = []
    for i, d in enumerate(days):
        df_rows.append(("AAA", d, 100 + i))
        df_rows.append(("BBB", d, 200 + 2 * i))  # perfectly proportional
    df = pd.DataFrame(df_rows, columns=["symbol", "day", "last_price"])
    pivot = df.pivot_table(index="day", columns="symbol", values="last_price")
    monkeypatch.setattr(analytics, "_daily_price_frame", lambda *a, **kw: pivot)

    out = analytics.correlation_matrix(["AAA", "BBB"], window="3m", min_observations=5)
    assert out["observations"] >= 5
    assert len(out["pairs"]) == 1
    assert out["pairs"][0]["correlation"] == pytest.approx(1.0, abs=1e-9)


# ────────────────────────── drawdown ──────────────────────────


def test_drawdown_no_data(env_token, monkeypatch):
    from app.mcp.services import analytics
    monkeypatch.setattr(analytics, "_symbol_price_series", lambda *a, **kw: [])
    out = analytics.drawdown_stats("NVDA")
    assert out["observations"] == 0
    assert out["max_drawdown_pct"] == 0.0
    assert out["recovered"] is True


def test_drawdown_known_series(env_token, monkeypatch):
    """A series 100 → 120 → 60 → 90 has max DD 50% and a current DD of 25%."""
    from app.mcp.services import analytics
    series = [
        (datetime(2026, 1, 1, tzinfo=timezone.utc), 100.0),
        (datetime(2026, 2, 1, tzinfo=timezone.utc), 120.0),  # peak
        (datetime(2026, 3, 1, tzinfo=timezone.utc), 60.0),   # trough (-50%)
        (datetime(2026, 4, 1, tzinfo=timezone.utc), 90.0),   # current (-25% vs peak)
    ]
    monkeypatch.setattr(analytics, "_symbol_price_series", lambda *a, **kw: series)
    out = analytics.drawdown_stats("ANY")
    assert out["observations"] == 4
    assert out["max_drawdown_pct"] == pytest.approx(-50.0, abs=1e-9)
    assert out["current_drawdown_pct"] == pytest.approx(-25.0, abs=1e-9)
    assert out["peak"] == 120.0
    assert out["trough"] == 60.0
    assert out["recovered"] is False


def test_drawdown_recovered_flag(env_token, monkeypatch):
    """100 → 50 → 200: max DD 50%, recovered = True (200 >= peak before trough = 100)."""
    from app.mcp.services import analytics
    series = [
        (datetime(2026, 1, 1, tzinfo=timezone.utc), 100.0),
        (datetime(2026, 2, 1, tzinfo=timezone.utc), 50.0),
        (datetime(2026, 3, 1, tzinfo=timezone.utc), 200.0),
    ]
    monkeypatch.setattr(analytics, "_symbol_price_series", lambda *a, **kw: series)
    out = analytics.drawdown_stats("ANY")
    assert out["max_drawdown_pct"] == pytest.approx(-50.0, abs=1e-9)
    assert out["recovered"] is True


def test_position_weights_lightweight(env_token, monkeypatch):
    from app.mcp.services import analytics, positions as positions_service
    monkeypatch.setattr(
        positions_service, "current_positions",
        lambda *a, **kw: [
            {"symbol": "NVDA", "weight_pct": 25.5},
            {"symbol": "VOO",  "weight_pct": 19.7},
        ],
    )
    out = analytics.position_weights()
    assert out == [
        {"symbol": "NVDA", "weight_pct": 25.5},
        {"symbol": "VOO",  "weight_pct": 19.7},
    ]
