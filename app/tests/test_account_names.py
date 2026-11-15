"""'(none)' is the label for "no account", never an account name (rescore NEW-1).

Every report and the MCP server show lots with ``account IS NULL`` as
``(none)``, and the MCP accepts ``(none)`` back as the filter for them. An
account actually named ``(none)`` used to be accepted by every writer and by
the schema; it then merged into the unassigned bucket, vanished from
allocation by account and lent its cash to that bucket. Here:

* the rule itself (app/account_names.py) and its SQL mirror, the one
  ``pdb_account_fold`` function in sql/schema.sql and migration 007: same
  definition in both files, its character class rendered from the Python
  list, and — run against the database — the same fold as Python over a
  corpus and the same verdict for every BMP character placed in and around
  '(none)' (QA of PR #133, B1: [[:space:]] missed NBSP and friends);
* every writer refuses the name — the Manage page parser and cash writer, the
  shared lot writer, add_lot / sell_lot / set_cash / add_income through their
  argument parsers, the CSV importer per row and up front, and the income
  insert — before anything reaches the database;
* against a real PostgreSQL (skips without one, fails without one when
  PORTFOLIODB_TESTS_REQUIRE_DB is set): the CHECKs on lots, income and
  cash_snapshots refuse it in any case or spacing and still take NULL;
  migration 007 refuses, naming the rows and changing nothing, while a row
  uses the name, applies once they are fixed, and is idempotent; and
  apply_schema.py reports a failing file's own error rather than the
  "current transaction is aborted" its cleanup used to raise over it.

The MCP side — one live ledger reconciled bucket by bucket — is
app/mcp/tests/test_account_identity_live.py.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest
from psycopg2 import errors as pg_errors
from psycopg2 import sql as pgsql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import account_names  # noqa: E402
import add_income  # noqa: E402
import add_lot  # noqa: E402
import apply_schema  # noqa: E402
import import_csv_history  # noqa: E402
import ledger_writes  # noqa: E402
import native_writes  # noqa: E402
import set_cash  # noqa: E402
from db import connect_for_tests  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "sql" / "migrations" / "007_reserved_account_names.sql"
SCHEMA = REPO / "sql" / "schema.sql"

RESERVED_SPELLINGS = [
    "(none)", "(NONE)", "(None)", " (none) ", "( none )", "(\tnone\n)",
    # QA of PR #133, B1: whitespace PostgreSQL's [[:space:]] did not fold.
    "(\u00a0none\u00a0)", "(\u2007none)", "(none\u202f)", "\u00a0(none)\u00a0",
    "(\u3000NONE)", "(\u2028none)", "(no\u200bne)", "\ufeff(none)", "(n\u00adone)",
    "(\x1cnone\x1f)", "(\x85none)",
    # NFKC: fullwidth letters and brackets.
    "\uff08\uff4e\uff4f\uff4e\uff45\uff09", "\uff08NONE\uff09",
]
ALLOWED = ["IBKR", "none", "None", "(merged)", "Broker (none of them)", "(nonexistent)",
           "(nöne)", "(none)x", "(non e1)", "[none]", "(nоne)"]  # the last: Cyrillic о
TABLES = ("lots", "income", "cash_snapshots")


class NoConn:
    """A connection that must never be used: the refusal has to come first."""

    def cursor(self, *a, **k):
        raise AssertionError("the reserved name reached the database layer")


# ── the rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", RESERVED_SPELLINGS)
def test_every_spelling_of_the_label_is_reserved(name):
    assert account_names.is_reserved(name)
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        account_names.normalize_account(name)
    with pytest.raises(argparse.ArgumentTypeError):
        account_names.account_arg(name)


@pytest.mark.parametrize("name", ALLOWED)
def test_ordinary_names_pass_unchanged(name):
    assert not account_names.is_reserved(name)
    assert account_names.normalize_account(f"  {name} ") == name


@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_blank_is_no_account(blank):
    assert account_names.normalize_account(blank) is None
    assert not account_names.is_reserved(blank)


CHECK_EXPR = "pdb_account_fold(account) <> '(none)'"


def _fold_function(text: str) -> str:
    m = re.search(r"CREATE OR REPLACE FUNCTION pdb_account_fold\(.*?\$fold\$;", text, re.S)
    assert m, "pdb_account_fold is not defined"
    return m.group(0)


def test_one_fold_function_in_schema_and_migration_007():
    in_schema = _fold_function(SCHEMA.read_text(encoding="utf-8"))
    assert in_schema == _fold_function(MIGRATION.read_text(encoding="utf-8"))
    for fragment in ("IMMUTABLE STRICT", "normalize(account_name, NFKC)", 'COLLATE "C"'):
        assert fragment in in_schema


def test_the_sql_character_class_is_rendered_from_the_python_list():
    """The class the function removes is exactly account_names.IGNORED_RANGES —
    explicit code points, not the locale-dependent [[:space:]]."""
    fn = _fold_function(SCHEMA.read_text(encoding="utf-8"))
    classes = re.findall(r"'(\[[^']*\])'", fn)
    assert classes == [account_names.sql_char_class()]
    assert "[[:space:]]" not in fn


def test_python_ignores_every_unicode_white_space_character():
    # The Unicode White_Space property (PropList.txt), and everything Python's
    # own str.split() treats as whitespace.
    white_space = {*range(0x09, 0x0E), 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B),
                   0x2028, 0x2029, 0x202F, 0x205F, 0x3000}
    split_ws = {c for c in range(0x110000) if chr(c).isspace() and len(f"a{chr(c)}b".split()) == 2}
    assert {chr(c) for c in white_space | split_ws} <= account_names.IGNORED


def test_schema_and_migration_007_carry_the_same_three_checks():
    schema = SCHEMA.read_text(encoding="utf-8")
    found = dict(re.findall(r"(\w+_account_not_reserved)\s+CHECK \(([^\n]*?)\)\s*[,\n]", schema))
    assert found == {f"{t}_account_not_reserved": CHECK_EXPR for t in TABLES}
    migration = MIGRATION.read_text(encoding="utf-8")
    assert "ARRAY['lots', 'income', 'cash_snapshots']" in migration
    assert f"CHECK ({CHECK_EXPR.replace(chr(39), chr(39) * 2)})" in migration
    # The preflight and the remediation predicates use the same function.
    assert "[[:space:]]', '', 'g')) = '(none)'" not in migration
    assert migration.count("WHERE pdb_account_fold(account) = '(none)'") >= 6


def test_the_sql_rule_reserves_exactly_the_python_list():
    reserved = set(re.findall(r"<> '(\([^']*\))'", CHECK_EXPR))
    assert reserved == set(account_names.RESERVED)


# ── every writer refuses it, before the database ────────────────────────────


@pytest.mark.parametrize("name", RESERVED_SPELLINGS)
def test_manage_page_lot_parser_refuses(name):
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        native_writes.parse_lot(
            symbol="NVDA", account=name, side="BUY", trade_date=date(2026, 1, 2),
            quantity="1", price="10", fees="0", notes=None,
        )


def test_manage_page_lot_parser_still_takes_blank_and_names():
    common = dict(symbol="NVDA", side="BUY", trade_date=date(2026, 1, 2),
                  quantity="1", price="10", fees="0", notes=None)
    assert native_writes.parse_lot(account="  ", **common).account is None
    assert native_writes.parse_lot(account=" IBKR ", **common).account == "IBKR"


@pytest.mark.parametrize("name", RESERVED_SPELLINGS)
def test_manage_page_cash_writer_refuses(name):
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        native_writes.record_cash(NoConn(), name, Decimal("500"), None)


@pytest.mark.parametrize("name", RESERVED_SPELLINGS)
def test_shared_lot_writer_refuses(name):
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        ledger_writes.insert_lot(
            NoConn(), symbol="NVDA", account=name, side="BUY", trade_date=date(2026, 1, 2),
            quantity=Decimal(1), price=Decimal(10), fees=Decimal(0),
        )


def test_record_lot_refuses_with_the_refused_exit_code():
    err = io.StringIO()
    code = add_lot.record_lot(
        NoConn(), symbol="NVDA", account="(none)", side="BUY", trade_date=date(2026, 1, 2),
        qty=Decimal(1), price=Decimal(10), fees=Decimal(0), notes=None, err=err,
    )
    assert code == add_lot.EXIT_REFUSED
    assert "REFUSED" in err.getvalue() and "(none)" in err.getvalue()


def _usage_error(fn, argv):
    """Run a CLI entry point; a usage error is SystemExit(2) and never reaches
    load_config (which would fail first without a password)."""
    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc:
        fn(argv)
    return exc.value.code, err.getvalue()


@pytest.mark.parametrize("name", ["(none)", " ( NONE ) "])
def test_add_and_sell_lot_cli_refuse(name):
    argv = ["--symbol", "NVDA", "--account", name, "--trade-date", "2026-01-02",
            "--qty", "1", "--price", "10"]
    code, err = _usage_error(add_lot.main, argv)
    assert code == 2 and "cannot be used as an account name" in err
    import sell_lot

    code, err = _usage_error(sell_lot.main, argv)
    assert code == 2 and "cannot be used as an account name" in err


def test_set_cash_cli_refuses():
    code, err = _usage_error(set_cash.main, ["--cash", "500", "--account", "(none)"])
    assert code == 2 and "cannot be used as an account name" in err


def test_add_income_cli_refuses():
    argv = ["--symbol", "NVDA", "--account", "(None)", "--pay-date", "2026-01-02",
            "--amount", "3"]
    code, err = _usage_error(add_income.main, argv)
    assert code == 2 and "cannot be used as an account name" in err


def test_income_insert_refuses_before_the_database():
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        add_income._insert_income(
            NoConn(), symbol="NVDA", account="(none)", kind="DIVIDEND", ex_date=None,
            pay_date="2026-01-02", amount=Decimal(3), currency="USD",
            tax_withheld=Decimal(0), per_share=None, source="manual", notes=None,
        )


def _csv_args(default_account):
    return SimpleNamespace(default_account=default_account, allow_non_usd=False,
                           combine_identical_fills=False)


_ROW = {"Symbol": "NVDA", "Trade Date": "20260102", "Purchase Price": "10",
        "Quantity": "1", "Commission": "0", "Comment": "", "Side": "BUY"}


def test_csv_importer_refuses_the_default_account_per_row():
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        import_csv_history._parse_lot(_ROW, "NVDA", _csv_args("(none)"), [])


def test_csv_importer_refuses_a_tagged_account_per_row():
    row = {**_ROW, "Comment": "moved to ( NONE )"}
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        import_csv_history._parse_lot(row, "NVDA", _csv_args("IBKR"), ["( NONE )"])


def test_csv_importer_rejects_the_row_in_validation():
    out = io.StringIO()
    with redirect_stdout(out):
        lots, _snaps, rejected = import_csv_history._validate_file(
            [_ROW], "t.csv", _csv_args("(none)"), []
        )
    assert lots == [] and rejected == 1
    assert "cannot be used as an account name" in out.getvalue()


def test_csv_importer_own_insert_refuses():
    with pytest.raises(ValueError, match="cannot be used as an account name"):
        import_csv_history.insert_lot(NoConn(), "NVDA", "(none)", "2026-01-02", 1, 10, 0, None)


@pytest.mark.parametrize("flags", [["--default-account", "(none)"],
                                   ["--default-account", "IBKR", "--tagged-accounts", "IRA,(NONE)"]])
def test_csv_importer_refuses_up_front(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(sys, "argv", ["import_csv_history.py", "--dir", str(tmp_path), *flags])
    out = io.StringIO()
    with redirect_stdout(out):
        code = import_csv_history.main()
    assert code == import_csv_history.EXIT_FAILED
    assert "Refusing to run" in out.getvalue() and "cannot be used as an account name" in out.getvalue()


# ── the database ────────────────────────────────────────────────────────────

PREFIX = "TSTRA"
SYM = f"{PREFIX}A"


def _wipe(conn) -> None:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lots WHERE symbol = %s", (SYM,))
        cur.execute("DELETE FROM income WHERE symbol = %s", (SYM,))
        cur.execute("DELETE FROM cash_snapshots WHERE account LIKE %s", (f"{PREFIX}%",))
        cur.execute("DELETE FROM instruments WHERE symbol = %s", (SYM,))
    conn.commit()


@pytest.fixture(scope="module")
def conn():
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    try:
        _wipe(c)
        with c.cursor() as cur:
            cur.execute("INSERT INTO instruments(symbol) VALUES (%s)", (SYM,))
        c.commit()
        yield c
    finally:
        try:
            _wipe(c)
        finally:
            c.close()


_INSERT = {
    "lots": ("INSERT INTO lots(symbol, account, side, trade_date, quantity, price) "
             "VALUES (%(sym)s, %(acct)s, 'BUY', '2026-01-02', 1, 10)"),
    "income": ("INSERT INTO income(symbol, account, kind, pay_date, amount) "
               "VALUES (%(sym)s, %(acct)s, 'DIVIDEND', '2026-01-02', 3)"),
    "cash_snapshots": "INSERT INTO cash_snapshots(account, cash) VALUES (%(acct)s, 500)",
}


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("name", RESERVED_SPELLINGS)
def test_the_database_refuses_the_label_as_an_account(conn, table, name):
    with conn.cursor() as cur, pytest.raises(pg_errors.CheckViolation) as exc:
        cur.execute(_INSERT[table], {"sym": SYM, "acct": name})
    conn.rollback()
    assert f"{table}_account_not_reserved" in str(exc.value)


# Run both implementations of the fold over the same strings: the SQL function
# the CHECKs use and account_names.fold the writers and the MCP readers use.
_CORPUS = [
    *RESERVED_SPELLINGS, *ALLOWED,
    "\U0001d427\U0001d428\U0001d427\U0001d41e",           # mathematical bold 'none'
    "(\U0001d427\U0001d428\U0001d427\U0001d41e)",
    "(ÉNONE)", "(NONÉ)", "(İnone)", "(ǅ)", "Kelvin K", "(ß)", "(ﬁ)",
    "(\u180enone)", "(\u2060none)", "(none\u200d)", "(\u205fnone)", "(\u1680none)",
    "(\u200anone)", "(\u000bnone\u000c)", "( ( none ) )", "", " ", "\u00a0", "\u200b",
    "IBKR\u00a0Roth", "Broker\u2007A", "é", "(noné)",
]


def _sql_folds(conn, names):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pdb_account_fold(n), pdb_account_fold(n) = '(none)' "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS t(n, i) ORDER BY i",
            (list(names),),
        )
        rows = cur.fetchall()
    conn.rollback()
    return rows


def test_sql_and_python_fold_identically_over_the_corpus(conn):
    corpus = _CORPUS + [f"(N{ch}one)" for ch in sorted(account_names.IGNORED)]
    for name, (sql_fold, sql_reserved) in zip(corpus, _sql_folds(conn, corpus)):
        assert sql_fold == account_names.fold(name), repr(name)
        assert sql_reserved == account_names.is_reserved(name), repr(name)


def test_sql_and_python_agree_on_every_bmp_character_around_the_label(conn):
    """Every Basic Multilingual Plane character (bar NUL and the surrogates,
    which text cannot hold) inside, beside and around '(none)': the database
    and Python must give the same reserved/not-reserved verdict for each."""
    chars = [chr(c) for c in range(1, 0x10000) if not 0xD800 <= c <= 0xDFFF]
    names = [s for ch in chars for s in (f"({ch}none)", f"(NONE{ch})", f"{ch}(none){ch}")]
    sql = _sql_folds(conn, names)
    mismatched = [n for n, (_f, r) in zip(names, sql) if r != account_names.is_reserved(n)]
    assert mismatched == [], [ascii(n) for n in mismatched[:20]]
    # Every ignored character, in all three positions, is reserved on both sides.
    assert sum(r for _f, r in sql) >= 3 * len(account_names.IGNORED)


@pytest.mark.parametrize("table", ["lots", "income"])
def test_the_database_still_takes_no_account(conn, table):
    with conn.cursor() as cur:
        cur.execute(_INSERT[table], {"sym": SYM, "acct": None})
    conn.rollback()


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("name", ["none", "(nonexistent)", "(merged)"])
def test_the_database_still_takes_names_that_merely_resemble_it(conn, table, name):
    with conn.cursor() as cur:
        cur.execute(_INSERT[table], {"sym": SYM, "acct": name})
    conn.rollback()


@pytest.fixture
def scratch(conn):
    """Minimal lots/income/cash_snapshots tables, without the CHECKs, in a
    throwaway schema: migration 007 runs against these through search_path,
    leaving the shared tables alone."""
    schema = pgsql.Identifier(f"t007_{uuid.uuid4().hex[:8]}")
    c = connect_for_tests(skip=pytest.skip, fail=pytest.fail)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(pgsql.SQL("CREATE SCHEMA {}").format(schema))
        cur.execute(pgsql.SQL("SET search_path = {}").format(schema))
        cur.execute("CREATE TABLE lots (id BIGSERIAL PRIMARY KEY, account TEXT)")
        cur.execute("CREATE TABLE income (id BIGSERIAL PRIMARY KEY, account TEXT)")
        cur.execute("CREATE TABLE cash_snapshots (account TEXT NOT NULL, ts TIMESTAMPTZ DEFAULT now())")
    try:
        yield c
    finally:
        with c.cursor() as cur:
            cur.execute(pgsql.SQL("DROP SCHEMA {} CASCADE").format(schema))
        c.close()


def _migrate(c) -> None:
    with c.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))


def _constraints(c) -> set[str]:
    with c.cursor() as cur:
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE connamespace = current_schema()::regnamespace "
            "AND conname LIKE '%%_account_not_reserved' AND convalidated"
        )
        return {r[0] for r in cur.fetchall()}


def test_migration_007_refuses_while_a_row_uses_the_name_and_changes_nothing(scratch):
    with scratch.cursor() as cur:
        cur.execute("INSERT INTO lots(account) VALUES ('(none)'), ('(none)'), (NULL), ('IBKR')")
        cur.execute("INSERT INTO cash_snapshots(account) VALUES (' (NONE) ')")
    with pytest.raises(pg_errors.CheckViolation) as exc:
        _migrate(scratch)
    with scratch.cursor() as cur:
        cur.execute("ROLLBACK")
    msg = str(exc.value)
    assert "migration 007 refused, nothing changed" in msg
    assert "lots: 2 row(s) with account '(none)'" in msg
    assert "cash_snapshots: 1 row(s) with account ' (NONE) '" in msg
    assert "income" not in msg.split("spacing counts):")[1].split(".")[0]
    assert _constraints(scratch) == set()
    with scratch.cursor() as cur:
        cur.execute("SELECT count(*) FROM lots WHERE account = '(none)'")
        assert cur.fetchone()[0] == 2  # not rewritten


def test_migration_007_applies_once_the_rows_are_fixed_and_is_idempotent(scratch):
    with scratch.cursor() as cur:
        cur.execute("INSERT INTO lots(account) VALUES ('(none)'), (NULL), ('IBKR')")
    with pytest.raises(pg_errors.CheckViolation):
        _migrate(scratch)
    with scratch.cursor() as cur:
        cur.execute("ROLLBACK")
        cur.execute("UPDATE lots SET account = 'Old none' WHERE account = '(none)'")
    _migrate(scratch)
    assert _constraints(scratch) == {f"{t}_account_not_reserved" for t in TABLES}
    _migrate(scratch)  # a second run is a no-op
    with scratch.cursor() as cur, pytest.raises(pg_errors.CheckViolation):
        cur.execute("INSERT INTO income(account) VALUES ('(None)')")


NBSP_NAME, FIGURE_NAME, NARROW_NAME = "(\u00a0none\u00a0)", "(\u2007NONE)", "(none\u202f)"


def test_migration_007_finds_unicode_spaced_rows_and_refuses(scratch):
    """QA of PR #133, B1: a '(none)' padded with no-break, figure or narrow
    no-break spaces passed the [[:space:]] preflight, so 007 applied, validated
    its CHECKs and left rows the readers treat as the label."""
    with scratch.cursor() as cur:
        cur.execute("INSERT INTO lots(account) VALUES (%s), (%s), (NULL)", (NBSP_NAME, NBSP_NAME))
        cur.execute("INSERT INTO income(account) VALUES (%s)", (FIGURE_NAME,))
        cur.execute("INSERT INTO cash_snapshots(account) VALUES (%s)", (NARROW_NAME,))
    with pytest.raises(pg_errors.CheckViolation) as exc:
        _migrate(scratch)
    with scratch.cursor() as cur:
        cur.execute("ROLLBACK")
    msg = str(exc.value)
    assert "lots: 2 row(s)" in msg and "U+00A0" in msg
    assert "income: 1 row(s)" in msg and "U+2007" in msg
    assert "cash_snapshots: 1 row(s)" in msg and "U+202F" in msg
    assert _constraints(scratch) == set()
    with scratch.cursor() as cur:
        cur.execute("SELECT count(*) FROM lots WHERE account = %s", (NBSP_NAME,))
        assert cur.fetchone()[0] == 2  # not rewritten


def test_migration_007_applies_after_the_documented_rename(scratch):
    with scratch.cursor() as cur:
        cur.execute("INSERT INTO lots(account) VALUES (%s), (NULL)", (NBSP_NAME,))
        cur.execute("INSERT INTO income(account) VALUES (%s)", (FIGURE_NAME,))
        cur.execute("INSERT INTO cash_snapshots(account) VALUES (%s)", (NARROW_NAME,))
    with pytest.raises(pg_errors.CheckViolation):
        _migrate(scratch)
    with scratch.cursor() as cur:
        cur.execute("ROLLBACK")
        # The file's own remediation UPDATEs, verbatim. They need the function,
        # which schema.sql creates before any migration runs; the refused run
        # rolled back its own copy, so create it here as schema.sql does.
        cur.execute(_fold_function(SCHEMA.read_text(encoding="utf-8")))
        for line in MIGRATION.read_text(encoding="utf-8").splitlines():
            if line.startswith("--   UPDATE "):
                cur.execute(line[len("--   "):])
    _migrate(scratch)
    assert _constraints(scratch) == {f"{t}_account_not_reserved" for t in TABLES}
    with scratch.cursor() as cur:
        cur.execute("SELECT count(*) FROM lots WHERE account = 'Old none'")
        assert cur.fetchone()[0] == 1
        for table, name in (("lots", NBSP_NAME), ("income", FIGURE_NAME),
                            ("cash_snapshots", NARROW_NAME)):
            with pytest.raises(pg_errors.CheckViolation):
                cur.execute(
                    pgsql.SQL("INSERT INTO {}(account) VALUES (%s)").format(pgsql.Identifier(table)),
                    (name,),
                )


def test_migration_007_replaces_a_draft_constraint_that_used_space_classes(scratch):
    """An earlier draft of 007 (never released) added the CHECKs with
    [[:space:]]; re-applying the file replaces them rather than keeping them
    because the names already exist."""
    with scratch.cursor() as cur:
        cur.execute("ALTER TABLE lots ADD CONSTRAINT lots_account_not_reserved CHECK "
                    "(lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)')")
    _migrate(scratch)
    with scratch.cursor() as cur:
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'lots_account_not_reserved' "
                    "AND connamespace = current_schema()::regnamespace")
        (definition,) = cur.fetchone()
        assert "pdb_account_fold" in definition and "space" not in definition
        with pytest.raises(pg_errors.CheckViolation):
            cur.execute("INSERT INTO lots(account) VALUES (%s)", (NBSP_NAME,))


def test_apply_schema_reports_a_failing_files_own_error(conn, tmp_path, monkeypatch):
    """A file failing inside its own BEGIN used to leave the session aborted,
    so the advisory unlock in run()'s cleanup failed too and its "current
    transaction is aborted" was all the operator saw — for 007, instead of
    the list of rows to fix."""
    sql_dir = tmp_path / "sql"
    (sql_dir / "migrations").mkdir(parents=True)
    name = f"999_refuses_{uuid.uuid4().hex[:6]}.sql"
    (sql_dir / "migrations" / name).write_text(
        "BEGIN;\nDO $$ BEGIN RAISE EXCEPTION 'refused on purpose: fix row 42'; END $$;\nCOMMIT;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(apply_schema, "SQL_DIR", sql_dir)
    monkeypatch.setattr(apply_schema, "BASE_FILES", ())
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = apply_schema.main([])
    assert rc == 1
    assert "refused on purpose: fix row 42" in err.getvalue()
    assert f"FAILED applying sql/migrations/{name}" in err.getvalue()
    assert "current transaction is aborted" not in err.getvalue()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations WHERE filename = %s", (name,))
        assert cur.fetchone()[0] == 0
    conn.commit()
    # The lock was released: a second run is not left waiting on it.
    c = psycopg2.connect(**apply_schema.connect_params(apply_schema.load_config()))
    try:
        with c.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", apply_schema.LOCK_KEY)
            assert cur.fetchone()[0] is True
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", apply_schema.LOCK_KEY)
    finally:
        c.close()
