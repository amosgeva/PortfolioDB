"""Tests for the read-only role bootstrap script.

The property under test is narrow but was a real Blocker: the caller-supplied
--password must never be spliced into SQL *text*. CREATE ROLE sits inside a DO
block, whose body is a string literal and so cannot take a bound parameter, and
the original code filled the CHANGE_ME placeholder with the real password. A
password containing a quote broke the statement; a crafted one injected.
"""

import sys, os
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeCursor:
    """Records every execute() so the test can inspect text vs bound params."""

    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, calls):
        self.calls = calls
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.calls)

    def close(self):
        self.closed = True


def _run(password):
    """Run main() with --password, returning the recorded execute() calls."""
    import create_ro_role

    calls = []
    cfg = mock.Mock(host="h", port=1, dbname="d", user="u", password="p")
    with mock.patch.object(create_ro_role, "load_config", return_value=cfg), \
         mock.patch.object(create_ro_role.psycopg2, "connect", return_value=_FakeConn(calls)), \
         mock.patch.object(sys, "argv", ["create_ro_role.py", "--password", password]):
        rc = create_ro_role.main()
    assert rc == 0
    return calls


HOSTILE = "a'; DROP ROLE portfoliouser; --"


def test_password_never_appears_in_sql_text():
    """The secret reaches Postgres as a bound parameter, never as SQL source."""
    calls = _run(HOSTILE)
    for sql, _params in calls:
        assert HOSTILE not in sql


def test_password_is_bound_on_alter_role():
    """ALTER ROLE carries the real password, parameterised."""
    calls = _run(HOSTILE)
    altered = [(s, p) for s, p in calls if "ALTER ROLE" in s]
    assert len(altered) == 1
    sql, params = altered[0]
    assert params == (HOSTILE,)
    assert "%s" in sql


def test_placeholder_is_filled_with_a_throwaway():
    """CREATE ROLE still gets a syntactically valid literal, just not the secret."""
    calls = _run(HOSTILE)
    create_sql = next(s for s, _ in calls if "CREATE ROLE" in s)
    import create_ro_role

    assert create_ro_role.PLACEHOLDER not in create_sql
    assert HOSTILE not in create_sql


@pytest.mark.parametrize("password", ["single'quote", 'double"quote', "back\\slash"])
def test_quote_bearing_passwords_survive(password):
    """The characters that used to break the statement now pass through intact."""
    calls = _run(password)
    altered = [(s, p) for s, p in calls if "ALTER ROLE" in s]
    assert altered[0][1] == (password,)
    for sql, _params in calls:
        assert password not in sql
