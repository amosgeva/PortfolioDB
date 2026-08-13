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
in .env, because the dashboard has no auth.
"""

from __future__ import annotations

import os
import threading
import time

import db

# How long one DB read serves all readers. Long enough to amortise the
# query, short enough that a Settings-page save feels instant elsewhere.
TTL_SECONDS = 10.0

_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_at: float = 0.0
_db_ok: bool = False


def _refresh_if_stale(force: bool = False) -> None:
    global _cache, _cache_at, _db_ok
    now = time.monotonic()
    with _lock:
        if not force and _cache_at and now - _cache_at < TTL_SECONDS:
            return
        _cache_at = now
        try:
            cfg = db.load_config()  # raises when no password is available
            conn = db.connect(cfg)
            try:
                rows = db.fetch_all(conn, "SELECT key, value FROM settings")
            finally:
                conn.close()
        except Exception:
            _cache, _db_ok = {}, False
            return
        _cache = {r["key"]: r["value"] for r in rows}
        _db_ok = True


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
