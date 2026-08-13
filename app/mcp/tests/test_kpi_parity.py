"""Engine-parity test: MCP KPIs must match the Streamlit dashboard's math.

The dashboard computes its KPI tiles with inline pandas math at lines
507-579 of streamlit_app.py. The MCP server replicates that math in
app/mcp/services/kpis.py. This test runs both paths over a deterministic
fixture and asserts every KPI tile matches to the cent.

If this ever fails, treat it as a serious incident — the dashboard and the
MCP server should never disagree about the user's portfolio.

The fixture builds an in-memory FakeConn that returns predetermined rows
for every SELECT the two pipelines run, so the test is hermetic.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

# ────────────────────────── deterministic fixture ──────────────────────────
#
# Two accounts (IBKR, Blink), four symbols (NVDA, AAPL, VOO, IAU). NVDA is
# split across accounts (cross-account merge path). One symbol (IAU) is on
# watchlist but not held. VOO has a SELL to exercise realized P&L. AAPL has
# no snapshot (exercise NaN price/market value path).

NOW = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
YESTERDAY_EOD = datetime(2026, 5, 19, 20, 30, tzinfo=timezone.utc)
PREV_SNAPSHOT = datetime(2026, 5, 20, 18, 45, tzinfo=timezone.utc)  # second-latest
LATEST_SNAPSHOT = NOW                                               # latest

LOTS = [
    # (id, symbol, account, side, trade_date, qty, price, fees)
    (1, "NVDA", "IBKR",  "BUY",  date(2025, 1, 10), "5", "120.00", "1.00"),
    (2, "NVDA", "IBKR",  "BUY",  date(2025, 6, 5),  "3", "140.00", "0.50"),
    (3, "NVDA", "Blink", "BUY",  date(2025, 4, 1),  "2", "130.00", "0.00"),
    (4, "AAPL", "IBKR",  "BUY",  date(2025, 2, 15), "10", "180.00", "1.00"),
    (5, "VOO",  "IBKR",  "BUY",  date(2024, 12, 1), "10", "440.00", "0.00"),
    (6, "VOO",  "IBKR",  "SELL", date(2026, 3, 20), "3",  "480.00", "0.50"),
    # IAU watchlist only — no lots
]

# Symbols ordered the way Postgres would return them (alphabetic).
HELD_SYMBOLS = ["AAPL", "NVDA", "VOO"]                # SYMBOL has open qty
LATEST_PRICES = {
    "NVDA": 165.50,   # snapshot present
    "VOO":  500.00,   # snapshot present
    # AAPL: NO snapshot — exercises NaN path
    "IAU":  60.00,    # watchlist, no qty — snapshot exists but doesn't contribute
}
PREV_DAY_PRICES = {
    "NVDA": 163.00,
    "VOO":  498.00,
}
SECOND_LATEST_PRICES = {
    "NVDA": 165.00,
    "VOO":  500.50,  # gained 0.50 since prev snapshot
}

CASH_ROWS = [
    # (account, cash, ts)  — order DESC by ts; first per account wins
    ("IBKR",  "500.00", datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)),
    ("Blink", "150.00", datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)),
    # earlier rows that should be ignored
    ("IBKR",  "200.00", datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)),
]
WATCHLIST_COUNT = 2  # arbitrary


# ────────────────────────── canonical (dashboard) implementation ─────────────


def _dashboard_kpis() -> dict[str, Any]:
    """A line-for-line port of streamlit_app.py:507-579 over the fixture.

    Computes KPIs the same way the dashboard does, in pandas, using the same
    fixture data the MCP service sees. This is the oracle the parity test
    compares against.
    """
    # Build fifo_pos by running the engine on LOTS.
    from portfolio import compute_fifo_merged
    lot_rows = [
        {"id": r[0], "symbol": r[1], "account": r[2], "side": r[3],
         "trade_date": r[4], "quantity": Decimal(r[5]),
         "price": Decimal(r[6]), "fees": Decimal(r[7])}
        for r in LOTS
    ]
    fifo_pos = compute_fifo_merged(lot_rows)

    # Build latest snapshot DataFrame.
    latest_rows = [
        {"symbol": sym, "last_price": Decimal(str(p))}
        for sym, p in LATEST_PRICES.items()
    ]
    latest = pd.DataFrame(latest_rows)

    merged = pd.DataFrame()
    if not fifo_pos.empty and not latest.empty:
        merged = fifo_pos.merge(latest[["symbol", "last_price"]], on="symbol", how="left")
        merged["last_price"] = pd.to_numeric(merged["last_price"], errors="coerce")
        merged["qty"] = pd.to_numeric(merged["qty"], errors="coerce")
        merged["open_cost"] = pd.to_numeric(merged["open_cost"], errors="coerce")
        merged["market_value"] = merged["qty"] * merged["last_price"]
        merged["unrealized_pnl"] = merged["market_value"] - merged["open_cost"]

    if not merged.empty:
        total_value = float(merged["market_value"].sum(skipna=True))
        total_cost = float(merged["open_cost"].sum())
        total_unrl = float(merged["unrealized_pnl"].sum(skipna=True))
        total_unrl_pct = (total_unrl / total_cost * 100) if total_cost else 0.0
        active_count = int((merged["qty"] > 0).sum())
    else:
        total_value = total_cost = total_unrl = 0.0
        total_unrl_pct = 0.0
        active_count = 0

    realized_total = float(fifo_pos["realized_pnl"].sum()) if not fifo_pos.empty else 0.0

    # Cash — keep first row per account by ts DESC.
    cash_latest: dict[str, float] = {}
    for acct, cash, _ts in CASH_ROWS:
        key = acct or "(merged)"
        if key not in cash_latest:
            cash_latest[key] = float(cash)
    merged_cash = sum(cash_latest.values())

    aum_total = total_value + merged_cash
    total_return_pct = ((realized_total + total_unrl) / total_cost * 100) if total_cost else 0.0

    daily_change_dollar = 0.0
    last_snapshot_delta = 0.0
    prev_total_value = 0.0
    if not merged.empty:
        for _, row in merged.iterrows():
            sym = row["symbol"]
            qty = float(row["qty"])
            lastp = float(row["last_price"]) if pd.notna(row["last_price"]) else 0.0
            prevp = PREV_DAY_PRICES.get(sym)
            if prevp is not None:
                daily_change_dollar += qty * (lastp - prevp)
                prev_total_value += qty * prevp
            snap2_p = SECOND_LATEST_PRICES.get(sym)
            if snap2_p is not None:
                last_snapshot_delta += qty * (lastp - snap2_p)
    daily_change_pct = (daily_change_dollar / prev_total_value * 100) if prev_total_value else 0.0

    return {
        "aum": aum_total,
        "market_value": total_value,
        "cost_basis": total_cost,
        "cash": merged_cash,
        "unrealized_pnl": total_unrl,
        "unrealized_pct": total_unrl_pct,
        "realized_pnl": realized_total,
        "total_return_pct": total_return_pct,
        "daily_change_usd": daily_change_dollar,
        "daily_change_pct": daily_change_pct,
        "delta_last_snapshot_usd": last_snapshot_delta,
        "active_symbols": active_count,
        "watchlist_count": WATCHLIST_COUNT,
    }


# ────────────────────────── DB fixture wiring ──────────────────────────


@pytest.fixture
def patched_services(monkeypatch, env_token, fake_db):
    """Patch every DB-touching primitive used by services/kpis.py.

    The kpis service composes three other services (positions, prices) plus
    its own direct cash + watchlist queries, so we patch the inner functions
    rather than trying to drive everything through fake_db sequencing.
    """
    from app.mcp.services import kpis as kpis_service
    from app.mcp.services import positions as positions_service
    from app.mcp.services import prices as prices_service

    # 1) Lots fetch returns our fixture (used by positions_service).
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

    # 2) Latest prices used by positions_service.positions_dataframe.
    def fake_latest_price_map_with_ts(**_kw):
        return {
            sym: {"last_price": p, "ts": LATEST_SNAPSHOT.isoformat()}
            for sym, p in LATEST_PRICES.items()
        }
    monkeypatch.setattr(
        prices_service, "latest_price_map_with_ts", fake_latest_price_map_with_ts
    )
    monkeypatch.setattr(
        prices_service, "latest_price_map",
        lambda **_kw: {s: float(p) for s, p in LATEST_PRICES.items()},
    )

    # 3) Prev-day + second-latest maps used by kpis_service.
    monkeypatch.setattr(
        prices_service, "prev_day_eod_price_map",
        lambda **_kw: {s: float(p) for s, p in PREV_DAY_PRICES.items()},
    )
    monkeypatch.setattr(
        prices_service, "second_latest_price_map",
        lambda **_kw: {s: float(p) for s, p in SECOND_LATEST_PRICES.items()},
    )

    # 4) Cash + watchlist queries used directly by kpis_service.
    monkeypatch.setattr(
        kpis_service, "_cash_totals",
        lambda *_a, **_kw: (
            150.0 + 500.0,
            [
                {"account": "IBKR", "cash": 500.0, "ts": CASH_ROWS[0][2].isoformat()},
                {"account": "Blink", "cash": 150.0, "ts": CASH_ROWS[1][2].isoformat()},
            ],
        ),
    )
    monkeypatch.setattr(kpis_service, "_watchlist_count", lambda: WATCHLIST_COUNT)

    # Pin the cutoff. kpis resolves one itself when not passed one, and that
    # goes to the database — which made this "parity" test quietly require a
    # live Postgres plus the read-only role. Pinning it also states the instant
    # the two sides are compared at, instead of leaving it as "now".
    from app.mcp.services.cutoff import Cutoff

    fixed_cutoff = Cutoff(ts=NOW, trade_date=NOW.date())
    monkeypatch.setattr(
        kpis_service.cutoff_service, "resolve", lambda *_a, **_kw: fixed_cutoff
    )

    # Income is a separate additive term added after this parity oracle was
    # written; pin it to a fixed total so the new KPI fields are deterministic
    # without the dashboard oracle (which has no income concept) diverging.
    monkeypatch.setattr(kpis_service, "_income_total", lambda *_a, **_kw: 30.0)


# ────────────────────────── the parity test ──────────────────────────


PARITY_KEYS = [
    "aum",
    "market_value",
    "cost_basis",
    "cash",
    "unrealized_pnl",
    "unrealized_pct",
    "realized_pnl",
    "total_return_pct",
    "daily_change_usd",
    "daily_change_pct",
    "delta_last_snapshot_usd",
    "active_symbols",
    "watchlist_count",
]


def test_mcp_kpis_match_dashboard_math(patched_services):
    from app.mcp.services import kpis as kpis_service

    expected = _dashboard_kpis()
    actual = kpis_service.portfolio_kpis("fifo")

    failed = []
    for key in PARITY_KEYS:
        e, a = expected[key], actual[key]
        if isinstance(e, float):
            if abs(e - a) > 1e-9:
                failed.append(f"{key}: dashboard={e!r}  mcp={a!r}")
        else:
            if e != a:
                failed.append(f"{key}: dashboard={e!r}  mcp={a!r}")
    assert not failed, "KPI parity broken:\n  " + "\n  ".join(failed)

    # Dividend additions (separate metric — existing total_return_pct above is
    # asserted unchanged via PARITY_KEYS). income_total is pinned to 30.0 by
    # the fixture; total_return_with_income_pct folds it into the numerator.
    assert actual["income_total"] == pytest.approx(30.0)
    expected_twi = (
        (expected["realized_pnl"] + expected["unrealized_pnl"] + 30.0)
        / expected["cost_basis"] * 100.0
    )
    assert actual["total_return_with_income_pct"] == pytest.approx(expected_twi)
