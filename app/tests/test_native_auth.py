"""The dashboard write gate (audit A-1), without a browser or a database.

The AppTest module drives the same rules through the real pages; this pins the
rules themselves: unset means locked, the compare is on bytes, failures are
throttled per client and process-wide, the session holds no password, and a
changed or idle password relocks.
"""

from __future__ import annotations

import pytest

import native_auth as auth

PW = "correct horse ✓ battery"  # non-ASCII on purpose: compare_digest on str raises for it


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    auth.reset_throttle()
    monkeypatch.delenv(auth.DASHBOARD_PW_ENV, raising=False)
    yield
    auth.reset_throttle()


def _set(monkeypatch, value=PW):
    monkeypatch.setenv(auth.DASHBOARD_PW_ENV, value)


def test_unset_password_means_locked_not_open():
    session = {}
    assert not auth.configured()
    assert not auth.is_unlocked(session)
    res = auth.attempt("", session)
    assert not res.ok and auth.DASHBOARD_PW_ENV in res.message
    with pytest.raises(auth.WriteLocked, match="not set"):
        auth.require_unlocked(session)


def test_blank_password_counts_as_unset(monkeypatch):
    _set(monkeypatch, "   ")
    assert not auth.configured()
    assert not auth.attempt("   ", {}).ok


def test_correct_non_ascii_password_unlocks_the_session(monkeypatch):
    _set(monkeypatch)
    session = {}
    res = auth.attempt(PW, session)
    assert res.ok
    assert auth.is_unlocked(session)
    auth.require_unlocked(session)  # does not raise
    # Nothing in the session is the password, or contains it.
    assert all(PW not in str(v) for v in session.values())


def test_non_ascii_wrong_password_is_a_refusal_not_a_crash(monkeypatch):
    _set(monkeypatch, "plain-ascii")
    res = auth.attempt("ünïcode", {})
    assert not res.ok and res.message == "Wrong password."


def test_messages_never_echo_the_candidate(monkeypatch):
    _set(monkeypatch)
    secret_guess = "my-guess-9f3a"
    for _ in range(auth.FREE_ATTEMPTS + 2):
        res = auth.attempt(secret_guess, {}, client="c1", now=0.0)
        assert secret_guess not in res.message


def test_failures_lock_the_client_out_and_double(monkeypatch):
    _set(monkeypatch)
    session = {}
    for i in range(auth.FREE_ATTEMPTS - 1):
        assert auth.attempt("nope", session, client="a", now=0.0).message == "Wrong password."
    res = auth.attempt("nope", session, client="a", now=0.0)
    assert "wait" in res.message
    assert auth.retry_after("a", now=0.0) == pytest.approx(auth.LOCKOUT_BASE_S)
    # Locked out: even the right password is not compared, let alone accepted.
    res = auth.attempt(PW, session, client="a", now=1.0)
    assert not res.ok and "Too many failed attempts" in res.message
    assert not auth.is_unlocked(session, now=1.0)
    # After the wait, one more failure doubles the lockout.
    later = auth.LOCKOUT_BASE_S + 1
    auth.attempt("nope", session, client="a", now=later)
    assert auth.retry_after("a", now=later) == pytest.approx(2 * auth.LOCKOUT_BASE_S)
    # Another client is unaffected (until the global ceiling).
    assert auth.attempt(PW, {}, client="b", now=later).ok


def test_lockout_is_capped(monkeypatch):
    _set(monkeypatch)
    now = 0.0
    for _ in range(auth.FREE_ATTEMPTS + 20):
        now += auth.LOCKOUT_MAX_S + 1  # always past the previous lockout
        auth.attempt("nope", {}, client="a", now=now)
    assert auth.retry_after("a", now=now) <= auth.LOCKOUT_MAX_S


def test_global_ceiling_stops_a_client_rotating_addresses(monkeypatch):
    _set(monkeypatch)
    for i in range(auth.GLOBAL_FREE_ATTEMPTS):
        auth.attempt("nope", {}, client=f"10.0.0.{i}", now=0.0)
    assert auth.retry_after("10.9.9.9", now=0.0) > 0
    assert not auth.attempt(PW, {}, client="10.9.9.9", now=0.0).ok


def test_success_clears_the_clients_failures(monkeypatch):
    _set(monkeypatch)
    for _ in range(auth.FREE_ATTEMPTS - 1):
        auth.attempt("nope", {}, client="a", now=0.0)
    assert auth.attempt(PW, {}, client="a", now=0.0).ok
    for _ in range(auth.FREE_ATTEMPTS - 1):
        auth.attempt("nope", {}, client="a", now=0.0)
    assert auth.retry_after("a", now=0.0) == 0


def test_lock_relocks(monkeypatch):
    _set(monkeypatch)
    session = {}
    assert auth.attempt(PW, session).ok
    auth.lock(session)
    assert not auth.is_unlocked(session)
    with pytest.raises(auth.WriteLocked, match="dashboard password"):
        auth.require_unlocked(session)


def test_a_changed_password_relocks_existing_sessions(monkeypatch):
    _set(monkeypatch)
    session = {}
    assert auth.attempt(PW, session, now=0.0).ok
    _set(monkeypatch, "rotated-password")
    assert not auth.is_unlocked(session, now=1.0)


def test_idle_session_relocks_and_activity_keeps_it_open(monkeypatch):
    _set(monkeypatch)
    session = {}
    assert auth.attempt(PW, session, now=0.0).ok
    step = auth.IDLE_TIMEOUT_S - 1
    assert auth.is_unlocked(session, now=step)          # refreshes the timer
    assert auth.is_unlocked(session, now=2 * step)      # still within the window of the last use
    assert not auth.is_unlocked(session, now=2 * step + auth.IDLE_TIMEOUT_S + 1)


def test_a_forged_session_value_does_not_unlock(monkeypatch):
    _set(monkeypatch)
    session = {"_pdb_write_unlock": PW, "_pdb_write_seen": 0.0}
    assert not auth.is_unlocked(session, now=0.0)
