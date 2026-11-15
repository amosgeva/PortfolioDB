"""Manage, Advisor, Data Health and Settings, driven through the real script.

Streamlit's own test harness runs ``streamlit_app.py`` against the real
database, presses the real buttons and reads the rows back. Nothing here calls
a page function directly: every assertion is about what the operator would
see and what the ledger then holds.

What it pins, by audit finding:

* A-1   every write and every model call needs the dashboard password; unset
        means read-only; wrong passwords are throttled; the password is never
        echoed; a forced click on a disabled control still writes nothing;
        the news-enrichment fetch is gated while the plain refresh is not.
* A-9   a deletion needs a reason and leaves the full row in lot_deletions.
* N-2   a duplicate fill says "already recorded — not added", never "Added".
* N-3   chat shows the newest messages, in order.
* N-5   rail, tab-bar and iframe navigation are reruns in one session, and
        typed drafts (lot, watchlist, cash, alert, Settings, one-pager)
        come back after a native or iframe round trip.
* N-6   what was typed is what is stored (Decimal); Settings is one transaction.
* N-7   an oversell or a no-account sale is questioned before it is recorded.
* N-10  the brief and the news fetch run off the page's thread, throttled
        process-wide, and land on the page that asked (success or failure)
        without another click.
* U-1   price alerts are persisted, validated and deactivated, never deleted,
        and only armed on a symbol the collector fetches; removing the last
        reason to collect an armed symbol asks first.

Needs the database, like the other SQL modules: skipped without one, a
failure when PORTFOLIODB_TESTS_REQUIRE_DB is set. Writes only rows for the
sentinel symbols and account below (and chat/brief rows it counts from a
recorded high-water mark), and removes them afterwards. No real model call
and no real news fetch: both are replaced in-process.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import llm
import native_auth
import native_jobs
import settings
from db import connect_for_tests

SCRIPT = Path(__file__).resolve().parent.parent / "streamlit_app.py"
PW = "correct horse ✓ battery"
SYM, SYM2 = "ZZW5A", "ZZW5B"
ACCT = "W5ACC"


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def db():
    conn = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    conn.autocommit = True
    marks = {}
    with conn.cursor() as cur:
        for table in ("chat_log", "advisor_briefs"):
            cur.execute(f"SELECT COALESCE(max(id), 0) FROM {table}")  # noqa: S608 — fixed names
            marks[table] = cur.fetchone()[0]
        cur.execute("SELECT key, value FROM settings")
        marks["settings"] = dict(cur.fetchall())
    _wipe(conn)
    try:
        yield conn
    finally:
        _wipe(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_log WHERE id > %s", (marks["chat_log"],))
            cur.execute("DELETE FROM advisor_briefs WHERE id > %s", (marks["advisor_briefs"],))
            cur.execute("DELETE FROM settings")
            for k, v in marks["settings"].items():
                cur.execute("INSERT INTO settings(key, value) VALUES (%s, %s)", (k, v))
        conn.close()


def _wipe(conn) -> None:
    syms = (SYM, SYM2)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM price_alerts WHERE symbol IN %s", (syms,))
        cur.execute("DELETE FROM lots WHERE symbol IN %s", (syms,))
        cur.execute("DELETE FROM lot_deletions WHERE lot->>'symbol' IN %s", (syms,))
        cur.execute("DELETE FROM cash_snapshots WHERE account = %s", (ACCT,))
        cur.execute("DELETE FROM instruments WHERE symbol IN %s", (syms,))


@pytest.fixture(autouse=True)
def isolate(db, monkeypatch):
    """Per test: clean sentinel rows, a fresh throttle and job table, and the
    process environment and settings cache put back afterwards."""
    _wipe(db)
    native_auth.reset_throttle()
    native_jobs.reset()
    saved_env = dict(os.environ)
    saved_settings = (settings._cache, settings._cache_at, settings._db_ok, settings.TTL_SECONDS)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")  # keyless provider: the Advisor renders its controls
    # The suite's conftest freezes settings to env/defaults; these pages are
    # tested against the real settings table, as they run.
    settings.TTL_SECONDS, settings._cache_at = 0.0, 0.0
    yield
    for name in ("brief", "news"):
        native_jobs.wait(name, 10)
    native_jobs.reset()
    native_auth.reset_throttle()
    os.environ.clear()
    os.environ.update(saved_env)
    settings._cache, settings._cache_at, settings._db_ok, settings.TTL_SECONDS = saved_settings


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setenv(native_auth.PASSWORD_ENV, PW)
    return PW


def _run(view: str) -> AppTest:
    at = AppTest.from_file(str(SCRIPT), default_timeout=120)
    at.query_params["view"] = view
    return at.run()


def _ok(at: AppTest) -> AppTest:
    assert not at.exception, [e.value for e in at.exception]
    return at


def _html(at: AppTest) -> str:
    return "\n".join(h.proto.body for h in at.get("html"))


def _all_text(at: AppTest) -> str:
    parts = [_html(at)]
    for kind in ("markdown", "caption", "success", "warning", "error", "info", "text"):
        parts += [str(e.value) for e in getattr(at, kind)]
    return "\n".join(parts)


def _unlock(at: AppTest, pw: str = PW) -> AppTest:
    at.text_input(key="m2_unlock_pw").input(pw)
    return _ok(at.button(key="m2_unlock_submit").click().run())


def _unlocked(view: str) -> AppTest:
    at = _unlock(_run(view))
    assert "unlocked" in _html(at), [e.value for e in at.error]
    return at


def _q(db, sql, params=()):
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _forged(widget):
    """The widget as a client that ignores ``disabled`` would send it: AppTest
    refuses a disabled widget, a hand-written websocket message does not."""
    widget.proto.disabled = False
    return widget


def _fill_lot(at: AppTest, *, symbol=SYM, side="BUY", qty="1", price="10", account=None,
              new_account="", fees="0", notes="") -> AppTest:
    at.text_input(key="m2_lot_sym").input(symbol)
    at.selectbox(key="m2_lot_side").select(side)
    if account is not None:
        at.selectbox(key="m2_lot_acct").select(account)
    at.text_input(key="m2_lot_new_acct").input(new_account)
    at.text_input(key="m2_lot_qty").input(qty)
    at.text_input(key="m2_lot_price").input(price)
    at.text_input(key="m2_lot_fees").input(fees)
    at.text_input(key="m2_lot_notes").input(notes)
    return _ok(_forged(at.button(key="m2_lot_submit")).click().run())


def _seed_lot(db, side="BUY", qty="5", price="10", account=ACCT, trade_date=date(2026, 1, 5)):
    _q(db, "INSERT INTO instruments(symbol) VALUES (%s) ON CONFLICT DO NOTHING", (SYM,))
    rows = _q(db, "INSERT INTO lots(symbol, account, side, trade_date, quantity, price, fees) "
                  "VALUES (%s,%s,%s,%s,%s,%s,0) RETURNING id",
              (SYM, account, side, trade_date, Decimal(qty), Decimal(price)))
    return rows[0][0]


def _lots(db, symbol=SYM):
    return _q(db, "SELECT side, account, quantity, price, fees, notes FROM lots WHERE symbol = %s ORDER BY id",
              (symbol,))


# ── A-1: the gate ──────────────────────────────────────────────────────────


def test_without_a_password_the_write_pages_are_read_only(db):
    at = _ok(_run("manage"))
    assert "read-only" in _html(at)
    assert native_auth.PASSWORD_ENV in _html(at)
    for key in ("m2_lot_submit", "m2_wl_submit", "m2_cash_submit", "m2_alert_submit", "m2_set_submit"):
        assert at.button(key=key).disabled, key
    # A forged click on the disabled control writes nothing (Streamlit drops
    # a disabled widget's value; the handler's own re-check is pinned in
    # test_every_write_rechecks_the_gate_at_the_moment_of_writing).
    at = _fill_lot(at)
    assert _lots(db) == []

    adv = _ok(_run("advisor"))
    assert "read-only" in _html(adv)
    assert adv.button(key="m2_brief").disabled
    assert adv.button(key="m2_chat_reset").disabled
    assert adv.button(key="m2_phil_save").disabled
    assert adv.chat_input(key="m2_chat").proto.disabled


def test_wrong_password_is_refused_throttled_and_never_echoed(db, password):
    at = _ok(_run("manage"))
    assert "locked" in _html(at)
    assert at.button(key="m2_lot_submit").disabled
    guess = "guess-7c1e"
    for _ in range(native_auth.FREE_ATTEMPTS - 1):
        at = _unlock(at, guess)
        assert [e.value for e in at.error] == ["Wrong password."]
        assert at.session_state["m2_unlock_pw"] == ""  # wiped once checked
    at = _unlock(at, guess)
    assert "wait" in at.error[0].value
    # Locked out: the right password is refused without being compared.
    at = _unlock(at, password)
    assert "Too many failed attempts" in at.error[0].value
    assert at.button(key="m2_lot_submit").disabled
    text = _all_text(at)
    assert guess not in text and password not in text


def test_unlock_enables_writes_and_lock_relocks(db, password):
    at = _unlocked("manage")
    assert not at.button(key="m2_lot_submit").disabled
    assert password not in _all_text(at)
    assert at.session_state["m2_unlock_pw"] == ""
    at = _ok(at.button(key="m2_lock").click().run())
    assert "locked" in _html(at) and "unlocked" not in _html(at)
    assert at.button(key="m2_lot_submit").disabled
    at = _fill_lot(at)
    assert _lots(db) == []


def test_every_write_rechecks_the_gate_at_the_moment_of_writing(db, password, monkeypatch):
    """The page renders unlocked, then the check at write time says no — the
    idle timeout landing between render and click. Each handler must refuse."""
    import inspect

    real = native_auth.is_unlocked

    def gate_open_write_closed(session, now=None):
        caller = inspect.stack()[1].function
        return real(session, now) if caller != "require_unlocked" else False

    at = _unlocked("manage")
    monkeypatch.setattr(native_auth, "is_unlocked", gate_open_write_closed)
    at = _fill_lot(at)
    assert any("dashboard password" in e.value for e in at.error)
    at.text_input(key="m2_cash_new").input(ACCT)
    at.text_input(key="m2_cash_amt").input("5")
    at = _ok(at.button(key="m2_cash_submit").click().run())
    at.text_input(key="m2_wl_sym").input(SYM2)
    at = _ok(at.button(key="m2_wl_submit").click().run())
    at.text_input(key="m2_alert_sym").input(SYM2)
    at.text_input(key="m2_alert_level").input("5")
    at.checkbox(key="m2_alert_watch").check()
    at = _ok(at.button(key="m2_alert_submit").click().run())
    at = _ok(at.button(key="m2_set_submit").click().run())
    assert sum("dashboard password" in e.value for e in at.error) >= 1
    assert _lots(db) == []
    assert _q(db, "SELECT count(*) FROM cash_snapshots WHERE account = %s", (ACCT,)) == [(0,)]
    assert _q(db, "SELECT count(*) FROM instruments WHERE symbol = %s", (SYM2,)) == [(0,)]
    assert _q(db, "SELECT count(*) FROM price_alerts WHERE symbol = %s", (SYM2,)) == [(0,)]

    adv = _unlocked("advisor")
    calls = []
    monkeypatch.setattr(native_jobs, "start", lambda *a, **k: calls.append(a))
    adv = _ok(adv.button(key="m2_brief").click().run())
    assert any("dashboard password" in e.value for e in adv.error)
    adv.text_area(key="m2_phil_text").input("should not save")
    adv = _ok(adv.button(key="m2_phil_save").click().run())
    adv = _ok(adv.chat_input(key="m2_chat").set_value("should not send").run())
    assert calls == []
    assert _q(db, "SELECT count(*) FROM settings WHERE value = 'should not save'") == [(0,)]
    assert _q(db, "SELECT count(*) FROM chat_log WHERE content = 'should not send'") == [(0,)]


# ── N-5: navigation is a rerun in the same session ────────────────────────


def test_rail_tabbar_and_iframe_navigation_keep_the_session(db, password):
    at = _unlocked("manage")
    at.session_state["w5_marker"] = "still here"

    at = _ok(at.button(key="m2_nav_advisor").click().run())
    assert at.query_params["view"] == ["advisor"] or at.query_params["view"] == "advisor"
    assert "Advisor" in _html(at) or any("Advisor" in m.value for m in at.markdown)
    assert "unlocked" in _html(at), "the unlock did not survive the move"

    at = _ok(at.button(key="m2_nav_portfolio").click().run())
    frames = at.get("iframe")
    assert any('"initialView": "portfolio"' in f.proto.srcdoc for f in frames)

    # The iframe rail clicks these hidden controls (app.js goNative).
    at = _ok(at.button(key="pdb_nav_health").click().run())
    assert "Data Health" in "\n".join(m.value for m in at.markdown)

    at = _ok(at.button(key="m2_tab_manage").click().run())
    assert "unlocked" in _html(at)
    assert at.session_state["w5_marker"] == "still here"


# ── N-2 / N-6: add lot ─────────────────────────────────────────────────────


def test_added_lot_is_stored_exactly_and_a_duplicate_is_not_reported_as_added(db, password):
    at = _unlocked("manage")
    at = _fill_lot(at, symbol="zzw5a", qty="0.12345678", price="184.37", fees="0.1", notes="first fill")
    assert [s.value for s in at.success] and at.success[0].value.startswith("Added BUY 0.12345678")
    assert _lots(db) == [("BUY", None, Decimal("0.12345678"), Decimal("184.37"), Decimal("0.1"), "first fill")]

    at = _fill_lot(at, symbol=SYM, qty="0.12345678", price="184.37", fees="0.1", notes="second fill")
    assert not any("Added" in s.value for s in at.success)
    assert at.warning and at.warning[0].value.startswith("DUPLICATE — NOT RECORDED")
    assert len(_lots(db)) == 1


@pytest.mark.parametrize("field,value,fragment", [
    ("symbol", "<b>x", "not a valid symbol"),
    ("price", "NaN", "NaN"),
    ("qty", "0", "greater than 0"),
    ("price", "1e15", "too large"),
])
def test_invalid_lot_input_writes_nothing(db, password, field, value, fragment):
    at = _unlocked("manage")
    at = _fill_lot(at, **{field: value})
    assert any(fragment in e.value for e in at.error), [e.value for e in at.error]
    assert _q(db, "SELECT count(*) FROM lots WHERE symbol IN (%s, %s)", (SYM, "<B>X"))[0][0] == 0


# ── N-7: sell warnings before recording ───────────────────────────────────


def test_oversell_is_questioned_and_only_recorded_on_confirmation(db, password):
    _seed_lot(db, qty="5")
    at = _unlocked("manage")
    at = _fill_lot(at, side="SELL", qty="10", price="12", new_account=ACCT)
    assert "Check this lot" in at.warning[0].value
    assert any("holds 5" in m.value for m in at.markdown)
    assert [r[0] for r in _lots(db)] == ["BUY"]  # nothing recorded yet

    at = _ok(at.button(key="m2_sell_cancel").click().run())
    assert [r[0] for r in _lots(db)] == ["BUY"]

    at = _fill_lot(at, side="SELL", qty="10", price="12", new_account=ACCT)
    at = _ok(at.button(key="m2_sell_confirm").click().run())
    assert [r[0] for r in _lots(db)] == ["BUY", "SELL"]
    assert at.success[0].value.startswith("Added SELL 10")


def test_sale_without_account_is_questioned_when_the_shares_are_in_a_named_account(db, password):
    _seed_lot(db, qty="5")
    at = _unlocked("manage")
    at = _fill_lot(at, side="SELL", qty="1", price="12")
    joined = "\n".join(m.value for m in at.markdown)
    assert f"held in {ACCT} (5)" in joined
    assert "in (no account)" in joined
    assert [r[0] for r in _lots(db)] == ["BUY"]


def test_sale_within_the_position_records_without_a_prompt(db, password):
    _seed_lot(db, qty="5")
    at = _unlocked("manage")
    at = _fill_lot(at, side="SELL", qty="2", price="12", new_account=ACCT)
    assert not any("Check this lot" in w.value for w in at.warning)
    assert not any("unreachable" in w.value for w in at.warning), "the settings table should be live here"
    assert [r[0] for r in _lots(db)] == ["BUY", "SELL"]


def test_a_lot_outside_the_usd_scope_is_questioned_like_the_cli_refuses_it(db, password):
    # ledger_writes.currency_scope_problem: the CLIs refuse without
    # --allow-non-usd; the page asks for confirmation instead.
    _q(db, "INSERT INTO instruments(symbol, currency) VALUES (%s, 'EUR')", (SYM2,))
    at = _unlocked("manage")
    at = _fill_lot(at, symbol=SYM2, qty="1", price="10")
    assert "Check this lot" in at.warning[0].value
    assert any("registered as trading in EUR" in m.value and "USD-only" in m.value for m in at.markdown)
    assert _lots(db, SYM2) == []
    at = _ok(at.button(key="m2_sell_confirm").click().run())
    assert len(_lots(db, SYM2)) == 1


def test_alert_collection_rule_matches_the_collector(db):
    """native_writes.is_collected must say exactly what the collector fetches
    (snapshot_prices.held_or_watched_symbols), for every registered symbol."""
    import native_writes as nw
    import snapshot_prices

    _seed_lot(db, qty="5")                                    # held
    _q(db, "INSERT INTO instruments(symbol, watchlist) VALUES (%s, FALSE)", (SYM2,))  # neither
    conn = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        collected = set(snapshot_prices.held_or_watched_symbols(conn))
        symbols = [r["symbol"] for r in nw.fetch_all(conn, "SELECT symbol FROM instruments")]
        mismatches = [s for s in symbols if nw.is_collected(conn, s) != (s in collected)]
    finally:
        conn.close()
    assert SYM in collected and SYM2 not in collected
    assert mismatches == []


# ── A-9: deletion with an audit row ───────────────────────────────────────


def test_delete_needs_a_reason_and_leaves_the_row_in_the_deletion_log(db, password):
    lot_id = _seed_lot(db, qty="1.5", price="42.25")
    at = _unlocked("manage")
    at.number_input(key="m2_del_id").set_value(lot_id)
    at = _ok(at.button(key="m2_del_by_id").click().run())
    assert f"Delete lot #{lot_id}?" in at.warning[0].value

    at = _ok(at.button(key="m2_confirm_del").click().run())
    assert any("reason" in e.value for e in at.error)
    assert len(_lots(db)) == 1

    at.text_input(key="m2_del_reason").input("entered twice")
    at = _ok(at.button(key="m2_confirm_del").click().run())
    assert _lots(db) == []
    (audit_lot_id, row, reason), = _q(
        db, "SELECT lot_id, lot, reason FROM lot_deletions WHERE lot->>'symbol' = %s", (SYM,))
    assert audit_lot_id == lot_id and reason == "entered twice"
    assert Decimal(str(row["quantity"])) == Decimal("1.5")
    assert Decimal(str(row["price"])) == Decimal("42.25")
    assert row["account"] == ACCT and row["side"] == "BUY"
    assert at.success[0].value.startswith(f"Deleted lot #{lot_id}")


def test_lot_deletions_refuses_a_blank_reason_at_the_database(db):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation):
        _q(db, "INSERT INTO lot_deletions(lot_id, lot, reason) VALUES (1, '{}'::jsonb, '   ')")


# ── watchlist, cash ────────────────────────────────────────────────────────


def test_watchlist_add_and_remove(db, password):
    at = _unlocked("manage")
    at.text_input(key="m2_wl_sym").input("zzw5b")
    at = _ok(at.button(key="m2_wl_submit").click().run())
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(True,)]
    at = _ok(at.button(key=f"m2_wl_rm_{SYM2}").click().run())
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(False,)]

    at.text_input(key="m2_wl_sym").input("../x")
    at = _ok(at.button(key="m2_wl_submit").click().run())
    assert any("not a valid symbol" in e.value for e in at.error)


def test_cash_is_stored_exactly(db, password):
    at = _unlocked("manage")
    at.text_input(key="m2_cash_new").input(ACCT)
    at.text_input(key="m2_cash_amt").input("1234.56789012")
    at = _ok(at.button(key="m2_cash_submit").click().run())
    assert _q(db, "SELECT cash FROM cash_snapshots WHERE account = %s", (ACCT,)) == [(Decimal("1234.56789012"),)]


# ── U-1: persisted alerts ─────────────────────────────────────────────────


def test_alerts_are_validated_persisted_and_deactivated(db, password):
    at = _unlocked("manage")
    at.text_input(key="m2_alert_sym").input(SYM2)
    at.text_input(key="m2_alert_level").input("200")
    at = _ok(at.button(key="m2_alert_submit").click().run())
    assert any("not in the ledger" in e.value for e in at.error)
    assert _q(db, "SELECT count(*) FROM price_alerts WHERE symbol = %s", (SYM2,)) == [(0,)]

    at.text_input(key="m2_alert_level").input("0")
    at.checkbox(key="m2_alert_watch").check()
    at = _ok(at.button(key="m2_alert_submit").click().run())
    assert any("greater than 0" in e.value for e in at.error)

    at.text_input(key="m2_alert_level").input("199.5")
    at.selectbox(key="m2_alert_dir").select("below")
    at = _ok(at.button(key="m2_alert_submit").click().run())
    (alert_id, direction, level, active), = _q(
        db, "SELECT id, direction, level, active FROM price_alerts WHERE symbol = %s", (SYM2,))
    assert (direction, level, active) == ("below", Decimal("199.5"), True)
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(True,)]

    # Survives a fresh session (the old alerts vanished on refresh).
    at = _unlocked("manage")
    assert "armed" in _html(at) and SYM2 in _html(at)
    at = _ok(at.button(key=f"m2_alert_off_{alert_id}").click().run())
    assert _q(db, "SELECT active FROM price_alerts WHERE id = %s", (alert_id,)) == [(False,)]
    assert any(e.label.startswith("Inactive alerts (1)") for e in at.expander)


# ── N-6: Settings in one transaction ──────────────────────────────────────


def test_settings_save_writes_together_and_clears_the_stale_base_url(db, password):
    _q(db, "INSERT INTO settings(key, value) VALUES ('llm_base_url', 'https://attacker.example') "
           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    _q(db, "DELETE FROM settings WHERE key = 'display_name'")
    at = _unlocked("manage")
    at.text_input(key="m2_set_name").input("W5 Tester")
    at = _ok(at.button(key="m2_set_submit").click().run())
    assert at.success[0].value == "Settings saved."
    rows = dict(_q(db, "SELECT key, value FROM settings"))
    assert rows.get("display_name") == "W5 Tester"
    assert "llm_base_url" not in rows


def test_settings_validation_errors_save_nothing(db, password):
    before = _q(db, "SELECT key, value FROM settings ORDER BY key")
    at = _unlocked("manage")
    at.text_input(key="m2_set_name").input("Should Not Save")
    at.text_input(key="m2_set_tz").input("Mars/Olympus")
    at = _ok(at.button(key="m2_set_submit").click().run())
    assert any("Unknown timezone" in e.value for e in at.error)
    assert _q(db, "SELECT key, value FROM settings ORDER BY key") == before


def test_save_settings_rolls_back_as_a_whole(db):
    import native_writes as nw

    def get_conn():
        return connect_for_tests(skip=pytest.skip, fail=pytest.fail)

    def put_conn(conn):
        conn.close()

    before = _q(db, "SELECT key, value FROM settings ORDER BY key")
    with pytest.raises(RuntimeError):
        with nw.transaction(get_conn, put_conn) as conn:
            nw.save_settings(conn, {"display_name": "half", "market_week": "1-5"})
            raise RuntimeError("failure after the first fields")
    assert _q(db, "SELECT key, value FROM settings ORDER BY key") == before
    # And the happy path commits every field at once.
    with nw.transaction(get_conn, put_conn) as conn:
        nw.save_settings(conn, {"display_name": "whole", "market_week": "1-5"})
    rows = dict(_q(db, "SELECT key, value FROM settings"))
    assert rows["display_name"] == "whole" and rows["market_week"] == "1-5"


# ── Advisor: one-pager, brief, chat ───────────────────────────────────────


def test_one_pager_save_is_gated(db, password):
    at = _ok(_run("advisor"))
    assert at.button(key="m2_phil_save").disabled
    at = _unlock(at)
    at.text_area(key="m2_phil_text").input("# My rules\n\nNo leverage.")
    at = _ok(at.button(key="m2_phil_save").click().run())
    assert _q(db, "SELECT value FROM settings WHERE key = 'philosophy_md'") == [("# My rules\n\nNo leverage.",)]


def test_brief_runs_in_the_background_and_is_throttled(db, password, monkeypatch):
    release = threading.Event()
    calls = []

    def fake_complete(system_blocks, messages, *, max_tokens=8000):
        calls.append(1)
        release.wait(20)
        return json.dumps({"summary": "W5 summary", "insights": [], "suggestions": [],
                           "markdown": "W5 brief body"})

    monkeypatch.setattr(llm, "complete", fake_complete)
    at = _unlocked("advisor")
    t0 = time.monotonic()
    at = _ok(at.button(key="m2_brief").click().run())
    assert time.monotonic() - t0 < 15, "the page waited for the model"
    assert native_jobs.status("brief").running
    assert "Generating a brief" in _html(at)

    # A second request while one runs (or right after) starts nothing.
    at = _ok(at.button(key="m2_brief").click().run())
    release.set()
    assert native_jobs.wait("brief", 20).state == "done"
    at = _ok(at.run())
    assert any(s.value == "Brief generated." for s in at.success)
    assert any("W5 brief body" in m.value for m in at.markdown)
    at = _ok(at.button(key="m2_brief").click().run())
    assert any("moments ago" in i.value for i in at.info)
    assert len(calls) == 1


def test_chat_shows_the_newest_messages_in_order(db, password):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM chat_log WHERE conversation_id = 'default'")
        existing = cur.fetchone()[0]
        for i in range(1, 106):
            cur.execute("INSERT INTO chat_log(role, content, conversation_id) VALUES (%s, %s, 'default')",
                        ("user" if i % 2 else "assistant", f"w5 message {i}"))
    at = _ok(_run("advisor"))
    shown = [m for m in at.chat_message]
    assert len(shown) == 100
    assert shown[-1].markdown[0].value == "w5 message 105"
    assert shown[0].markdown[0].value == "w5 message 6"
    assert any(f"latest 100 of {existing + 105}" in c.value for c in at.caption)


def test_chat_needs_the_password_and_saves_both_sides(db, password, monkeypatch):
    monkeypatch.setattr(llm, "stream", lambda system_blocks, messages, *, max_tokens=8000: iter(["Hi", " there"]))
    mark = _q(db, "SELECT COALESCE(max(id), 0) FROM chat_log")[0][0]

    at = _ok(_run("advisor"))
    assert at.chat_input(key="m2_chat").proto.disabled
    at = _ok(_forged(at.chat_input(key="m2_chat")).set_value("forged question").run())
    assert _q(db, "SELECT count(*) FROM chat_log WHERE id > %s", (mark,)) == [(0,)]

    at = _unlock(at)
    at = _ok(at.chat_input(key="m2_chat").set_value("how concentrated am I?").run())
    rows = _q(db, "SELECT role, content FROM chat_log WHERE id > %s ORDER BY id", (mark,))
    assert rows == [("user", "how concentrated am I?"), ("assistant", "Hi there")]


# ── N-10 / A-1: the news fetch — gated, non-blocking, delivered ───────────

HEADLINE_URL = "https://example.invalid/w5-headline"


@pytest.fixture
def stale_news(db):
    if not _q(db, "SELECT to_regclass('fd_news') IS NOT NULL")[0][0]:
        pytest.fail("fd_news is missing: apply sql/schema_fd.sql")
    _q(db, "DELETE FROM fd_news WHERE url = %s", (HEADLINE_URL,))
    if _q(db, "SELECT count(*) FROM fd_news WHERE fetched_at > now() - interval '4 hours'")[0][0]:
        pytest.skip("news is fresh in this database, so no fetch would start")
    yield
    _q(db, "DELETE FROM fd_news WHERE url = %s", (HEADLINE_URL,))


class FakeEnrichment:
    """Stands in for `fd_weekly_enrichment.py` behind subprocess.run: blocks
    until released, then (on success) writes one headline the way the real
    script would, so a redraw that picks it up is visible in the page."""

    def __init__(self, db, *, ok=True):
        self.db, self.ok = db, ok
        self.release = threading.Event()
        self.calls = []

    def __call__(self, args, *a, **kw):
        self.calls.append(args)
        self.release.wait(20)
        if not self.ok:
            return subprocess.CompletedProcess(args, 1, "", "vendor said no\nmore detail")
        sym = _q(self.db, "SELECT symbol FROM lots GROUP BY symbol "
                          "HAVING sum(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) > 0 "
                          "ORDER BY symbol LIMIT 1")[0][0]
        _q(self.db, "INSERT INTO fd_news(symbol, url, published_at, source, title, raw) "
                    "VALUES (%s, %s, now(), 'W5', 'W5 fresh headline', '{}'::jsonb)", (sym, HEADLINE_URL))
        return subprocess.CompletedProcess(args, 0, "ok", "")


def _portfolio_unlocked() -> AppTest:
    at = _unlocked("manage")
    return _ok(at.button(key="m2_nav_portfolio").click().run())


def _srcdoc(at: AppTest) -> str:
    return "\n".join(f.proto.srcdoc for f in at.get("iframe"))


def test_news_fetch_is_not_started_without_a_password(db, stale_news, monkeypatch):
    fake = FakeEnrichment(db)
    monkeypatch.setattr(subprocess, "run", fake)
    at = _ok(_run("portfolio"))
    at = _ok(at.button(key="bg_refresh").click().run())
    assert fake.calls == [] and not native_jobs.status("news").running
    assert any("headlines need the dashboard password" in t.value for t in at.toast)
    assert at.get("iframe"), "the ordinary refresh still redraws the page"


def test_news_fetch_is_not_started_while_locked(db, stale_news, password, monkeypatch):
    fake = FakeEnrichment(db)
    monkeypatch.setattr(subprocess, "run", fake)
    at = _ok(_run("portfolio"))
    at = _ok(at.button(key="bg_refresh").click().run())
    assert fake.calls == [] and not native_jobs.status("news").running
    assert any("headlines need the dashboard password" in t.value for t in at.toast)


def test_news_fetch_runs_in_the_background_and_lands_without_another_click(
        db, stale_news, password, monkeypatch):
    import streamlit as st

    fake = FakeEnrichment(db)
    monkeypatch.setattr(subprocess, "run", fake)
    polls = []
    real_fragment = st.fragment

    def spy_fragment(func=None, *, run_every=None, **kw):
        polls.append(run_every)
        return real_fragment(func, run_every=run_every, **kw)

    monkeypatch.setattr(st, "fragment", spy_fragment)

    at = _portfolio_unlocked()
    t0 = time.monotonic()
    at = _ok(at.button(key="bg_refresh").click().run())
    assert time.monotonic() - t0 < 15, "the refresh waited for the fetch"
    assert native_jobs.status("news").running and len(fake.calls) == 1
    # The page that asked is polling the job (a timed fragment rerun).
    assert polls and polls[-1] == pytest.approx(2.0)
    assert "W5 fresh headline" not in _srcdoc(at)

    # Another browser session asks too: nothing new starts.
    other = _portfolio_unlocked()
    _ok(other.button(key="bg_refresh").click().run())
    assert len(fake.calls) == 1

    fake.release.set()
    assert native_jobs.wait("news", 20).state == "done"
    # The fragment's timer fires: a plain rerun of the app, no refresh click.
    at = _ok(at.run())
    assert any(t.value == "Headlines updated." for t in at.toast)
    assert "W5 fresh headline" in _srcdoc(at), "the new headline was not drawn"
    assert len(fake.calls) == 1


def test_news_fetch_failure_is_reported_to_the_page_that_asked(db, stale_news, password, monkeypatch):
    fake = FakeEnrichment(db, ok=False)
    monkeypatch.setattr(subprocess, "run", fake)
    at = _portfolio_unlocked()
    at = _ok(at.button(key="bg_refresh").click().run())
    fake.release.set()
    assert native_jobs.wait("news", 20).state == "failed"
    at = _ok(at.run())
    assert any(t.value == "News refresh failed: vendor said no" for t in at.toast)
    at = _ok(at.run())
    assert not at.toast, "the failure is reported once, not on every rerun"


# ── N-5: drafts survive navigation ────────────────────────────────────────


def test_typed_drafts_survive_native_and_iframe_round_trips(db, password):
    at = _unlocked("manage")
    at.text_input(key="m2_lot_sym").input("DRAFT")
    at.text_input(key="m2_lot_price").input("12.5")
    at.text_input(key="m2_lot_notes").input("half typed")
    at.selectbox(key="m2_lot_side").select("SELL")
    at.text_input(key="m2_wl_sym").input("WLDRAFT")
    at.text_input(key="m2_cash_amt").input("777.70")
    at.text_input(key="m2_alert_sym").input("ALDRAFT")
    at.text_input(key="m2_alert_level").input("42")
    at.text_input(key="m2_set_model").input("draft-model")
    # The click that leaves carries the typed values (no form holds them back).
    at = _ok(at.button(key="m2_nav_advisor").click().run())
    at.text_area(key="m2_phil_text").input("draft rules, not saved")
    at = _ok(at.button(key="m2_nav_portfolio").click().run())   # the iframe half
    assert at.get("iframe")
    at = _ok(at.button(key="pdb_nav_manage").click().run())     # back via the iframe rail
    assert at.text_input(key="m2_lot_sym").value == "DRAFT"
    assert at.text_input(key="m2_lot_price").value == "12.5"
    assert at.text_input(key="m2_lot_notes").value == "half typed"
    assert at.selectbox(key="m2_lot_side").value == "SELL"
    assert at.text_input(key="m2_wl_sym").value == "WLDRAFT"
    assert at.text_input(key="m2_cash_amt").value == "777.70"
    assert at.text_input(key="m2_alert_sym").value == "ALDRAFT"
    assert at.text_input(key="m2_alert_level").value == "42"
    assert at.text_input(key="m2_set_model").value == "draft-model"
    at = _ok(at.button(key="m2_tab_advisor").click().run())
    assert at.text_area(key="m2_phil_text").value == "draft rules, not saved"
    # Drafts are drafts: nothing was written on the way.
    assert _lots(db, "DRAFT") == []
    assert _q(db, "SELECT count(*) FROM settings WHERE value IN ('draft-model', 'draft rules, not saved')") == [(0,)]


def test_a_server_held_draft_survives_advisor_and_back(db, password):
    # The QA reproduction: a value already in session state, then advisor → manage.
    at = _unlocked("manage")
    at.session_state["m2_lot_sym"] = "DRAFT"
    at = _ok(at.run())
    assert at.text_input(key="m2_lot_sym").value == "DRAFT"
    at = _ok(at.button(key="m2_nav_advisor").click().run())
    at = _ok(at.button(key="m2_nav_manage").click().run())
    assert at.text_input(key="m2_lot_sym").value == "DRAFT"


def test_the_password_is_not_kept_as_a_draft(db, password):
    at = _ok(_run("manage"))
    at.text_input(key="m2_unlock_pw").input("not-the-password")
    at = _ok(at.button(key="m2_nav_advisor").click().run())
    at = _ok(at.button(key="m2_nav_manage").click().run())
    assert at.text_input(key="m2_unlock_pw").value in ("", None)


# ── U-1: an alert only on a symbol the collector fetches ──────────────────


def test_alert_on_a_registered_but_uncollected_symbol_is_refused(db, password):
    # In instruments, never held, not watchlisted: the collector skips it.
    _q(db, "INSERT INTO instruments(symbol, watchlist) VALUES (%s, FALSE)", (SYM2,))
    at = _unlocked("manage")
    at.text_input(key="m2_alert_sym").input(SYM2)
    at.text_input(key="m2_alert_level").input("100")
    at = _ok(at.button(key="m2_alert_submit").click().run())
    assert any("not collected" in e.value for e in at.error), [e.value for e in at.error]
    assert _q(db, "SELECT count(*) FROM price_alerts WHERE symbol = %s", (SYM2,)) == [(0,)]

    at.checkbox(key="m2_alert_watch").check()
    at = _ok(at.button(key="m2_alert_submit").click().run())
    assert _q(db, "SELECT count(*) FROM price_alerts WHERE symbol = %s AND active", (SYM2,)) == [(1,)]
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(True,)]


def test_alert_on_a_sold_out_symbol_is_refused_and_on_a_held_one_accepted(db, password):
    _seed_lot(db, qty="5")
    at = _unlocked("manage")
    at.text_input(key="m2_alert_sym").input(SYM)
    at.text_input(key="m2_alert_level").input("100")
    at = _ok(at.button(key="m2_alert_submit").click().run())
    assert _q(db, "SELECT count(*) FROM price_alerts WHERE symbol = %s", (SYM,)) == [(1,)], [e.value for e in at.error]

    _seed_lot(db, side="SELL", qty="5", trade_date=date(2026, 1, 6))  # sold out, never watched
    _q(db, "UPDATE price_alerts SET active = FALSE WHERE symbol = %s", (SYM,))
    at = _ok(at.button(key="m2_alert_submit").click().run())
    assert any("not collected" in e.value for e in at.error)
    assert _q(db, "SELECT count(*) FROM price_alerts WHERE symbol = %s AND active", (SYM,)) == [(0,)]


def test_removing_the_last_reason_to_collect_an_armed_symbol_asks_first(db, password):
    _q(db, "INSERT INTO instruments(symbol, watchlist) VALUES (%s, TRUE)", (SYM2,))
    _q(db, "INSERT INTO price_alerts(symbol, direction, level) VALUES (%s, 'above', 10)", (SYM2,))
    at = _unlocked("manage")
    at = _ok(at.button(key=f"m2_wl_rm_{SYM2}").click().run())
    assert any("armed alert" in w.value for w in at.warning)
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(True,)]

    at = _ok(at.button(key="m2_unwatch_cancel").click().run())
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(True,)]

    at = _ok(at.button(key=f"m2_wl_rm_{SYM2}").click().run())
    at = _ok(at.button(key="m2_unwatch_confirm").click().run())
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(False,)]
    # The armed alert now says it cannot fire.
    assert "not collected" in _html(at)


def test_selling_the_last_shares_of_an_unwatched_armed_symbol_asks_first(db, password):
    _seed_lot(db, qty="5")
    _q(db, "INSERT INTO price_alerts(symbol, direction, level) VALUES (%s, 'below', 8)", (SYM,))
    at = _unlocked("manage")
    at = _fill_lot(at, side="SELL", qty="5", price="12", new_account=ACCT)
    assert any("1 armed alert" in m.value for m in at.markdown)
    assert [r[0] for r in _lots(db)] == ["BUY"]


def test_removing_a_watch_with_no_armed_alerts_needs_no_confirmation(db, password):
    _q(db, "INSERT INTO instruments(symbol, watchlist) VALUES (%s, TRUE)", (SYM2,))
    at = _unlocked("manage")
    at = _ok(at.button(key=f"m2_wl_rm_{SYM2}").click().run())
    assert _q(db, "SELECT watchlist FROM instruments WHERE symbol = %s", (SYM2,)) == [(False,)]


# ── Data Health still renders under the new shell ─────────────────────────


def test_data_health_renders_with_the_rail_and_no_error(db):
    from test_dashboard_apptest import require_ro_role

    require_ro_role()
    at = _ok(_run("health"))
    assert not at.error, [e.value for e in at.error]
    assert at.button(key="m2_nav_health")
    assert "Per-symbol status" in [s.value for s in at.subheader]
