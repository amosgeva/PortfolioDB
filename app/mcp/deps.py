"""Shared dependencies for the MCP server.

Owns the psycopg2 ThreadedConnectionPool that all tools and resources borrow
from.

Usage:
    from app.mcp.deps import get_conn

    with get_conn() as conn:
        rows = fetch_all(conn, "SELECT ...")

Three properties this module is responsible for:

* **Deadlines.** Every connection carries a connect timeout, a statement
  timeout and an idle-in-transaction timeout. The server answers an LLM over
  the network; without them one slow query or one unreachable database host
  held a worker (and, through /healthz, the event loop) indefinitely
  (audit M-1, M-2).
* **Failure isolation.** A failure inside one request — a cancelled query, a
  deadlock, a statement timeout — costs that request its connection at most.
  The pool used to be torn down on any OperationalError, closing connections
  other threads were in the middle of using (audit M-3).
* **Consistent reads.** ``snapshot()`` pins every ``get_conn()`` in the
  current context to one REPEATABLE READ transaction, so a composite read
  (the portfolio review) sees one database state, not one per service
  (audit Q-6).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

# app/ is put on sys.path by this package's __init__ (see app/mcp/__init__.py),
# which runs before any submodule, so the bare sibling import below resolves
# no matter which app.mcp module is imported first.
from db import connect_timeout, load_config, load_target

log = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

RO_USER_ENV = "PORTFOLIODB_MCP_RO_USER"
# The NAME of the environment variable, not a secret — scanners match the word.
RO_PW_ENV = "PORTFOLIODB_MCP_RO_PASSWORD"  # nosec B105  # nosemgrep
# The explicit opt-out. Set it and the server connects with the application's
# read-write credentials, protected only by the session setting below.
ALLOW_RW_FALLBACK_ENV = "PORTFOLIODB_MCP_ALLOW_RW_FALLBACK"

# Server-side deadlines, in milliseconds. The same values are set on the
# read-only role by sql/create_ro_role.sql, so a session that somehow skipped
# the pool's options is still bounded; the pool sets them too so the explicit
# read-write fallback gets them as well.
STATEMENT_TIMEOUT_ENV = "PORTFOLIODB_MCP_STATEMENT_TIMEOUT_MS"
IDLE_TX_TIMEOUT_ENV = "PORTFOLIODB_MCP_IDLE_TX_TIMEOUT_MS"
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_IDLE_TX_TIMEOUT_MS = 60_000

# How many times get_conn() will discard a dead pooled connection and try the
# next one before giving up. A restarted Postgres leaves every idle connection
# dead at once; each is detected by the health check and dropped individually.
_MAX_ACQUIRE_ATTEMPTS = 3


def _env_ms(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not an integer; using %s", name, raw, default)
        value = default
    # 0 means "no limit" to Postgres. A deadline that can be switched off by a
    # typo is not a deadline, so anything below one second is raised to it.
    return max(1000, value)


def statement_timeout_ms() -> int:
    return _env_ms(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_MS)


def idle_tx_timeout_ms() -> int:
    return _env_ms(IDLE_TX_TIMEOUT_ENV, DEFAULT_IDLE_TX_TIMEOUT_MS)


def session_options() -> str:
    """The libpq ``options`` string every pooled connection is opened with."""
    return (
        "-c default_transaction_read_only=on"
        f" -c statement_timeout={statement_timeout_ms()}"
        f" -c idle_in_transaction_session_timeout={idle_tx_timeout_ms()}"
    )


def _credentials() -> tuple[str, str, bool]:
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

    Only the fallback reads the application's configuration: the read-only
    path never touches PORTFOLIODB_PASSWORD, so the MCP container does not
    need to be given it (docker-compose.yml hands the mcp service the
    database's address and its own settings, not the write password).
    """
    ro_user = (os.getenv(RO_USER_ENV) or "").strip()
    if ro_user:
        return ro_user, os.getenv(RO_PW_ENV) or "", True
    if (os.getenv(ALLOW_RW_FALLBACK_ENV) or "").strip().lower() in ("1", "true", "yes"):
        try:
            cfg = load_config()
        except RuntimeError as e:
            raise RuntimeError(
                f"{ALLOW_RW_FALLBACK_ENV} is set, so the MCP server needs the application's "
                "credentials, but PORTFOLIODB_PASSWORD is not in its environment. The mcp "
                "compose service is deliberately not given it; add it in "
                "docker-compose.override.yml (see docs/exposure.md#the-mcp-server), or "
                f"better, create the read-only role with `make ro-role`. ({e})"
            ) from e
        log.warning(
            "MCP server is connecting as %s, the application's READ-WRITE role, because "
            "%s is set. Read-only is then only a session setting. Create the read-only "
            "role with `make ro-role` and put the two lines it prints in .env.",
            cfg.user, ALLOW_RW_FALLBACK_ENV,
        )
        return cfg.user, cfg.password, False
    raise RuntimeError(
        f"The MCP server needs its read-only database role: set {RO_USER_ENV} and "
        f"{RO_PW_ENV} in the repo-root .env. `make ro-role` (or `docker compose "
        "run --rm dashboard python app/create_ro_role.py --generate`) creates the role "
        "and prints both lines. To run on the application's read-write credentials "
        f"anyway, set {ALLOW_RW_FALLBACK_ENV}=1."
    )


