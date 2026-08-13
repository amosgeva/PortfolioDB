from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import psycopg2.extras


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str


_ENV_LINE = re.compile(r"^\s*([^#][^=]+?)\s*=\s*(.+?)\s*$")


def _load_env_file_if_needed() -> None:
    """Populate missing PORTFOLIODB_* env vars from the repo-root .env file.

    Mirrors what the PowerShell launchers do, so any entry point (including
    a bare `python app/foo.py`) picks up the DB credentials without forcing
    the caller to set them manually.

    Only fills in env vars that aren't already set — caller overrides win.
    (No early-return on PORTFOLIODB_PASSWORD: other PORTFOLIODB_* keys, e.g.
    the MCP read-only credentials, must load even when the password is set.)
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            m = _ENV_LINE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if key.startswith("PORTFOLIODB_") and not os.getenv(key):
                os.environ[key] = val
    except Exception:
        # If .env is unreadable for any reason, fall through and let the
        # downstream "PORTFOLIODB_PASSWORD is not set" error surface.
        pass


def load_config() -> DbConfig:
    # Lazy .env fallback so manual `python ...` invocations work like the launchers do.
    _load_env_file_if_needed()

    # Prefer env vars; fall back to defaults matching docker-compose.yml
    host = os.getenv("PORTFOLIODB_HOST", "127.0.0.1")
    port = int(os.getenv("PORTFOLIODB_PORT", "54320"))
    dbname = os.getenv("PORTFOLIODB_DB", "portfoliodb")
    user = os.getenv("PORTFOLIODB_USER", "portfoliouser")
    password = os.getenv("PORTFOLIODB_PASSWORD", "")

    if not password:
        raise RuntimeError(
            "PORTFOLIODB_PASSWORD is not set. Set it in the environment or in the repo-root .env file."
        )

    return DbConfig(host=host, port=port, dbname=dbname, user=user, password=password)


def connect(cfg: DbConfig):
    return psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        password=cfg.password,
    )


@contextmanager
def transaction(cfg: DbConfig | None = None):
    """One connection, one transaction: commit on success, roll back on any
    error, and always CLOSE the connection.

    Note `with connect(cfg) as conn:` does neither of the latter two —
    psycopg2's connection context manager only commits/rolls back and leaves
    the connection open. Pair with run() so a multi-statement CLI command
    (e.g. instrument upsert + lot insert) is atomic.
    """
    conn = connect(cfg or load_config())
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(conn, sql: str, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


def run(conn, sql: str, params=None):
    """Execute WITHOUT committing — for use inside transaction()."""
    with conn.cursor() as cur:
        cur.execute(sql, params or ())


def execute(conn, sql: str, params=None):
    """Execute and commit immediately (one statement = one transaction).

    Prefer transaction() + run() for anything multi-statement.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
    conn.commit()
