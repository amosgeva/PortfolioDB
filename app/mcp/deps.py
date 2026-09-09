"""Shared dependencies for the MCP server.

Owns the psycopg2 ThreadedConnectionPool that all tools and resources borrow
from. Mirrors the pool pattern in streamlit_app.py so behavior is consistent.

Usage:
    from app.mcp.deps import get_conn

    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT ...")
"""

from __future__ import annotations

import contextlib
import logging
import os
from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

# app/ is put on sys.path by this package's __init__ (see app/mcp/__init__.py),
# which runs before any submodule, so the bare sibling import below resolves
# no matter which app.mcp module is imported first.
from db import load_config

log = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None

RO_USER_ENV = "PORTFOLIODB_MCP_RO_USER"
RO_PASSWORD_ENV = "PORTFOLIODB_MCP_RO_PASSWORD"
# The explicit opt-out. Set it and the server connects with the application's
# read-write credentials, protected only by the session setting below.
ALLOW_RW_FALLBACK_ENV = "PORTFOLIODB_MCP_ALLOW_RW_FALLBACK"


def _credentials(cfg) -> tuple[str, str, bool]:
    """(user, password, is_read_only_role) for the pool.

    The MCP server answers an LLM. Its surface is read-only by design, and
    `default_transaction_read_only=on` below makes it read-only by session —
    but a session setting is a promise the code makes, and any statement can
    unmake it (`SET default_transaction_read_only = off` is allowed to every
    role). The read-only role is what makes it a property of the database:
    it holds SELECT and nothing else, so a bug or a prompt injection that
    reaches a write fails on privilege whatever the session says.

    So the role is the default, not the recommendation. Without it the server
    refuses to connect and says how to create the role (`make ro-role` prints
    the two .env lines). The fallback to the application's credentials is
    still there for a deliberate operator, behind PORTFOLIODB_MCP_ALLOW_RW_FALLBACK,
    and is logged as a warning every time the pool is built.
    """
    ro_user = (os.getenv(RO_USER_ENV) or "").strip()
    if ro_user:
        return ro_user, os.getenv(RO_PASSWORD_ENV) or "", True
    if (os.getenv(ALLOW_RW_FALLBACK_ENV) or "").strip().lower() in ("1", "true", "yes"):
        log.warning(
            "MCP server is connecting as %s, the application's READ-WRITE role, because "
            "%s is set. Read-only is then only a session setting. Create the read-only "
            "role with `make ro-role` and put the two lines it prints in .env.",
            cfg.user, ALLOW_RW_FALLBACK_ENV,
        )
        return cfg.user, cfg.password, False
    raise RuntimeError(
        f"The MCP server needs its read-only database role: set {RO_USER_ENV} and "
        f"{RO_PASSWORD_ENV} in the repo-root .env. `make ro-role` (or `docker compose "
        "run --rm dashboard python app/create_ro_role.py --generate`) creates the role "
        "and prints both lines. To run on the application's read-write credentials "
        f"anyway, set {ALLOW_RW_FALLBACK_ENV}=1."
    )


def _build_pool() -> ThreadedConnectionPool:
    cfg = load_config()
    minconn = int(os.getenv("PORTFOLIODB_MCP_POOL_MIN", "1"))
    maxconn = int(os.getenv("PORTFOLIODB_MCP_POOL_MAX", "10"))
    # Two layers, see _credentials: the role decides what the connection MAY
    # do; the session option below makes a stray write fail early even on the
    # fallback credentials.
    user, password, ro_role = _credentials(cfg)
    log.info(
        "Creating MCP DB pool: host=%s port=%s db=%s user=%s min=%s max=%s read_only=session%s",
        cfg.host, cfg.port, cfg.dbname, user, minconn, maxconn,
        "+role" if ro_role else " ONLY (fallback)",
    )
    return ThreadedConnectionPool(
        minconn,
        maxconn,
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=user,
        password=password,
        options="-c default_transaction_read_only=on",
    )


def init_pool() -> ThreadedConnectionPool:
    """Initialize the pool eagerly. Safe to call multiple times."""
    global _pool
    if _pool is None:
        _pool = _build_pool()
    return _pool


def close_pool() -> None:
    """Close all pooled connections. Called on server shutdown."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            log.exception("Error closing MCP DB pool")
        _pool = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = _build_pool()
    return _pool


@contextmanager
def get_conn() -> Iterator[psycopg2.extensions.connection]:
    """Borrow a healthy connection from the pool; return it on exit.

    Verifies the connection with SELECT 1 before yielding, and recreates the
    pool once if the connection is dead — same recovery pattern used by the
    Streamlit dashboard.
    """
    pool = _get_pool()
    conn = None
    closed_due_to_error = False

    try:
        conn = pool.getconn()
        if conn.closed:
            raise psycopg2.OperationalError("Connection from pool is closed")
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        yield conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        closed_due_to_error = True
        if conn is not None:
            with contextlib.suppress(Exception):
                pool.putconn(conn, close=True)
            conn = None
        # Rebuild pool on next call.
        close_pool()
        raise
    finally:
        if conn is not None and not closed_due_to_error:
            try:
                if not conn.closed:
                    conn.rollback()
                pool.putconn(conn, close=conn.closed != 0)
            except Exception:
                log.exception("Error returning conn to pool")
                with contextlib.suppress(Exception):
                    conn.close()


def ping_db() -> bool:
    """Lightweight DB liveness probe used by the /healthz endpoint."""
    return ping_db_detail()[0]


def explain_db_error(exc: BaseException) -> str:
    """A one-line diagnosis for a connection failure, for humans.

    The same failure used to read two different ways: /healthz explained that a
    read-only role may never have been created, while the dashboard's Data
    Health page printed psycopg2's raw text — container IP included — on the one
    page whose job is telling you whether your numbers are trustworthy. This is
    the single place that turns an exception into an explanation.

    psycopg2 names the role and the host but never the password, so echoing its
    first line is safe.
    """
    first = (str(exc).strip().splitlines() or [""])[0] or exc.__class__.__name__
    ro_user = os.getenv("PORTFOLIODB_MCP_RO_USER")
    if ro_user and "password authentication failed" in first and ro_user in first:
        return (
            f"The read-only role '{ro_user}' rejected the password in "
            "PORTFOLIODB_MCP_RO_PASSWORD. Either the role was never created or its "
            "password changed. Fix it with `make ro-role` (which prints the two "
            ".env lines), or clear both PORTFOLIODB_MCP_RO_* values to fall back to "
            "the application credentials. Then `make restart`."
        )
    return first


def ping_db_detail() -> tuple[bool, str | None]:
    """(reachable, reason) — the reason is what /healthz reports on failure.

    Worth the extra return value: "reachable: false" on its own sends you
    looking for a dead Postgres, when the usual cause is a read-only role that
    was never created. See explain_db_error.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, None
    except Exception as e:
        log.exception("DB ping failed")
        return False, explain_db_error(e)
