"""Create the read-only database role the MCP server should connect as.

Runs against the sql/ tree baked into the image, so an install that pulled the
published image and never cloned the repository can still do this:

    docker compose run --rm dashboard python app/create_ro_role.py --generate

Why bother: the MCP surface is read-only by design, and this makes it read-only
by *privilege* — a bug or a prompt injection cannot write through a role that
holds no write grants.

Prints the two .env lines to add. Idempotent: re-running updates the password of
an existing role rather than failing.
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

import psycopg2

from db import load_config

SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "create_ro_role.sql"
PLACEHOLDER = "CHANGE_ME"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--password", help="Password for the read-only role.")
    g.add_argument("--generate", action="store_true", help="Generate one instead.")
    args = ap.parse_args()

    if not SQL_PATH.is_file():
        print(f"Missing {SQL_PATH} — is this running with the application image?")
        return 1

    password = args.password or secrets.token_urlsafe(18)
    sql = SQL_PATH.read_text(encoding="utf-8")
    if PLACEHOLDER not in sql:
        print(f"{SQL_PATH.name} no longer contains {PLACEHOLDER}; refusing to guess.")
        return 1

    cfg = load_config()
    conn = psycopg2.connect(
        host=cfg.host, port=cfg.port, dbname=cfg.dbname,
        user=cfg.user, password=cfg.password,
    )
    conn.autocommit = True          # the script carries its own DO block
    try:
        with conn.cursor() as cur:
            cur.execute(sql.replace(PLACEHOLDER, password))
            # The SQL only creates the role when absent, so set the password
            # explicitly — otherwise re-running silently keeps the old one and
            # the .env lines printed below would be wrong.
            cur.execute("ALTER ROLE portfoliodb_ro WITH PASSWORD %s", (password,))
    finally:
        conn.close()

    print("Read-only role ready. Add these to .env, then `make restart`:\n")
    print("PORTFOLIODB_MCP_RO_USER=portfoliodb_ro")
    print(f"PORTFOLIODB_MCP_RO_PASSWORD={password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
