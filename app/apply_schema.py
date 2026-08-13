"""Create or refresh the schema, then apply migrations in order.

Runs *inside* the container against the sql/ tree baked into the image, so an
install that pulled the published image and never cloned the repository can
still set up its database:

    docker compose run --rm dashboard python app/apply_schema.py

Everything here is safe to re-run: schema.sql and schema_fd.sql are
``IF NOT EXISTS`` throughout, and each file under sql/migrations/ is written to
be idempotent. That is also why there is no migration bookkeeping table — the
files are the record, and re-applying them is a no-op.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import psycopg2

from db import load_config

# app/apply_schema.py → repo root (or /app in the image), then sql/
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def sql_files() -> list[Path]:
    """Base schema first, then migrations in filename order."""
    files = [SQL_DIR / "schema.sql", SQL_DIR / "schema_fd.sql"]
    files += sorted((SQL_DIR / "migrations").glob("*.sql"))
    return [f for f in files if f.is_file()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="List what would be applied.")
    args = ap.parse_args()

    files = sql_files()
    if not files:
        print(f"No SQL found under {SQL_DIR} — is this running with the app image?")
        return 1

    if args.dry_run:
        for f in files:
            print(f"would apply {f.relative_to(SQL_DIR.parent)}")
        return 0

    cfg = load_config()
    conn = psycopg2.connect(
        host=cfg.host, port=cfg.port, dbname=cfg.dbname,
        user=cfg.user, password=cfg.password,
    )
    # Autocommit because the files carry their own BEGIN/COMMIT; wrapping them
    # in another transaction would nest and warn.
    conn.autocommit = True
    try:
        for f in files:
            with conn.cursor() as cur:
                cur.execute(f.read_text(encoding="utf-8"))
            print(f"applied {f.relative_to(SQL_DIR.parent)}")
    finally:
        conn.close()

    print(f"{len(files)} file(s) applied. Safe to run again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
