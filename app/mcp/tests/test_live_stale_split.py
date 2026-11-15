"""An approved split, and only a pre-split (stale) quote — through the public tools.

QA of PR #126, baseline defect: the positions frame restates a stale quote by
comparing its timestamp with the split's boundary, but the latest-price map it
read handed over the serialised timestamp (an ISO string). The restatement was
skipped, and 10 shares bought at 100 before a 2:1 split were valued as 20
restated shares at the unadjusted 100 — 2,000 instead of 1,000.

Fixture: one symbol, BUY 10 @ 100 forty days ago, a single quote of 100 thirty
days ago, a reviewed 2:1 split twenty days ago, and no quote since. Every
public surface that values the position must say 20 shares at 50 = 1,000.

Needs PORTFOLIODB_PASSWORD (to insert fixtures) and the read-only role.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.slow

SYM = "ZZSTALE"
ACCT = "ZZSTALE-ACCT"


def _required() -> bool:
    return (os.getenv("PORTFOLIODB_TESTS_REQUIRE_DB") or "").strip().lower() in ("1", "true", "yes")


@pytest.fixture(scope="module")
def stale_split():
    import db

    report = pytest.fail if _required() else pytest.skip
    if not (os.getenv("PORTFOLIODB_MCP_RO_USER") and os.getenv("PORTFOLIODB_MCP_RO_PASSWORD")):
        report("the read-only role is not configured (PORTFOLIODB_MCP_RO_*)")
    try:
        conn = db.connect(db.load_config())
    except Exception as e:
        report(f"DB unavailable: {e}")
    conn.autocommit = True
    now = datetime.now(timezone.utc)

    def wipe(cur):
        cur.execute("DELETE FROM corporate_actions WHERE symbol = %s", (SYM,))
        cur.execute("DELETE FROM price_snapshots WHERE symbol = %s", (SYM,))
        cur.execute("DELETE FROM lots WHERE symbol = %s", (SYM,))
        cur.execute("DELETE FROM instruments WHERE symbol = %s", (SYM,))

    with conn.cursor() as cur:
        wipe(cur)
        cur.execute("INSERT INTO instruments(symbol, sector, country, asset_type) "
                    "VALUES (%s, 'Tech', 'US', 'stock')", (SYM,))
        cur.execute("INSERT INTO lots(symbol, account, side, trade_date, quantity, price) "
                    "VALUES (%s, %s, 'BUY', %s, 10, 100)",
                    (SYM, ACCT, now.date() - timedelta(days=40)))
        cur.execute("INSERT INTO price_snapshots(ts, symbol, last_price, source) "
                    "VALUES (%s, %s, 100, 'test')", (now - timedelta(days=30), SYM))
        cur.execute("INSERT INTO corporate_actions(symbol, kind, ex_date, ratio, source, reviewed) "
                    "VALUES (%s, 'SPLIT', %s, 2, 'manual', TRUE)",
                    (SYM, now.date() - timedelta(days=20)))
    from app.mcp.services import positions

    positions.clear_frame_cache()
    yield
    with conn.cursor() as cur:
        wipe(cur)
    conn.close()
    positions.clear_frame_cache()


@pytest.fixture(scope="module")
def server(stale_split):
    import importlib

    had = os.environ.get("PORTFOLIODB_MCP_TOKEN")
    os.environ["PORTFOLIODB_MCP_TOKEN"] = had or "stale-split-test-token-0123456789-abcdef"
    try:
        yield importlib.import_module("app.mcp.server").build_server()
    finally:
        if had is None:
            os.environ.pop("PORTFOLIODB_MCP_TOKEN", None)


def _call(server, name, args):
    from fastmcp import Client

    async def go():
        async with Client(server) as client:
            res = await client.call_tool(name, args, raise_on_error=False)
            assert not res.is_error, " ".join(getattr(c, "text", "") for c in res.content)
            sc = res.structured_content
            if isinstance(sc, dict) and set(sc) == {"result"}:
                return sc["result"]
            if sc is not None:
                return sc
            return json.loads(" ".join(getattr(c, "text", "") for c in res.content))

    return asyncio.run(go())


def test_get_positions_restates_the_stale_quote(server):
    rows = _call(server, "get_positions", {"account": ACCT})
    [row] = [r for r in rows if r["symbol"] == SYM]
    assert row["qty"] == pytest.approx(20.0)
    assert row["last_price"] == pytest.approx(50.0)
    assert row["market_value"] == pytest.approx(1000.0)
    assert row["unrealized_pnl"] == pytest.approx(0.0)


def test_unrealized_pnl_and_review_value_it_at_1000(server):
    unreal = _call(server, "get_unrealized_pnl", {"account": ACCT})
    [row] = [r for r in unreal["rows"] if r["symbol"] == SYM]
    assert row["market_value"] == pytest.approx(1000.0)
    review = _call(server, "get_portfolio_review_snapshot",
                   {"account": ACCT, "detail_level": "summary"})
    assert review["summary"]["invested_market_value"] == pytest.approx(1000.0)


def test_latest_price_says_both_what_was_observed_and_what_it_is_now(server):
    q = _call(server, "get_latest_price", {"symbol": SYM})
    assert q["last_price"] == pytest.approx(100.0)               # as observed
    assert q["split_adjusted_last_price"] == pytest.approx(50.0)  # in today's units


def test_pre_trade_prompt_prices_in_post_split_units(server):
    from fastmcp import Client

    async def go():
        async with Client(server) as client:
            return await client.get_prompt(
                "pre_trade_check", {"symbol": SYM, "side": "BUY", "qty": "2"}
            )

    text = " ".join(getattr(m.content, "text", "") for m in asyncio.run(go()).messages)
    # 2 shares at the restated 50, not at the pre-split 100.
    assert "notional $100.00" in text
    assert "notional $200.00" not in text, "the trade was priced at the pre-split quote"
