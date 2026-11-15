"""One live ledger: every account bucket reconciles with the whole book (rescore NEW-1).

The rescore's probe, on a real PostgreSQL with the MCP's read-only role: through
the Manage page's own parser and writer, a lot without an account went in and
a lot in an account literally named '(none)' went in beside it. The MCP then
reported a $30 book allocated as $10 by account — the named account's $20
gone — and gave the named account's $500 cash as the unassigned bucket's.

Here the same ledger is attempted, plus an ordinary named account with cash and
a cash-only account, on top of the demo ledger:

* the '(none)' writes are refused — by the Manage parser, the Manage cash
  writer, the shared lot writer, the income insert, and by the database for a
  statement that bypasses all of them — and none is stored;
* every bucket the MCP lists ('(none)' = account IS NULL, the named account,
  the cash-only account, the demo accounts) is listed once, filters to its own
  lots, and adds up to the whole book: allocation by account, cash, and the
  portfolio review's summary (value, invested, cash, cost basis, realized,
  unrealized, income, fees).

Needs PORTFOLIODB_PASSWORD (fixtures) and the read-only role. Marked slow, like
the other live-surface modules: `pytest app/mcp/tests/ -m slow`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.slow

PREFIX = "ZZAI"
SYM_A, SYM_B, SYM_PROBE = f"{PREFIX}A", f"{PREFIX}B", f"{PREFIX}P"
SYMS = (SYM_A, SYM_B, SYM_PROBE)
NAMED = f"{PREFIX} Named"
CASH_ONLY = f"{PREFIX} Cash only"
RESERVED = ["(none)", "(NONE)", " ( none ) ",
            # QA of PR #133, B1: no-break, figure and narrow no-break spaces.
            "(\u00a0none\u00a0)", "(\u2007NONE)", "(none\u202f)", "\uff08\uff4e\uff4f\uff4e\uff45\uff09"]

# What the fixture holds, by bucket.
NONE_MV = 1 * 10 + 3 * 25      # A: 1 @ $10 quote; B: 3 @ $25 quote
BASE: dict[str, float] = {}    # the unassigned bucket before the fixture
NAMED_MV = 2 * 10 + 3 * 25     # A: 2; B: 4 bought, 1 sold
NAMED_CASH = 200.0
CASH_ONLY_CASH = 500.0

SUMMARY_KEYS = ("portfolio_value", "invested_market_value", "cash", "cost_basis",
                "realized_pnl", "unrealized_pnl", "income_total", "fees_total")


def _required() -> bool:
    return (os.getenv("PORTFOLIODB_TESTS_REQUIRE_DB") or "").strip().lower() in ("1", "true", "yes")


@pytest.fixture(scope="module")
def admin():
    """A read-write connection, used the way the Manage page uses one."""
    import app.mcp  # noqa: F401  — puts app/ on the path for the bare modules
    import db

    report = pytest.fail if _required() else pytest.skip
    if not (os.getenv("PORTFOLIODB_MCP_RO_USER") and os.getenv("PORTFOLIODB_MCP_RO_PASSWORD")):
        report("the read-only role is not configured (PORTFOLIODB_MCP_RO_*)")
    try:
        conn = db.connect(db.load_config())
    except Exception as e:
        report(f"DB unavailable: {e}")
    yield conn
    conn.close()


def _wipe(conn) -> None:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lots WHERE symbol = ANY(%s)", (list(SYMS),))
        cur.execute("DELETE FROM income WHERE symbol = ANY(%s)", (list(SYMS),))
        cur.execute("DELETE FROM price_snapshots WHERE symbol = ANY(%s)", (list(SYMS),))
        # '(none)' only exists here if a writer let it through (the probe below).
        cur.execute("DELETE FROM cash_snapshots WHERE account IN (%s, %s, '(none)')",
                    (NAMED, CASH_ONLY))
        cur.execute("DELETE FROM instruments WHERE symbol = ANY(%s)", (list(SYMS),))
    conn.commit()


def _lot(account, symbol, side, qty, price, day):
    import native_writes as nw

    return nw.parse_lot(symbol=symbol, account=account, side=side, trade_date=day,
                        quantity=str(qty), price=str(price), fees="1", notes="identity test")


@pytest.fixture(scope="module")
def ledger(admin):
    import add_income
    import native_writes as nw

    from app.mcp.services import income, positions

    _wipe(admin)
    # What the unassigned bucket already holds (none on the demo ledger), so
    # the exact figures below are this fixture's on any database.
    positions.clear_frame_cache()
    BASE["none_mv"] = positions.positions_summary(
        "fifo", account="(none)", include_income=False)["market_value"] or 0.0
    BASE["none_income"] = income.income_summary(group_by="none", account="(none)")["total_income"]
    day = date.today() - timedelta(days=40)
    for lot in (
        _lot("", SYM_A, "BUY", 1, 10, day),            # blank → no account
        _lot(None, SYM_B, "BUY", 3, 20, day),
        _lot(NAMED, SYM_A, "BUY", 2, 10, day),
        _lot(NAMED, SYM_B, "BUY", 4, 22, day),
        _lot(NAMED, SYM_B, "SELL", 1, 24, day + timedelta(days=5)),
    ):
        assert nw.add_lot(admin, lot)
    nw.record_cash(admin, NAMED, Decimal("200"), "identity test")
    nw.record_cash(admin, CASH_ONLY, Decimal("500"), "identity test")
    pay = date.today() - timedelta(days=10)
    for account, symbol, amount in ((None, SYM_A, 3), (NAMED, SYM_B, 7)):
        assert add_income._insert_income(
            admin, symbol=symbol, account=account, kind="DIVIDEND", ex_date=None,
            pay_date=pay, amount=Decimal(amount), currency="USD", tax_withheld=Decimal(0),
            per_share=None, source="test", notes=None,
        ) == 1
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    with admin.cursor() as cur:
        for symbol, price in ((SYM_A, 10), (SYM_B, 25)):
            cur.execute("INSERT INTO price_snapshots(ts, symbol, last_price, source) "
                        "VALUES (%s, %s, %s, 'test')", (now, symbol, price))
    admin.commit()
    positions.clear_frame_cache()
    yield
    _wipe(admin)
    positions.clear_frame_cache()


@pytest.fixture(scope="module")
def server(ledger):
    import importlib

    had = os.environ.get("PORTFOLIODB_MCP_TOKEN")
    os.environ["PORTFOLIODB_MCP_TOKEN"] = had or "account-identity-test-token-0123456789-abcdef"
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
            assert not res.is_error, f"{name}({args}) failed"
            sc = res.structured_content
            return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc

    return asyncio.run(go())


@pytest.fixture(scope="module")
def accounts(server):
    return _call(server, "list_accounts", {})


# ── the probe: the reserved name is refused at every writer and by the database ──


@pytest.mark.parametrize("name", RESERVED)
def test_every_writer_refuses_the_label(admin, ledger, name):
    import add_income
    import ledger_writes
    import native_writes as nw
    from psycopg2 import errors as pg_errors

    day = date.today() - timedelta(days=40)
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        _lot(name, SYM_A, "BUY", 2, 10, day)                      # Manage page
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        nw.record_cash(admin, name, Decimal("500"), None)          # Manage page cash
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        ledger_writes.insert_lot(admin, symbol=SYM_A, account=name, side="BUY",
                                 trade_date=day, quantity=Decimal(2), price=Decimal(10),
                                 fees=Decimal(0))                  # CLIs, Manage page
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        add_income._insert_income(admin, symbol=SYM_A, account=name, kind="DIVIDEND",
                                  ex_date=None, pay_date=day, amount=Decimal(1), currency="USD",
                                  tax_withheld=Decimal(0), per_share=None, source="test",
                                  notes=None)
    admin.rollback()
    for stmt, params in (
        ("INSERT INTO lots(symbol, account, side, trade_date, quantity, price) "
         "VALUES (%s, %s, 'BUY', %s, 2, 10)", (SYM_A, name, day)),
        ("INSERT INTO income(symbol, account, kind, pay_date, amount) "
         "VALUES (%s, %s, 'DIVIDEND', %s, 1)", (SYM_A, name, day)),
        ("INSERT INTO cash_snapshots(account, cash) VALUES (%s, 500)", (name,)),
    ):
        with admin.cursor() as cur, pytest.raises(pg_errors.CheckViolation):
            cur.execute(stmt, params)
        admin.rollback()


def test_no_reserved_name_is_stored(admin, ledger):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM lots WHERE pdb_account_fold(account) = '(none)'),"
            " (SELECT count(*) FROM income WHERE pdb_account_fold(account) = '(none)'),"
            " (SELECT count(*) FROM cash_snapshots WHERE pdb_account_fold(account) = '(none)')"
        )
        assert cur.fetchone() == (0, 0, 0)
        # And by the readers' own rule, over every stored account.
        cur.execute("SELECT account FROM lots UNION SELECT account FROM income "
                    "UNION SELECT account FROM cash_snapshots")
        import account_names

        assert [a for (a,) in cur.fetchall() if account_names.is_reserved(a)] == []
    admin.rollback()


# ── listing and filtering ──


def test_every_bucket_is_listed_once(admin, accounts):
    labels = [a["account"] for a in accounts]
    assert len(labels) == len(set(labels)), labels
    by = {a["account"]: a for a in accounts}
    with admin.cursor() as cur:
        cur.execute("SELECT count(*) FROM lots WHERE account IS NULL")
        null_lots = cur.fetchone()[0]
    admin.rollback()
    assert by["(none)"] == {"account": "(none)", "position_lot_count": null_lots,
                            "has_cash": False, "latest_cash_ts": None}
    assert by[NAMED]["position_lot_count"] == 3 and by[NAMED]["has_cash"]
    assert by[CASH_ONLY]["position_lot_count"] == 0 and by[CASH_ONLY]["has_cash"]


@pytest.mark.parametrize("label", ["(none)", "(NONE)"])
def test_the_label_filters_to_exactly_the_unassigned_lots(server, label):
    lots = _call(server, "get_lots", {"account": label, "symbol": SYM_A, "limit": 50})
    rows = lots["lots"] if isinstance(lots, dict) else lots
    assert [(r["account"], r["quantity"]) for r in rows] == [(None, 1.0)]


def test_a_named_filter_selects_that_account(server):
    pos = {p["symbol"]: p["qty"] for p in _call(server, "get_positions", {"account": NAMED})}
    assert pos == {SYM_A: pytest.approx(2), SYM_B: pytest.approx(3)}
    none = {p["symbol"]: p["qty"] for p in _call(server, "get_positions", {"account": "(none)"})
            if p["symbol"] in (SYM_A, SYM_B)}
    assert none == {SYM_A: pytest.approx(1), SYM_B: pytest.approx(3)}


# ── reconciliation: every bucket adds up to the whole book ──


def test_allocation_by_account_reconciles_with_the_whole_book(server, accounts):
    from app.mcp.services import positions

    whole = positions.positions_summary("fifo", include_income=False)["market_value"]
    alloc = _call(server, "get_allocation", {"dimension": "account"})
    rows = {r["key"]: r["market_value"] for r in alloc["rows"]}
    assert alloc["total_market_value"] == pytest.approx(whole)
    assert sum(rows.values()) == pytest.approx(whole)
    assert rows["(none)"] == pytest.approx(BASE["none_mv"] + NONE_MV)
    assert rows[NAMED] == pytest.approx(NAMED_MV)
    assert CASH_ONLY not in rows
    for a in accounts:
        mv = positions.positions_summary("fifo", account=a["account"],
                                         include_income=False)["market_value"]
        assert rows.get(a["account"], 0.0) == pytest.approx(mv), a["account"]


def test_cash_reconciles_and_the_unassigned_bucket_has_none(server, accounts):
    whole = _call(server, "get_cash_balance", {})
    per = {a["account"]: _call(server, "get_cash_balance", {"account": a["account"]})
           for a in accounts}
    assert sum(p["cash"] for p in per.values()) == pytest.approx(whole["total_cash"])
    assert sorted(r["account"] for r in whole["by_account"]) == sorted(
        a for a, p in per.items() if p["ts"] is not None
    )
    assert per["(none)"]["cash"] == 0.0
    assert per[NAMED]["cash"] == pytest.approx(NAMED_CASH)
    assert per[CASH_ONLY]["cash"] == pytest.approx(CASH_ONLY_CASH)
    assert _call(server, "get_cash_balance", {"account": "(NONE)"})["cash"] == 0.0


def test_the_review_reconciles_bucket_by_bucket(server, accounts):
    whole = _call(server, "get_portfolio_review_snapshot", {"detail_level": "summary"})["summary"]
    per = {a["account"]: _call(server, "get_portfolio_review_snapshot",
                               {"detail_level": "summary", "account": a["account"]})["summary"]
           for a in accounts}
    for key in SUMMARY_KEYS:
        assert sum(s[key] for s in per.values()) == pytest.approx(whole[key], abs=1e-6), key
    none, named, cash_only = per["(none)"], per[NAMED], per[CASH_ONLY]
    assert none["invested_market_value"] == pytest.approx(BASE["none_mv"] + NONE_MV)
    assert none["cash"] == 0.0 and none["cash_by_account"] == []
    assert none["income_total"] == pytest.approx(BASE["none_income"] + 3)
    assert named["invested_market_value"] == pytest.approx(NAMED_MV)
    assert named["cash"] == pytest.approx(NAMED_CASH)
    assert named["income_total"] == pytest.approx(7)
    assert named["realized_pnl"] == pytest.approx(1 * 24 - 1 - (22 + 1 / 4))
    assert cash_only["portfolio_value"] == pytest.approx(CASH_ONLY_CASH)
    assert cash_only["invested_market_value"] == 0.0


# ── the rescore's probe, step by step (last: it adds and removes its own rows) ──


@pytest.mark.parametrize("name", ["(none)", "(\u00a0none\u00a0)"])
def test_the_rescore_probe(admin, server, name):
    """Rescore NEW-1, as run: through the Manage parser and writer, BUY 1 @ $10
    without an account and BUY 2 @ $10 in an account named '(none)'; a $10
    quote and $500 cash in that account; then the MCP's whole book against its
    allocation by account, and the unassigned bucket's cash. On the code the
    rescore read, both writes went in, the book was $30, the allocation $10,
    and the unassigned cash $500. Now the two writes are refused and the
    book, the allocation and the cash agree."""
    import native_writes as nw

    from app.mcp.services import positions

    day = date(2026, 1, 2)
    stored = []
    try:
        assert nw.add_lot(admin, _lot(None, SYM_PROBE, "BUY", 1, 10, day))
        try:
            assert nw.add_lot(admin, _lot(name, SYM_PROBE, "BUY", 2, 10, day))
            stored.append("lot")
        except ValueError:
            pass
        try:
            nw.record_cash(admin, name, Decimal("500"), None)
            stored.append("cash")
        except ValueError:
            pass
        with admin.cursor() as cur:
            cur.execute("INSERT INTO price_snapshots(ts, symbol, last_price, source) "
                        "VALUES (%s, %s, 10, 'test')",
                        (datetime.now(timezone.utc) - timedelta(minutes=1), SYM_PROBE))
        admin.commit()
        positions.clear_frame_cache()

        whole = positions.positions_summary("fifo", include_income=False)["market_value"]
        alloc = _call(server, "get_allocation", {"dimension": "account"})
        assert alloc["total_market_value"] == pytest.approx(whole)
        assert {r["key"]: r["market_value"] for r in alloc["rows"]}["(none)"] == pytest.approx(
            BASE["none_mv"] + NONE_MV + 10
        )
        assert _call(server, "get_cash_balance", {"account": "(none)"})["cash"] == 0.0
        assert stored == [], f"the reserved name was stored: {stored}"
    finally:
        admin.rollback()
        with admin.cursor() as cur:
            cur.execute("DELETE FROM lots WHERE symbol = %s", (SYM_PROBE,))
            cur.execute("DELETE FROM price_snapshots WHERE symbol = %s", (SYM_PROBE,))
            cur.execute("DELETE FROM cash_snapshots WHERE account = %s", (name,))
        admin.commit()
        positions.clear_frame_cache()
