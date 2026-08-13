"""Health and freshness probes — reused by the get_health tool and /healthz."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg2 import sql

from app.mcp.services import common

from app.mcp.deps import get_conn, ping_db_detail


def db_status() -> dict[str, Any]:
    ok, reason = ping_db_detail()
    if ok:
        return {"reachable": True}
    # Say *why*: the common cause is a read-only role that does not exist yet,
    # which looks identical to a dead database without this.
    return {"reachable": False, "error": reason}


def last_snapshot_run() -> dict[str, Any] | None:
    """Most recent row from snapshot_runs, or None if empty."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ts_start, ts_end, status,
                       symbols_total, symbols_ok, symbols_failed, error
                FROM snapshot_runs
                ORDER BY id DESC
                LIMIT 1
                """,
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def fd_freshness() -> dict[str, Any]:
    """Latest fetched_at per FD table, plus a coverage row count."""
    tables = list(common.FD_TABLES.values())
    out: dict[str, Any] = {}
    with get_conn() as conn:
        for tbl in tables:
            try:
                with conn.cursor() as cur:
                    # tbl comes from the hardcoded `tables` whitelist above;
                    # compose it as a quoted identifier so the query is never
                    # built from string formatting.
                    cur.execute(
                        sql.SQL("SELECT MAX(fetched_at), COUNT(*) FROM ")
                        + sql.Identifier(tbl)
                    )
                    row = cur.fetchone()
                    out[tbl] = {
                        "latest_fetched_at": row[0].isoformat() if row[0] else None,
                        "row_count": int(row[1] or 0),
                    }
            except Exception as e:
                # Table may not exist yet (FD enrichment not run). That's
                # informational, not an error — report and move on.
                conn.rollback()
                out[tbl] = {"error": str(e)}
    return out


def server_health() -> dict[str, Any]:
    """Full health payload returned by the get_health tool."""
    db = db_status()
    snap = last_snapshot_run() if db["reachable"] else None
    fd = fd_freshness() if db["reachable"] else {}

    snap_payload: dict[str, Any] | None = None
    if snap is not None:
        snap_payload = {
            "id": int(snap["id"]),
            "ts_start": snap["ts_start"].isoformat() if snap.get("ts_start") else None,
            "ts_end": snap["ts_end"].isoformat() if snap.get("ts_end") else None,
            "status": snap["status"],
            "symbols_total": snap.get("symbols_total"),
            "symbols_ok": snap.get("symbols_ok"),
            "symbols_failed": snap.get("symbols_failed"),
            "error": snap.get("error"),
        }

    return {
        "ok": db["reachable"],
        "now": datetime.now(timezone.utc).isoformat(),
        "db": db,
        "last_snapshot_run": snap_payload,
        "fd_freshness": fd,
    }
