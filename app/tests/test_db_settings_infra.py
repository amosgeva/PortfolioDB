"""db.py and settings.py infrastructure: .env quoting, connection deadlines,
a lazy settings reader that a read-only process can use, and a refresh that
does not hold its lock across a blocking connect.

* A-7  ``PORTFOLIODB_PASSWORD="x"`` reads as ``x``, the way compose reads it.
* A-6  every connection made through db.py has a connect timeout.
* A-6  a settings refresh blocked on the database stalls nobody else.
* R-5  importing settings / a module-level reporting zone opens no connection.
* X-4 / Q-5  settings can be read through an injected connection source.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import settings  # noqa: E402


# ────────────────────────── A-7: .env values ──────────────────────────


@pytest.mark.parametrize("line, expected", [
    ('PORTFOLIODB_PASSWORD="x"', ("PORTFOLIODB_PASSWORD", "x")),
    ("PORTFOLIODB_PASSWORD='x'", ("PORTFOLIODB_PASSWORD", "x")),
    ('PORTFOLIODB_PASSWORD="a b#c"', ("PORTFOLIODB_PASSWORD", "a b#c")),
    ('PORTFOLIODB_PASSWORD="say \\"hi\\""', ("PORTFOLIODB_PASSWORD", 'say "hi"')),
    ("PORTFOLIODB_PASSWORD='lit\\eral'", ("PORTFOLIODB_PASSWORD", "lit\\eral")),
    ('PORTFOLIODB_PASSWORD="x" # trailing comment', ("PORTFOLIODB_PASSWORD", "x")),
    ("PORTFOLIODB_PASSWORD=plain # comment", ("PORTFOLIODB_PASSWORD", "plain")),
    ("PORTFOLIODB_PASSWORD=has#hash", ("PORTFOLIODB_PASSWORD", "has#hash")),
    ("export PORTFOLIODB_TZ=UTC", ("PORTFOLIODB_TZ", "UTC")),
    ('PORTFOLIODB_PASSWORD="unterminated', ("PORTFOLIODB_PASSWORD", '"unterminated')),
])
def test_a7_env_values_are_read_as_compose_reads_them(line, expected):
    assert db.parse_env_line(line) == expected


@pytest.mark.parametrize("line", ['PORTFOLIODB_PASSWORD=""', "PORTFOLIODB_PASSWORD=''", "# x=1", "", "NOEQUALS"])
def test_a7_empty_and_non_assignments_are_absent(line):
    assert db.parse_env_line(line) is None


def test_a7_quoted_password_reaches_load_config(tmp_path, monkeypatch):
    """Through the real loader, not just the parser."""
    env = tmp_path / ".env"
    env.write_text('PORTFOLIODB_PASSWORD="s3cr3t"\n', encoding="utf-8")
    monkeypatch.delenv("PORTFOLIODB_PASSWORD", raising=False)
    monkeypatch.setattr(db, "Path", lambda *_a, **_k: _FakePath(env))
    assert db.load_config().password == "s3cr3t"


class _FakePath:
    """Makes db._load_env_file_if_needed find the temp .env."""

    def __init__(self, env):
        self._env = env

    def resolve(self):
        return self

    @property
    def parent(self):
        return self

    def __truediv__(self, _name):
        return self._env


# ────────────────────────── A-6: connection deadlines ──────────────────────────


def test_a6_connect_passes_a_timeout(monkeypatch):
    seen = {}
    monkeypatch.delenv(db.CONNECT_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: seen.update(kw) or object())
    db.connect(db.DbConfig("h", 1, "d", "u", "p"))
    assert seen["connect_timeout"] == db.DEFAULT_CONNECT_TIMEOUT_SECONDS


@pytest.mark.parametrize("raw, expected", [("5", 5), ("0", 2), ("-1", 2), ("abc", 10), ("", 10)])
def test_a6_timeout_cannot_be_switched_off(monkeypatch, raw, expected):
    monkeypatch.setenv(db.CONNECT_TIMEOUT_ENV, raw)
    assert db.connect_timeout() == expected


def test_a6_pool_parameters_carry_the_timeout():
    params = db.connect_params(db.DbConfig("h", 1, "d", "u", "p"))
    assert params["connect_timeout"] >= 2
    assert {"host", "port", "dbname", "user", "password"} <= set(params)


def test_a6_dashboard_pool_is_built_from_db_connect_params():
    """The Streamlit pool used to pass host/port/... itself and no timeout."""
    src = (Path(__file__).resolve().parent.parent / "streamlit_app.py").read_text(encoding="utf-8")
    pool_block = src[src.index("def get_pool"):src.index("def get_conn")]
    assert "connect_params(cfg)" in pool_block


# ────────────────────────── settings: lazy, injectable, unblocked ──────────────────────────


@pytest.fixture
def live_settings(monkeypatch):
    """Undo the suite-wide freeze (conftest) for one test, and restore it."""
    monkeypatch.setattr(settings, "TTL_SECONDS", 10.0)
    monkeypatch.setattr(settings, "_cache", {})
    monkeypatch.setattr(settings, "_cache_at", 0.0)
    monkeypatch.setattr(settings, "_db_ok", False)
    monkeypatch.setattr(settings, "_refreshing", False)
    monkeypatch.setattr(settings, "_first_load", threading.Event())
    yield
    settings.use_connection_source(None)


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, *a, **kw):
        conn = self

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def execute(self, q, p=None):
                assert "FROM settings" in q

            def fetchall(self):
                return [{"key": k, "value": v} for k, v in conn.rows.items()]

        return C()


def test_x4_q5_settings_read_through_an_injected_source(live_settings, monkeypatch):
    """A process without the application password registers its own source —
    the MCP passes its read-only pool — and reads the table through it."""
    monkeypatch.delenv("PORTFOLIODB_PASSWORD", raising=False)
    monkeypatch.setattr(db, "load_config", lambda: pytest.fail("must not need the write password"))

    @contextlib.contextmanager
    def ro_source():
        yield _Conn({"reporting_tz": "Asia/Tokyo", "philosophy_md": "one-pager"})

    settings.use_connection_source(ro_source)
    assert settings.get("reporting_tz", env="PORTFOLIODB_TZ", default="UTC") == "Asia/Tokyo"
    assert settings.get("philosophy_md") == "one-pager"
    assert settings.db_available() is True


def test_a6_refresh_does_not_hold_the_lock_across_a_blocking_read(live_settings):
    """One thread is stuck reading the database; another reader with a cached
    value answers immediately instead of queueing behind it."""
    gate = threading.Event()
    entered = threading.Event()

    @contextlib.contextmanager
    def slow_source():
        entered.set()
        gate.wait(5)
        yield _Conn({"display_name": "new"})

    # A previously cached value, now stale.
    settings._cache = {"display_name": "old"}
    settings._cache_at = time.monotonic() - 60
    settings._first_load.set()
    settings.use_connection_source(slow_source)

    t = threading.Thread(target=lambda: settings.get("display_name"))
    t.start()
    assert entered.wait(2)
    t0 = time.monotonic()
    value = settings.get("display_name")
    elapsed = time.monotonic() - t0
    gate.set()
    t.join(5)
    assert value == "old"
    assert elapsed < 0.5, "a reader waited on another thread's connect"
    assert settings.get("display_name") == "new"


def test_a6_first_read_wait_is_bounded(live_settings, monkeypatch):
    """Before anything was ever read, concurrent readers wait for the first
    read — but no longer than one connect timeout, then fall back."""
    gate = threading.Event()

    @contextlib.contextmanager
    def hung():
        gate.wait(10)
        yield _Conn({})

    monkeypatch.setattr(settings, "_first_load_wait_seconds", lambda: 0.3)
    settings.use_connection_source(hung)
    t = threading.Thread(target=lambda: settings.get("x"))
    t.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    assert settings.get("x", default="fallback") == "fallback"
    assert time.monotonic() - t0 < 2
    gate.set()
    t.join(5)


def test_a6_a_forced_read_after_a_save_wins_over_an_older_slow_read(live_settings):
    """Reads are ordered: a slow refresh that started before a save cannot
    overwrite the forced refresh the save triggered."""
    first_gate = threading.Event()
    calls = []

    @contextlib.contextmanager
    def source():
        n = len(calls)
        calls.append(n)
        if n == 0:
            first_gate.wait(5)
            yield _Conn({"k": "stale"})
        else:
            yield _Conn({"k": "fresh"})

    settings.use_connection_source(source)
    t = threading.Thread(target=lambda: settings.get("k"))
    t.start()
    time.sleep(0.05)
    settings._refresh_if_stale(force=True)  # what set_value does after writing
    first_gate.set()
    t.join(5)
    assert settings._cache == {"k": "fresh"}


def test_r5_importing_settings_and_reporting_tz_connects_to_nothing():
    import subprocess

    code = (
        "import sys; sys.path.insert(0, 'app');"
        "import psycopg2; calls = [];"
        "psycopg2.connect = lambda *a, **k: calls.append(1);"
        "import settings, reporting_tz, holdings, corporate_actions;"
        "print(len(calls))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parents[2], timeout=60,
                         env={**__import__("os").environ, "PORTFOLIODB_PASSWORD": "x"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0"


# ────────────────────────── reporting_tz.lazy_tzinfo ──────────────────────────


def test_r5_lazy_zone_behaves_as_the_real_zone(monkeypatch):
    import reporting_tz

    zone = reporting_tz._ReportingZone()
    monkeypatch.setattr(reporting_tz, "tzinfo", lambda: __import__("zoneinfo").ZoneInfo("America/New_York"))
    monkeypatch.setattr(settings, "db_available", lambda: False)
    utc = datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)  # 03:30 EDT, just after the jump
    local = utc.astimezone(zone)
    assert (local.hour, local.minute) == (3, 30)
    assert local.utcoffset().total_seconds() == -4 * 3600
    combined = datetime.combine(date(2026, 1, 15), datetime.min.time(), tzinfo=zone)
    assert combined.astimezone(timezone.utc).hour == 5
    assert zone.key == "America/New_York"


def test_r5_lazy_zone_pins_only_a_value_read_from_the_table(monkeypatch):
    import zoneinfo

    import reporting_tz

    zone = reporting_tz._ReportingZone()
    answers = iter(["UTC", "Asia/Tokyo", "Europe/Paris"])
    monkeypatch.setattr(reporting_tz, "tzinfo", lambda: zoneinfo.ZoneInfo(next(answers)))
    available = iter([False, True, True])
    monkeypatch.setattr(settings, "db_available", lambda: next(available))
    assert zone.resolve().key == "UTC"          # env fallback: not pinned
    assert zone.resolve().key == "Asia/Tokyo"   # from the table: pinned
    assert zone.resolve().key == "Asia/Tokyo"   # and kept
