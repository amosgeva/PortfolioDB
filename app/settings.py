"""DB-backed runtime settings with env fallback (public-repo plan §7).

One `settings` table row per setting. Resolution order for every read:

    DB value  →  env var(s)  →  default

so the Settings page is the primary interface, while env vars keep working
as bootstrap/override and a fresh clone without the migration degrades
cleanly to env-only behaviour.

Fail-soft by design: any problem reaching the database (no password, DB
down, table missing) silently yields the env/default path, and the failure
is cached for the TTL so pure modules importing this transitively don't
retry a dead connection on every call. Secrets never live here — they stay
in .env, because the dashboard has no accounts, only one shared password in
front of its write pages.

Lazy: importing this module touches nothing. The first ``get()`` reads the
table, through a connection source that defaults to the application's
credentials. A process that does not hold those — the MCP server, on its
read-only role — registers its own with ``use_connection_source()``, so the
settings it reads (reporting timezone, the saved one-pager) are the table's,
not the environment's fallback (audit X-4, Q-5). Reading needs SELECT on
``settings`` and nothing else; writes keep going through the application's
credentials, because only the dashboard writes.

The refresh does not hold the module lock across the connect: one thread
reads while the others keep serving the previous values (or, before the very
first read has finished, wait for it a bounded time). A database that takes
the whole connect timeout to fail used to stall every settings reader in the
process for that long, one after another (audit A-6).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Callable, ContextManager

import db

# How long one DB read serves all readers. Long enough to amortise the
# query, short enough that a Settings-page save feels instant elsewhere.
TTL_SECONDS = 10.0

_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_at: float = 0.0
_db_ok: bool = False

# Single-flight bookkeeping. A generation number orders the reads, so a slow
# read that started before a save can never overwrite the forced read the save
# triggered afterwards.
_refreshing: bool = False
_generation: int = 0
_stored_generation: int = 0
_first_load = threading.Event()

ConnectionSource = Callable[[], ContextManager]


@contextlib.contextmanager
def _application_connection():
    """The default source: one short-lived connection on the app's credentials."""
    conn = db.connect(db.load_config())  # raises when no password is available
    try:
        yield conn
    finally:
        conn.close()


_connection_source: ConnectionSource = _application_connection


def use_connection_source(source: ConnectionSource | None) -> None:
    """Read settings through ``source`` — a zero-argument callable returning a
    context manager that yields an open connection — instead of the
    application's credentials. ``None`` restores the default.

    Register it before the first read (the MCP does so in its package
    ``__init__``). Values already cached are kept until the TTL expires, so a
    frozen cache — a test's — stays frozen.
    """
    global _connection_source
    with _lock:
        _connection_source = source or _application_connection


def _read_rows() -> dict[str, str]:
    with _connection_source() as conn:
        rows = db.fetch_all(conn, "SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}


def _first_load_wait_seconds() -> float:
    # The first read can take at most one connect timeout plus a query; past
    # that, the caller gets the env/default answer rather than hanging.
    return float(db.connect_timeout()) + 2.0


def _refresh_if_stale(force: bool = False) -> None:
    global _cache, _cache_at, _db_ok, _refreshing, _generation, _stored_generation
    now = time.monotonic()
    with _lock:
        # _cache_at is stamped when a read FINISHES, never when one is claimed:
        # stamping it at the claim made a concurrent cold reader see a "fresh"
        # empty cache and answer with defaults while the first read was still
        # in flight.
        fresh = bool(_cache_at) and now - _cache_at < TTL_SECONDS
        if fresh and not force:
            return
        if _refreshing and not force:
            # Someone else is reading. Serve what we have — unless nothing has
            # ever been read, in which case wait (bounded) for that first read.
            wait_for_first = not _first_load.is_set()
            gen = None
        else:
            _refreshing = True
            _generation += 1
            gen = _generation
            wait_for_first = False
    if gen is None:
        if wait_for_first:
            _first_load.wait(_first_load_wait_seconds())
        return

    values: dict[str, str] = {}
    ok = False
    try:
        values, ok = _read_rows(), True
    except Exception:
        values, ok = {}, False
    finally:
        with _lock:
            if gen > _stored_generation:
                _cache, _db_ok, _stored_generation = values, ok, gen
                # A failed read is cached for the TTL too, so a dead database
                # is not retried on every call.
                _cache_at = time.monotonic()
            if gen == _generation:
                _refreshing = False
        _first_load.set()


def refresh() -> None:
    """Re-read the settings table now, ignoring the TTL.

    For a caller that knows the table just changed (the Settings page after a
    save it did not make through set_value/unset). Ordered with any read in
    flight, so the fresh values win.
    """
    _refresh_if_stale(force=True)


def get(key: str, *, env: str | tuple[str, ...] | None = None, default: str | None = None) -> str | None:
    """Resolve one setting: DB → env var(s), first non-blank wins → default."""
    _refresh_if_stale()
    val = _cache.get(key)
    if val is not None and val.strip():
        return val.strip()
    for name in ((env,) if isinstance(env, str) else (env or ())):
        raw = os.getenv(name, "")
        if raw.strip():
            return raw.strip()
    return default


def fallback(
    key: str, *, env: str | tuple[str, ...] | None = None, default: str | None = None
) -> str | None:
    """What this setting would resolve to with **no** database row.

    The Settings page needs this to avoid a trap: its fields are pre-filled with
    the *resolved* value, so saving the form would otherwise write a DB override
    for every field — including ones the operator never touched. Those overrides
    then silently outrank `.env`, and editing `.env` appears to do nothing. With
    this, the page can skip (or clear) any value that already matches the
    env/default answer.
    """
    for name in ((env,) if isinstance(env, str) else (env or ())):
        raw = os.getenv(name, "")
        if raw.strip():
            return raw.strip()
    return default


def source_of(key: str, *, env: str | tuple[str, ...] | None = None) -> str:
    """Where get() would take this value from: 'db' | 'env' | 'default'.

    Lets the Settings UI show why a field has its value without duplicating
    the resolution logic.
    """
    _refresh_if_stale()
    val = _cache.get(key)
    if val is not None and val.strip():
        return "db"
    for name in ((env,) if isinstance(env, str) else (env or ())):
        if os.getenv(name, "").strip():
            return "env"
    return "default"


def set_value(key: str, value: str) -> None:
    """Upsert one setting and refresh the cache immediately."""
    cfg = db.load_config()
    conn = db.connect(cfg)
    try:
        db.execute(
            conn,
            """
            INSERT INTO settings(key, value, updated_at) VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, value),
        )
    finally:
        conn.close()
    _refresh_if_stale(force=True)


def unset(key: str) -> None:
    """Delete one setting so get() falls back to env/default again."""
    cfg = db.load_config()
    conn = db.connect(cfg)
    try:
        db.execute(conn, "DELETE FROM settings WHERE key = %s", (key,))
    finally:
        conn.close()
    _refresh_if_stale(force=True)


def db_available() -> bool:
    """Whether the last resolution actually read the settings table."""
    _refresh_if_stale()
    return _db_ok
