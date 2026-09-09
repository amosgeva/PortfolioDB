"""Health and freshness probes.

Two payloads, deliberately different:

* ``liveness()`` — what the unauthenticated ``/healthz`` route serves. Up or
  down, and how old the last snapshot is. Nothing that names a symbol, a host,
  a role or a table.
* ``server_health()`` — the full diagnostic the ``get_health`` tool returns,
  behind the bearer token: the last run's counts and error text, per-table
  freshness, and the database's own failure reason.

They used to be one payload, served without a token. ``snapshot_runs.error``
is written verbatim by the collector and names the symbols that failed, with a
traceback when yfinance threw one; the FD freshness block enumerates tables
and row counts; a database failure reason names the role and the container
IP. Anyone who could open a TCP connection to the port learned part of the
portfolio universe (audit F11, 2026-09-09). The route now serves the small
payload, and everything a human needs to debug is one authenticated call away.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from psycopg2 import sql

from app.mcp.services import common

from app.mcp.deps import get_conn, ping_db_detail

log = logging.getLogger(__name__)

LIVENESS_KEYS = ("ok", "db", "last_snapshot_age_s")


def liveness() -> dict[str, Any]:
    """The ``/healthz`` payload: exactly the keys in LIVENESS_KEYS, nothing else.

    One ``SELECT 1`` and one row from ``snapshot_runs`` — the cheap probe the
    route always promised. The ping's failure reason stays server-side: it can
    name the read-only role and the database host, and the caller has not
    shown a token. ``get_health`` returns it.
    """
    reachable, _reason = ping_db_detail()
    if not reachable:
        return {"ok": False, "db": "down", "last_snapshot_age_s": None}

    age: int | None = None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ts_end FROM snapshot_runs "
                    "WHERE ts_end IS NOT NULL ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
        if row and row[0] is not None:
            ts = row[0]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    except Exception:
        # A missing table on a fresh install is not a reason to report the
        # server down; log the detail, serve the age as unknown.
        log.exception("liveness: could not read snapshot_runs")
    return {"ok": True, "db": "up", "last_snapshot_age_s": age}


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
