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
