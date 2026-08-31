"""Exit-code contract for the weekly Financial Datasets enrichment run.

main() used to return 0 unconditionally, so the scheduled job reported success
through a revoked API key, a dead network and an unreachable database alike.
The contract now is: non-zero only when the run could not do what it was asked
to do, so that transient single-endpoint flakiness does not train anyone to
ignore a red run.
"""

import sys, os
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fd_weekly_enrichment as fd


@pytest.fixture
def run_main(monkeypatch):
    """Drive main() with the network, cache, clock and DB all stubbed out."""

    def _run(*, responses, argv=None, db_ok=True, symbols=("NVDA",), api_key="test-key",
             read_cache=None):
        calls = {"n": 0}

        def fake_request_json(_key, section, symbol):
            calls["n"] += 1
            return responses(section, symbol)

        monkeypatch.setattr(fd, "request_json", fake_request_json)
        monkeypatch.setattr(fd, "_resolve_api_key", lambda: api_key)
        monkeypatch.setattr(fd, "active_symbols_from_db", lambda: list(symbols))
        monkeypatch.setattr(fd, "read_cache", read_cache or (lambda _s, _sec: (None, False)))
        monkeypatch.setattr(fd, "write_cache", lambda *_a, **_k: None)
        monkeypatch.setattr(fd, "top_movers_from_db", lambda _n: [])
        monkeypatch.setattr(fd.time, "sleep", lambda _s: None)

        if db_ok:
            monkeypatch.setattr(fd, "connect", lambda _cfg: mock.Mock())
            monkeypatch.setattr(fd.fd_store, "persist_section", lambda *_a, **_k: 1)
        else:
            def _boom(_cfg):
                raise RuntimeError("connection refused")
            monkeypatch.setattr(fd, "connect", _boom)
        monkeypatch.setattr(fd, "load_config", lambda: mock.Mock())

        monkeypatch.setattr(sys, "argv", ["fd_weekly_enrichment.py", *(argv or [])])
        return fd.main(), calls["n"]

    return _run


def _ok(_section, _symbol):
    # format_report() reaches into .get("data", {}).get(...), so the payload has
    # to be dict-shaped all the way down, as the real API responses are.
    return {"data": {}}


def _fail(_section, _symbol):
    return {"_error": "HTTP 401", "body": "unauthorized"}


def test_clean_run_returns_zero(run_main):
    rc, n = run_main(responses=_ok)
    assert rc == 0
    assert n > 0, "fixture should have driven at least one fetch"


def test_total_api_failure_returns_one(run_main):
    """A revoked key or a dead vendor must not look like a successful run."""
    rc, n = run_main(responses=_fail)
    assert n > 0
    assert rc == 1


def test_unreachable_database_returns_one(run_main):
    """Persistence was requested; nothing was written; fd_* tables go stale."""
    rc, _n = run_main(responses=_ok, db_ok=False)
    assert rc == 1


def test_no_persist_tolerates_a_dead_database(run_main):
    """--no-persist did not ask for the DB, so its absence is not a failure."""
    rc, _n = run_main(responses=_ok, db_ok=False, argv=["--no-persist"])
    assert rc == 0


def test_partial_failure_still_returns_zero(run_main):
    """One flaky endpoint out of several is reported, not escalated to red."""
    seen = {"n": 0}

    def flaky(section, symbol):
        seen["n"] += 1
        return _fail(section, symbol) if seen["n"] == 1 else _ok(section, symbol)

    rc, n = run_main(responses=flaky)
    assert n > 1, "need multiple sections for this to be a partial failure"
    assert rc == 0


def test_outage_behind_a_warm_cache_still_returns_one(run_main):
    """Cache hits must not disguise a vendor outage.

    `attempted` counts calls actually made to the vendor, not sections served.
    If it counted cache hits, a half-warm cache during a total outage would
    leave failed < attempted and the run would exit 0.
    """
    seen = {"n": 0}

    def half_warm(_sym, _section):
        seen["n"] += 1
        if seen["n"] % 2 == 0:
            return {"data": {}, "fetched_at": "2026-08-30T00:00:00Z"}, True
        return None, False

    rc, n = run_main(responses=_fail, read_cache=half_warm)
    assert n > 0, "some sections should still have reached the vendor"
    assert seen["n"] > n, "and some should have been served from cache"
    assert rc == 1


def test_dry_run_returns_zero_without_calling_the_api(run_main):
    rc, n = run_main(responses=_fail, argv=["--dry-run-cost"])
    assert rc == 0
    assert n == 0


def test_missing_api_key_still_returns_zero(run_main):
    """A deliberate skip on an install that never configured the key."""
    rc, n = run_main(responses=_ok, api_key=None)
    assert rc == 0
    assert n == 0