def _build_pool() -> ThreadedConnectionPool:
    host, port, dbname = load_target()
    minconn = int(os.getenv("PORTFOLIODB_MCP_POOL_MIN", "1"))
    maxconn = int(os.getenv("PORTFOLIODB_MCP_POOL_MAX", "10"))
    # Two layers, see _credentials: the role decides what the connection MAY
    # do; the session option below makes a stray write fail early even on the
    # fallback credentials.
    user, password, ro_role = _credentials()
    log.info(
        "Creating MCP DB pool: host=%s port=%s db=%s user=%s min=%s max=%s read_only=session%s "
        "connect_timeout=%ss statement_timeout=%sms idle_tx_timeout=%sms",
        host, port, dbname, user, minconn, maxconn,
        "+role" if ro_role else " ONLY (fallback)",
        connect_timeout(), statement_timeout_ms(), idle_tx_timeout_ms(),
    )
    return ThreadedConnectionPool(
        minconn,
        maxconn,
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        connect_timeout=connect_timeout(),
        application_name="portfoliodb-mcp",
        options=session_options(),
    )


def init_pool() -> ThreadedConnectionPool:
    """Initialize the pool eagerly. Safe to call multiple times."""
    return _get_pool()


def close_pool() -> None:
    """Close all pooled connections. Called on server shutdown only.

    Not on a request's failure: the pool is shared by every worker thread, and
    closing it under them is how one cancelled query used to break every other
    in-flight request (audit M-3).
    """
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        try:
            pool.closeall()
        except Exception:
            log.exception("Error closing MCP DB pool")


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    pool = _pool
    if pool is not None:
        return pool
    with _pool_lock:
        if _pool is None:
            _pool = _build_pool()
        return _pool


# The connection pinned by snapshot() for the current context, if any.
_pinned: contextvars.ContextVar[psycopg2.extensions.connection | None] = (
    contextvars.ContextVar("portfoliodb_mcp_pinned_conn", default=None)
)


def _discard(pool: ThreadedConnectionPool, conn) -> None:
    with contextlib.suppress(Exception):
        pool.putconn(conn, close=True)


def _acquire(pool: ThreadedConnectionPool):
    """A pooled connection that has just answered SELECT 1.

    A connection that fails the check is closed and replaced — that is the
    "genuine connection loss" case, and it is handled one connection at a time.
    A failure to *open* a connection (database down, bad password) propagates
    unchanged; there is nothing to tear down.
    """
    last_error: BaseException | None = None
    for _ in range(_MAX_ACQUIRE_ATTEMPTS):
        conn = pool.getconn()
        try:
            if conn.closed:
                raise psycopg2.InterfaceError("connection from pool is closed")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()
            return conn
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            last_error = e
            _discard(pool, conn)
    assert last_error is not None
    raise last_error


def _release(pool: ThreadedConnectionPool, conn) -> None:
    """Return a borrowed connection: rolled back if it is alive, closed if not."""
    if conn.closed:
        _discard(pool, conn)
        return
    try:
        conn.rollback()
    except Exception:
        # The connection failed while being reset: it is the broken one.
        _discard(pool, conn)
        return
    try:
        pool.putconn(conn)
    except Exception:
        log.exception("Error returning conn to pool")
        with contextlib.suppress(Exception):
            conn.close()


@contextmanager
def get_conn() -> Iterator[psycopg2.extensions.connection]:
    """Borrow a healthy connection from the pool; return it on exit.

    Inside ``snapshot()``, yields the pinned connection instead, without
    resetting it — every read in that context shares one transaction.

    An exception raised by the caller's own query costs nothing beyond the
    rollback: the connection goes back to the pool if it is still alive and is
    closed if it is not. The pool itself is never torn down here.
    """
    pinned = _pinned.get()
    if pinned is not None:
        yield pinned
        return

    pool = _get_pool()
    conn = _acquire(pool)
    try:
        yield conn
    finally:
        _release(pool, conn)


