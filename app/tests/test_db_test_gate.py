"""`db.connect_for_tests`: skip on a laptop, fail in the job that has a database.

1.7.3 re-audit, N06: the SQL integration modules skipped themselves when no
database was configured, and the one CI job with a database never ran them, so
"all green" said nothing about them. The gate makes a missing database a
failure when PORTFOLIODB_TESTS_REQUIRE_DB is set. No database here: the two
connection steps are stubbed.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402


class _Skip(Exception):
    pass


class _Fail(Exception):
    pass


def _skip(msg):
    raise _Skip(msg)


def _fail(msg):
    raise _Fail(msg)


@pytest.fixture
def no_flag(monkeypatch):
    monkeypatch.delenv(db.TESTS_REQUIRE_DB_ENV, raising=False)


def test_missing_configuration_skips_by_default(monkeypatch, no_flag):
    monkeypatch.setattr(db, "load_config", lambda: (_ for _ in ()).throw(RuntimeError("PORTFOLIODB_PASSWORD is not set")))
    with pytest.raises(_Skip, match="DB config unavailable"):
        db.connect_for_tests(skip=_skip, fail=_fail)


@pytest.mark.parametrize("flag", ["1", "true", "YES"])
def test_missing_configuration_fails_when_the_database_is_required(monkeypatch, flag):
    monkeypatch.setenv(db.TESTS_REQUIRE_DB_ENV, flag)
    monkeypatch.setattr(db, "load_config", lambda: (_ for _ in ()).throw(RuntimeError("PORTFOLIODB_PASSWORD is not set")))
    with pytest.raises(_Fail, match="DB config unavailable"):
        db.connect_for_tests(skip=_skip, fail=_fail)


def test_unreachable_database_follows_the_same_rule(monkeypatch, no_flag):
    monkeypatch.setattr(db, "load_config", lambda: "cfg")
    monkeypatch.setattr(db, "connect", lambda cfg: (_ for _ in ()).throw(OSError("connection refused")))
    with pytest.raises(_Skip, match="DB unreachable"):
        db.connect_for_tests(skip=_skip, fail=_fail)
    monkeypatch.setenv(db.TESTS_REQUIRE_DB_ENV, "1")
    with pytest.raises(_Fail, match="DB unreachable"):
        db.connect_for_tests(skip=_skip, fail=_fail)


def test_a_reachable_database_is_returned(monkeypatch, no_flag):
    monkeypatch.setattr(db, "load_config", lambda: "cfg")
    monkeypatch.setattr(db, "connect", lambda cfg: ("conn", cfg))
    assert db.connect_for_tests(skip=_skip, fail=_fail) == ("conn", "cfg")


def test_the_gate_is_the_only_skip_in_the_sql_modules():
    """The three modules must not grow their own skip logic back."""
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1]
    for rel in ("tests/test_dedupe_guards.py", "tests/test_fd_store.py", "mcp/tests/test_data_quality_sql.py"):
        text = (app_dir / rel).read_text(encoding="utf-8")
        assert "connect_for_tests(skip=pytest.skip, fail=pytest.fail)" in text, rel
        assert not re.search(r"pytest\.skip\(f?\"DB ", text), f"{rel} skips on its own again"
