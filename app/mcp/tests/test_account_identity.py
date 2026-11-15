"""The '(none)' label means account IS NULL, and only that, in every reader.

Rescore NEW-1: an account literally named '(none)' could be stored, and the
readers then treated the label as a name in some places and as NULL in
others — the listing coalesced before grouping, allocation de-duplicated the
labels, and the cash reader compared the label with stored names, returning
the named account's cash for the unassigned bucket. The writers and the
database now refuse that name (app/account_names.py, migration 007); these
check the readers' half with the SQL inspected, through the fake cursor. The
live ledger reconciliation is test_account_identity_live.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
CASH_COLS = ["account", "cash", "ts", "currency"]


def test_the_mcp_label_is_the_one_the_writers_reserve():
    import account_names

    from app.mcp.services import common

    assert common.NONE_ACCOUNT == account_names.UNASSIGNED_LABEL
    assert account_names.is_reserved(common.NONE_ACCOUNT)


@pytest.mark.parametrize("label", ["(none)", "(NONE)", " ( none ) ", "(\u00a0none\u202f)",
                                   "(none 2)", "(no\u200bne)"])
def test_every_spelling_of_the_label_filters_to_null(label):
    from psycopg2 import sql

    from app.mcp.services import common
    from app.mcp.tests.conftest import render_sql

    query, params = common.add_account_filter(sql.SQL("SELECT 1 FROM lots WHERE 1=1"), [], label)
    assert render_sql(query).endswith("AND account IS NULL") and params == []
    assert common.account_matches(None, label)
    assert not common.account_matches("(none)", label)  # a stored name never matches the label


@pytest.mark.parametrize("name", ["\uff08none\uff09", "(non\U0001ccda)"])
def test_other_letters_are_a_name_not_the_label(name):
    """QA of PR #133, B2: these folded to '(none)' under Python 3.14's NFKC
    and not under PostgreSQL's. Without Unicode data in the fold they are an
    ordinary name on both sides, bound as one."""
    from psycopg2 import sql

    from app.mcp.services import common

    query, params = common.add_account_filter(sql.SQL("SELECT 1 FROM lots WHERE 1=1"), [], name)
    assert params == [name] and not common.is_none_account(name)
    assert common.account_matches(name, name) and not common.account_matches(None, name)


@pytest.mark.parametrize("label", ["(none)", "(None)"])
def test_cash_for_the_unassigned_bucket_is_none_even_beside_a_stored_literal(fake_db, label):
    """A cash balance always names an account, so the unassigned bucket has
    none — even if a row named '(none)' predates migration 007. The reader
    used to compare the label with stored names and return that row."""
    from app.mcp.services import cash

    fake_db(responses=[(CASH_COLS, [("(none)", 500, NOW, "USD"), ("IBKR", 20, NOW, "USD")])])
    out = cash.latest_cash(NOW, account=label)
    assert out["total"] == 0.0 and out["by_account"] == []
    assert fake_db.executed() == []  # it does not even ask


def test_cash_for_a_named_account_is_still_that_account(fake_db):
    from app.mcp.services import cash

    fake_db(responses=[(CASH_COLS, [("IBKR", 20, NOW, "USD"), ("Other", 7, NOW, "USD")])])
    out = cash.latest_cash(NOW, account="IBKR")
    assert out["total"] == 20.0 and [r["account"] for r in out["by_account"]] == ["IBKR"]


def test_list_accounts_groups_on_the_stored_account_and_labels_null(mcp_server, fake_db):
    """Coalescing before grouping merged a stored '(none)' into the NULL bucket
    and joined that bucket to the named account's cash."""
    import asyncio

    from fastmcp import Client

    fake_db(responses=[(
        ["account", "lot_count", "latest_cash_ts"],
        [(None, 3, None), ("Cash only", 0, NOW), ("IBKR", 2, NOW)],
        ("GROUP BY account", "NULLS FIRST"),
    )])

    async def go():
        async with Client(mcp_server) as client:
            return await client.call_tool("list_accounts", {})

    res = asyncio.run(go())
    rows = res.structured_content["result"]
    sql_text = fake_db.executed()[0][0]
    assert "COALESCE(account, '(none)')" not in sql_text
    assert rows[0] == {"account": "(none)", "position_lot_count": 3, "has_cash": False,
                       "latest_cash_ts": None}
    assert [r["account"] for r in rows] == ["(none)", "Cash only", "IBKR"]


def test_allocation_by_account_filters_with_the_shared_rule(env_token, fake_db, monkeypatch):
    """Any spelling of the label selects the NULL bucket, and a named filter
    its own; each bucket is valued through the same filter every reader uses."""
    from app.mcp.services import analytics, positions

    by_label = {"A": 100.0, "(none)": 50.0}
    seen = []

    def summary(method="fifo", account=None, **kw):
        seen.append(account)
        return {"market_value": by_label[account]}

    monkeypatch.setattr(positions, "positions_summary", summary)
    for flt, expected in [(None, {"A": 100.0, "(none)": 50.0}),
                          ("(NONE)", {"(none)": 50.0}),
                          ("A", {"A": 100.0})]:
        fake_db(responses=[(["account"], [("A",), (None,)], "DISTINCT account FROM lots")])
        out = analytics.allocation_by("account", account=flt)
        assert {r["key"]: r["market_value"] for r in out["rows"]} == expected, flt
        assert out["total_market_value"] == sum(expected.values())
    assert set(seen) == {"A", "(none)"}