@contextmanager
def snapshot() -> Iterator[psycopg2.extensions.connection]:
    """Pin every ``get_conn()`` in this context to one consistent read.

    Opens a REPEATABLE READ, READ ONLY transaction on one pooled connection and
    makes it the answer to ``get_conn()`` until the block exits. The services a
    composite response is assembled from each borrow "a connection"; under this
    they all read the same database state, so a correction, backfill or new
    snapshot that commits half-way through is seen by all of them or none.

    Re-entrant: inside an existing snapshot this yields the same connection.
    The statement timeout applies per statement, so a long review is bounded by
    its slowest query, not by the sum.
    """
    existing = _pinned.get()
    if existing is not None:
        yield existing
        return

    pool = _get_pool()
    conn = _acquire(pool)
    token = None
    try:
        with conn.cursor() as cur:
            # First statement of the transaction psycopg2 opens implicitly.
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        token = _pinned.set(conn)
        yield conn
        # A rollback inside the block would have ended the snapshot silently and
        # let the remaining reads run in a fresh READ COMMITTED transaction.
        # Make that loud instead of letting the response claim a consistency it
        # no longer has.
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_isolation")
            level = (cur.fetchone() or [""])[0]
        if level != "repeatable read":
            raise RuntimeError(
                "snapshot() transaction ended early (a reader rolled back inside it); "
                "the composite read is not consistent"
            )
    finally:
        if token is not None:
            _pinned.reset(token)
        _release(pool, conn)


def in_snapshot() -> bool:
    """True inside ``snapshot()`` — callers that cache reads key on this."""
    return _pinned.get() is not None


def ping_db() -> bool:
    """Lightweight DB liveness probe used by the /healthz endpoint."""
    return ping_db_detail()[0]


def explain_db_error(exc: BaseException) -> str:
    """A one-line diagnosis for a database failure — safe to show anyone.

    psycopg2's text names the database host (a container address, a LAN IP)
    and the role, and it is no business of an LLM client's (audit M-7). This
    maps the failure to a category and an action, and never echoes the driver's
    message. The full exception stays in the server log, where ping_db_detail
    and the tool error handler record it.

    Shared with the dashboard's Data Health page, which needs the same thing:
    an explanation, not a traceback.
    """
    if isinstance(exc, RuntimeError) and not isinstance(exc, psycopg2.Error):
        # Our own configuration errors (no read-only role configured, fallback
        # without a password). Written here, free of secrets, and actionable.
        return (str(exc).strip().splitlines() or [""])[0] or exc.__class__.__name__

    text = str(exc).lower()
    ro_user = (os.getenv(RO_USER_ENV) or "").strip().lower()

    if "password authentication failed" in text:
        if ro_user and ro_user in text:
            # The usual cause: a role that was never created, or a password
            # that changed. No "clear the variables to fall back" advice — that
            # fallback no longer exists without an explicit flag (audit M-11).
            return (
                f"The read-only role configured in {RO_USER_ENV} rejected the password in "
                f"{RO_PW_ENV}. Either the role was never created on this database or its "
                "password changed. Run `make ro-role` (or `python app/create_ro_role.py "
                "--generate`), put the two lines it prints in .env, and restart the MCP server."
            )
        return "The database rejected the configured user's password."
    if "role" in text and "does not exist" in text:
        return "The configured database role does not exist. Run `make ro-role` to create it."
    if "database" in text and "does not exist" in text:
        return "The configured database (PORTFOLIODB_DB) does not exist on the server."
    if "timeout expired" in text:
        return (
            f"Timed out after {connect_timeout()}s connecting to the database "
            "(PORTFOLIODB_HOST / PORTFOLIODB_PORT, deadline PORTFOLIODB_CONNECT_TIMEOUT)."
        )
    if "statement timeout" in text:
        return "A query exceeded the server's statement timeout and was cancelled."
    if "idle-in-transaction" in text or "idle in transaction" in text:
        return "A transaction was left idle past the server's timeout and was closed."
    if "permission denied" in text:
        return (
            "The database role lacks a privilege this read needs. Re-run `make ro-role` "
            "to refresh the read-only role's grants."
        )
    if (
        "could not connect" in text
        or "connection refused" in text
        or "could not translate host name" in text
        or "server closed the connection" in text
        or "no route to host" in text
    ):
        return (
            "Could not reach the database server at the configured address "
            "(PORTFOLIODB_HOST / PORTFOLIODB_PORT)."
        )
    return f"Database error ({exc.__class__.__name__}); details are in the server log."


def ping_db_detail() -> tuple[bool, str | None]:
    """(reachable, reason) — the reason is what get_health reports on failure.

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
