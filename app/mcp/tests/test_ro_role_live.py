"""The read-only role blocks a write on privilege, not on the session setting.

`default_transaction_read_only=on` is what the pool asks for; any statement
can ask for the opposite. This connects through the real pool, switches the
transaction to READ WRITE — which PostgreSQL allows every role to do — and
tries to insert. With the read-only role the insert must fail with
InsufficientPrivilege: the role holds SELECT and nothing else. If it failed
only with ReadOnlySqlTransaction, the guard would be the session, not the
grant, which is the situation this exists to rule out.

Nothing persists: the statement is refused, and get_conn rolls back on exit.
Skips when the pool is not on the read-only role (an operator running the
explicit fallback), because then there is nothing to prove.
"""

from __future__ import annotations

import pytest
from psycopg2 import errors as pg_errors

from app.mcp.deps import get_conn

pytestmark = pytest.mark.slow


@pytest.fixture
def ro_conn():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                user = cur.fetchone()[0]
            if user == "portfoliouser":
                pytest.skip("MCP pool is on the application credentials (fallback); role guard not in use")
            yield conn
    except RuntimeError as e:
        pytest.skip(str(e))
    except Exception as e:  # unreachable database
        pytest.skip(f"DB unreachable: {e}")


def test_write_is_refused_on_privilege_even_in_a_read_write_transaction(ro_conn):
    ro_conn.rollback()  # end the read-only transaction get_conn opened
    with ro_conn.cursor() as cur:
        # First statement of the new transaction: override the session default.
        cur.execute("SET TRANSACTION READ WRITE")
        cur.execute("SHOW transaction_read_only")
        assert cur.fetchone()[0] == "off", "the override itself must have worked"
        with pytest.raises(pg_errors.InsufficientPrivilege):
            cur.execute("INSERT INTO cash_snapshots(account, cash) VALUES ('ZZROTEST', 1)")
    ro_conn.rollback()


def test_the_role_can_read(ro_conn):
    with ro_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM lots")
        assert cur.fetchone()[0] >= 0


def test_rerunning_the_script_narrows_an_existing_install(monkeypatch):
    """An install created by an earlier version holds SELECT on every table,
    chat_log included, and default privileges on future ones. Re-running the
    shipped script (same password) must narrow that to the list — the upgrade
    path for M-10 — and leave the role able to log in as before."""
    import os
    import sys

    import psycopg2

    ro_user = os.getenv("PORTFOLIODB_MCP_RO_USER")
    ro_pw = os.getenv("PORTFOLIODB_MCP_RO_PASSWORD")
    if not (ro_user and ro_pw and os.getenv("PORTFOLIODB_PASSWORD")):
        pytest.skip("needs the application credentials and the RO role's")

    import create_ro_role
    import db

    admin = db.connect(db.load_config())
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            # The pre-1.8 state.
            cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO portfoliodb_ro")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE portfoliouser IN SCHEMA public "
                "GRANT SELECT ON TABLES TO portfoliodb_ro"
            )
        monkeypatch.setattr(sys, "argv", ["create_ro_role.py", "--password", ro_pw])
        assert create_ro_role.main() == 0
        with admin.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege('portfoliodb_ro', 'chat_log', 'SELECT'), "
                "       has_table_privilege('portfoliodb_ro', 'settings', 'SELECT'), "
                "       has_table_privilege('portfoliodb_ro', 'lots', 'SELECT')"
            )
            chat, settings_ok, lots_ok = cur.fetchone()
            cur.execute(
                "SELECT count(*) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace "
                "WHERE n.nspname = 'public' AND array_to_string(d.defaclacl, ',') LIKE '%portfoliodb_ro%'"
            )
            default_grants = cur.fetchone()[0]
    finally:
        admin.close()
    assert chat is False
    assert settings_ok is True and lots_ok is True
    assert default_grants == 0, "future tables must not be readable by default any more"

    host, port, dbname = db.load_target()
    probe = psycopg2.connect(host=host, port=port, dbname=dbname, user=ro_user,
                             password=ro_pw, connect_timeout=5)
    probe.close()


# Every table the MCP surface reads (tools, resources, prompts), and the one
# it must not (audit M-10). Mirrors the list in sql/create_ro_role.sql.
MCP_READS = (
    "instruments", "lots", "price_snapshots", "cash_snapshots", "income",
    "corporate_actions", "snapshot_runs", "settings", "advisor_briefs",
)


@pytest.mark.parametrize("table", MCP_READS)
def test_the_role_reads_every_table_the_mcp_needs(ro_conn, table):
    with ro_conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608 — fixed list above
    ro_conn.rollback()


def test_the_role_cannot_read_the_advisor_chat_log(ro_conn):
    """chat_log holds the operator's Advisor conversations; nothing on the MCP
    surface reads it, so the role must not be able to."""
    with ro_conn.cursor() as cur:
        with pytest.raises(pg_errors.InsufficientPrivilege):
            cur.execute("SELECT 1 FROM chat_log LIMIT 1")
    ro_conn.rollback()


def test_the_role_carries_its_own_deadlines(ro_conn):
    """Set on the role itself, so a session that skipped the pool's options is
    still bounded (audit M-2)."""
    with ro_conn.cursor() as cur:
        cur.execute(
            "SELECT rolconfig FROM pg_roles WHERE rolname = current_user"
        )
        config = cur.fetchone()[0] or []
    joined = ",".join(config)
    assert "statement_timeout=" in joined
    assert "idle_in_transaction_session_timeout=" in joined


def test_the_pool_session_has_the_deadlines(ro_conn):
    with ro_conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()[0] not in ("0", "0ms")
        cur.execute("SHOW idle_in_transaction_session_timeout")
        assert cur.fetchone()[0] not in ("0", "0ms")


def test_a_statement_past_the_deadline_is_cancelled_and_the_pool_survives(ro_conn, monkeypatch):
    """M-2 + M-3 through the real pool: a query that overruns is cancelled by
    the server (QueryCanceled, an OperationalError), the request that ran it
    gets the error, and the shared pool is still there for the next request."""
    from app.mcp import deps

    ro_conn.rollback()
    pool_before = deps._pool
    with pytest.raises(pg_errors.QueryCanceled):
        with deps.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '50ms'")
                cur.execute("SELECT pg_sleep(1)")
    assert deps._pool is pool_before, "a per-request error must not tear the pool down"
    with deps.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
