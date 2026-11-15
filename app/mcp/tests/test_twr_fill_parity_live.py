"""The dashboard's and the MCP's lot loaders give the same TWR (QA of PR #125).

Reproduction from the QA report, on a real database: one QAOLD bought at 100;
for QANEW, unquoted until the third day, a SELL of one at 120 stored first and
a same-day BUY of two at 100. The dashboard reads lots through
ledger_inputs.load (prepared, BUY before SELL); the MCP returns service reads
them through its own query (unordered, no id or account). The fill-price
fallback once took "the last lot" and so valued QANEW at 120 on one surface
and 100 on the other (MAX TWR 8.18% vs 12.00%). Both must now agree.

Lives in the MCP suite (repo root, `-m slow`) because it calls an MCP service
on its read-only pool; skips when no database is reachable.
"""

from __future__ import annotations

import importlib
from datetime import date

import pytest

importlib.import_module("app.mcp.deps")

import ledger_inputs  # noqa: E402
import twr  # noqa: E402
from db import connect, load_config  # noqa: E402

pytestmark = pytest.mark.slow

OLD, NEW = "ZZTWROLD", "ZZTWRNEW"
D1, D2, D3 = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)
PRICES = {D1: {OLD: 100.0}, D2: {OLD: 100.0}, D3: {OLD: 100.0, NEW: 110.0}}


def _wipe(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lots WHERE symbol IN (%s, %s)", (OLD, NEW))
        cur.execute("DELETE FROM instruments WHERE symbol IN (%s, %s)", (OLD, NEW))
    conn.commit()


@pytest.fixture
def ledger():
    try:
        conn = connect(load_config())
    except Exception as e:
        pytest.skip(f"DB unreachable: {e}")
    try:
        _wipe(conn)
        with conn.cursor() as cur:
            for s in (OLD, NEW):
                cur.execute("INSERT INTO instruments(symbol) VALUES (%s)", (s,))
            rows = [(OLD, "BUY", D1, 1, 100), (NEW, "SELL", D2, 1, 120), (NEW, "BUY", D2, 2, 100)]
            for sym, side, day, qty, price in rows:
                cur.execute(
                    "INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees) "
                    "VALUES (%s, 'A', %s, %s, %s, %s, 0)", (sym, side, day, qty, price))
        conn.commit()
        yield conn
    finally:
        _wipe(conn)
        conn.close()


def _curve(lots):
    mine = [l for l in lots if l["symbol"] in (OLD, NEW)]
    records = twr.build_daily_records(mine, PRICES, [])
    return [r["mv"] for r in records], [round(g, 12) for _, g in twr.growth_curve(records)]


def test_dashboard_and_mcp_loaders_value_the_day_alike(ledger):
    from app.mcp.services import returns as returns_service

    dashboard_lots = [
        {"symbol": r["symbol"], "side": r["side"], "trade_date": r["trade_date"],
         "quantity": float(r["quantity"]), "price": float(r["price"]),
         "fees": float(r["fees"] or 0), "id": r.get("id"), "account": r.get("account")}
        for r in ledger_inputs.load(ledger).lots
    ]
    mcp_lots = returns_service._fetch_lots(None)
    dash, mcp = _curve(dashboard_lots), _curve(mcp_lots)
    assert dash == mcp
    # One QANEW share held after D2, valued at the day's quantity-weighted fill
    # price (120·1 + 100·2) / 3 until its first quote.
    assert dash[0][1] == pytest.approx(100.0 + (120.0 + 200.0) / 3)
