"""CSV importer validation added in 1.8.0, through `_import_file` and `main`.

* E-6: messages cite the real CSV line; an unknown timezone token is an error,
  not New York; tagged accounts match as whole words, not substrings.
* D-2 / A-3: the symbol passes the same rule the database enforces.
* D-1: a row already in the ledger is listed by line; two identical rows in
  one file — two fills the dedupe index would silently merge into one — are
  refused unless --combine-identical-fills records them as one lot.
* F-15: a zero or negative "Current Price" is no price, never a snapshot.
* F-11: a non-US listing is refused unless --allow-non-usd.

Same fakes as test_import_csv_policy.py: a fake `run` and a connection that
records commits and rollbacks.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import import_csv_history as imp  # noqa: E402

HEADER = "Symbol,Trade Date,Side,Purchase Price,Quantity,Commission,Comment,Date,Time,Current Price\n"


class FakeConn:
    def __init__(self):
        self.commits = self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    log: list[tuple[str, tuple]] = []
    existing = {"DUPL"}

    def fake_run(conn, sql, params=None):
        sql_one = " ".join(sql.split())
        log.append((sql_one, tuple(params or ())))
        if sql_one.startswith("INSERT INTO lots") and params[0] in existing:
            return 0
        return 1

    monkeypatch.setattr(imp, "run", fake_run)
    monkeypatch.setattr(imp.oversell, "oversell_warning", lambda *a, **k: None)
    return log


def _args(**kw):
    base = dict(default_account="IBKR", dry_run=False, continue_on_error=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _csv(tmp_path, body, name="trades.csv"):
    p = tmp_path / name
    p.write_text(HEADER + body, encoding="utf-8")
    return str(p)


def _lots(log):
    return [p for s, p in log if s.startswith("INSERT INTO lots")]


def _snaps(log):
    return [p for s, p in log if s.startswith("INSERT INTO price_snapshots")]


GOOD = "AAA,20260213,BUY,100.00,10,1.00,,2026/05/20,16:00 EDT,110.00\n"


# ── E-6: line numbers, timezone tokens, account tags ───────────────


def test_line_numbers_are_the_files_not_the_lists(tmp_path, db, capsys):
    """Two blank lines before the bad row: the reader skips them, so the list
    index said line 3; the file says line 5."""
    body = GOOD + "\n\n" + "BBB,20260213,BUY,100.00,-5,0,,2026/05/20,16:00 EDT,1\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(), [])
    assert counts["rejected"] == 1
    out = capsys.readouterr().out
    assert "trades.csv:5 " in out and "trades.csv:3 " not in out


def test_an_unknown_timezone_token_is_rejected_not_read_as_new_york(tmp_path, db, capsys):
    body = "AAA,20260213,BUY,100.00,10,1.00,,2026/05/20,16:00 CET,110.00\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(), [])
    assert counts["rejected"] == 1 and db == [], "atomic mode: the file is not written"
    out = capsys.readouterr().out
    assert "unrecognised timezone" in out and "trades.csv:2" in out


def test_known_tokens_and_offsets_still_parse():
    from datetime import datetime, timezone
    assert imp.parse_snapshot_ts("2026/05/20", "16:00 UTC") == datetime(2026, 5, 20, 16, tzinfo=timezone.utc)
    assert imp.parse_snapshot_ts("2026/05/20", "16:00 EDT") == datetime(2026, 5, 20, 20, tzinfo=timezone.utc)
    assert imp.parse_snapshot_ts("2026/05/20", "16:00 -05:00") == datetime(2026, 5, 20, 21, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="unrecognised timezone"):
        imp.parse_snapshot_ts("2026/05/20", "16:00 XYZ")


def test_under_continue_on_error_the_lots_go_in_and_the_snapshot_is_skipped(tmp_path, db):
    body = "AAA,20260213,BUY,100.00,10,1.00,,2026/05/20,16:00 CET,110.00\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(continue_on_error=True), [])
    assert counts["inserted"] == 1 and counts["rejected"] == 1
    assert _snaps(db) == []


@pytest.mark.parametrize("comment,expected", [
    ("SMALLIRA rollover", "BrokerA"),     # used to land in IRA
    ("Roth IRA", "IRA"),
    ("ira", "IRA"),
    ("moved to IRA-2", "IRA"),            # '-' is a word boundary
    ("(IBKR) core", "IBKR"),
    ("IBKRX", "BrokerA"),
])
def test_account_tags_match_whole_words(comment, expected):
    assert imp.infer_account(comment, "BrokerA", ["IRA", "IBKR"]) == expected


# ── D-2 / A-3: symbols ─────────────────────────────────────────────


@pytest.mark.parametrize("sym", ["<IMG SRC=X ONERROR=ALERT(1)>", "&#60;B&#62;", "../X", "A B"])
def test_a_markup_symbol_is_rejected_with_its_line(tmp_path, db, capsys, sym):
    """The CSV-to-dashboard XSS path (A-3): a 'symbol' with a price was stored
    and rendered. It is now a parse error, before any database work."""
    body = GOOD + f'"{sym}",20260213,BUY,100.00,10,1.00,,2026/05/20,16:00 EDT,110.00\n'
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(), [])
    assert counts["rejected"] == 1 and db == []
    out = capsys.readouterr().out
    assert "not a valid symbol" in out and "trades.csv:3" in out


def test_a_quote_only_row_with_a_bad_symbol_is_rejected_too(tmp_path, db, capsys):
    body = '"<SCRIPT>",,,,,,,2026/05/20,16:00 EDT,1.00\n'
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(continue_on_error=True), [])
    assert counts["rejected"] == 1 and _snaps(db) == []


def test_symbols_are_normalised(tmp_path, db):
    imp._import_file(FakeConn(), _csv(tmp_path, " brk.b ,20260213,BUY,100.00,10,0,,2026/05/20,16:00 EDT,110\n"), _args(), [])
    assert _lots(db)[0][0] == "BRK.B"


# ── D-1: duplicates are said, not swallowed ────────────────────────


def test_a_row_already_in_the_ledger_is_listed_by_line(tmp_path, db, capsys):
    body = GOOD + "DUPL,20260217,BUY,40.00,2,0.00,,2026/05/20,16:00 EDT,41.00\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(), [])
    assert counts["duplicate"] == 1 and counts["inserted"] == 1 and counts["rejected"] == 0
    out = capsys.readouterr().out
    assert "trades.csv:3 | DUPL BUY 2 @ 40.00 on 2026-02-17 (IBKR): already recorded, not written again" in out


def test_two_identical_rows_in_one_file_are_refused(tmp_path, db, capsys):
    """Two fills of 5 at 100 on one day: the index would keep one and drop the
    other while the import reported success."""
    fill = "AAA,20260213,BUY,100.00,5,0.50,,2026/05/20,16:00 EDT,110.00\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, fill + fill), _args(), [])
    assert counts["rejected"] == 1 and db == []
    out = capsys.readouterr().out
    assert "trades.csv:3" in out and "identical to line 2" in out and "--combine-identical-fills" in out


def test_identical_rows_can_be_combined_into_one_lot(tmp_path, db, capsys):
    fill = "AAA,20260213,BUY,100.00,5,0.50,,2026/05/20,16:00 EDT,110.00\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, fill + fill + fill),
                              _args(combine_identical_fills=True), [])
    assert counts["rejected"] == 0 and counts["inserted"] == 1 and counts["combined"] == 2
    (lot,) = _lots(db)
    assert lot[4] == Decimal("15") and lot[6] == Decimal("1.50")   # quantity, fees summed
    assert "combined with identical line(s) 3, 4" in capsys.readouterr().out


def test_ten_and_ten_point_zero_are_the_same_key(tmp_path, db):
    """Decimal equality, as in the index."""
    rows = ("AAA,20260213,BUY,100,10,0,,2026/05/20,16:00 EDT,110\n"
            "AAA,20260213,BUY,100.00,10.0,0,,2026/05/20,16:00 EDT,110\n")
    counts = imp._import_file(FakeConn(), _csv(tmp_path, rows), _args(), [])
    assert counts["rejected"] == 1


def test_main_exit_code_reflects_an_in_file_duplicate(tmp_path, db, monkeypatch):
    fill = "AAA,20260213,BUY,100.00,5,0.50,,2026/05/20,16:00 EDT,110.00\n"
    _csv(tmp_path, fill + fill)
    monkeypatch.setattr(imp, "load_config", lambda: None)
    monkeypatch.setattr(imp, "connect", lambda cfg: FakeConn())
    monkeypatch.setattr(sys, "argv", ["x", "--dir", str(tmp_path), "--default-account", "IBKR"])
    assert imp.main() == imp.EXIT_FAILED
    monkeypatch.setattr(sys, "argv", ["x", "--dir", str(tmp_path), "--default-account", "IBKR",
                                      "--combine-identical-fills"])
    assert imp.main() == imp.EXIT_OK


# ── F-15: no zero prices ───────────────────────────────────────────


@pytest.mark.parametrize("price", ["0", "0.00", "-1"])
def test_a_non_positive_current_price_is_not_a_snapshot(tmp_path, db, price):
    body = f"AAA,20260213,BUY,100.00,10,1.00,,2026/05/20,16:00 EDT,{price}\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(), [])
    assert counts["inserted"] == 1 and counts["rejected"] == 0
    assert _snaps(db) == []


# ── F-11: USD-only ─────────────────────────────────────────────────


def test_a_non_us_listing_is_rejected(tmp_path, db, capsys):
    body = "TEVA.TA,20260213,BUY,3500,10,0,,2026/05/20,16:00 EDT,3600\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(), [])
    assert counts["rejected"] == 1 and db == []
    assert "non-US listing" in capsys.readouterr().out


def test_allow_non_usd_imports_it(tmp_path, db):
    body = "TEVA.TA,20260213,BUY,3500,10,0,,2026/05/20,16:00 EDT,3600\n"
    counts = imp._import_file(FakeConn(), _csv(tmp_path, body), _args(allow_non_usd=True), [])
    assert counts["inserted"] == 1
