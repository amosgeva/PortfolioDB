"""Create or refresh the schema, then apply migrations in order — once each.

Runs *inside* the container against the sql/ tree baked into the image, so an
install that pulled the published image and never cloned the repository can
still set up its database:

    docker compose run --rm dashboard python app/apply_schema.py

What it does, in one session holding an advisory lock (two concurrent runs
cannot interleave):

1. Applies ``sql/schema.sql`` and ``sql/schema_fd.sql``. Both are idempotent
   (``IF NOT EXISTS`` tables, ``CREATE OR REPLACE`` functions, views and
   triggers) and are applied every time, which is how a new function reaches
   an existing database.
2. Applies each ``sql/migrations/*.sql`` in filename order **unless the
   ``schema_migrations`` table already records it** with the same checksum
   (sha256 of the text, line endings normalised), then records it. Until
   1.8.0 there was no record at all: every file was re-run every time, and a
   changed definition under an old name never reached a database that had it
   (audit D-3).
3. A migration whose text changed after it was applied is reported loudly on
   stderr — its old and new checksums and when it was applied — and applied
   again (the files are written to be idempotent), and its record updated. A
   shipped migration should never change; when one does, someone should look.
4. Prints the WARNINGs and NOTICEs the files raise (a constraint that could
   not be validated, an index rebuilt) and lists corporate actions still
   pending review, which change no figure until approved.

Applying the files by hand with ``psql`` (what CI does) keeps working; it just
leaves ``schema_migrations`` empty, and the next run of this script applies
and records them.

Exit status: 0 on success (including a changed migration, which is reported,
not fatal), 1 when there is no SQL to apply or a file fails — then the file
is named, its own error is printed, and it is not recorded. (Migration 007
fails on purpose while an account is named ``(none)``, listing the rows.)
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg2

from db import connect_params, load_config

# app/apply_schema.py → repo root (or /app in the image), then sql/
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
BASE_FILES = ("schema.sql", "schema_fd.sql")

# Session advisory lock (namespace, kind) shared by every schema run.
LOCK_KEY = (7_210_402, 100)


def base_files() -> list[Path]:
    return [f for f in (SQL_DIR / name for name in BASE_FILES) if f.is_file()]


def migration_files() -> list[Path]:
    return sorted(f for f in (SQL_DIR / "migrations").glob("*.sql") if f.is_file())


def sql_files() -> list[Path]:
    """Base schema first, then migrations in filename order."""
    return base_files() + migration_files()


def checksum(path: Path) -> str:
    """sha256 of the file's text with line endings normalised, so a CRLF
    checkout on Windows and an LF one in the image agree."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rel(path: Path) -> str:
    return path.relative_to(SQL_DIR.parent).as_posix()


def _drain_notices(conn) -> None:
    """Print what the SQL raised (WARNING/NOTICE), minus PostgreSQL's routine
    "already exists, skipping" chatter from IF NOT EXISTS."""
    for line in conn.notices:
        msg = line.strip()
        if "already exists, skipping" in msg or "does not exist, skipping" in msg:
            continue
        stream = sys.stderr if msg.startswith("WARNING") else sys.stdout
        sys.stdout.flush()
        print(f"  {msg}", file=stream, flush=True)
    del conn.notices[:]


def applied_migrations(conn) -> dict[str, tuple[str, object]]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum, applied_at FROM schema_migrations")
        return {name: (cs, at) for name, cs, at in cur.fetchall()}


def plan(files: list[Path], applied: dict[str, tuple[str, object]]) -> list[tuple[Path, str, str]]:
    """[(file, state, checksum)] with state 'new', 'changed' or 'applied'."""
    out = []
    for f in files:
        cs = checksum(f)
        if f.name not in applied:
            out.append((f, "new", cs))
        elif applied[f.name][0] != cs:
            out.append((f, "changed", cs))
        else:
            out.append((f, "applied", cs))
    return out


def pending_reviews(conn) -> list[tuple]:
    """Corporate actions recorded by the collector and not yet approved."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT symbol, ex_date, ratio FROM corporate_actions "
                "WHERE NOT reviewed AND source <> 'manual' ORDER BY symbol, ex_date"
            )
            return cur.fetchall()
        except psycopg2.Error:
            return []


def _connect():
    cfg = load_config()
    # connect_params carries the connect timeout (audit A-6 / E-4).
    return psycopg2.connect(**connect_params(cfg))


def _execute_file(conn, path: Path) -> None:
    """Run one SQL file from the shipped ``sql/`` tree.

    The text executed is the file's, by design: this is the schema applier.
    Only files under ``SQL_DIR`` are ever passed (``base_files`` /
    ``migration_files`` glob that directory, which is baked into the image);
    nothing here takes a path or SQL from a user, so this is not an injection
    sink. Checked anyway, so a future caller cannot point it elsewhere.
    """
    resolved = path.resolve()
    if SQL_DIR.resolve() not in resolved.parents:
        raise ValueError(f"refusing to apply {path}: not under {SQL_DIR}")
    try:
        with conn.cursor() as cur:
            cur.execute(resolved.read_text(encoding="utf-8"))  # NOSONAR — shipped schema file, see docstring
    except psycopg2.Error:
        # Say which file: the error alone does not (migration 007 refuses on
        # purpose while a reserved account name is in use).
        sys.stdout.flush()
        print(f"FAILED applying {_rel(path)} - nothing in it was applied or recorded.",
              file=sys.stderr, flush=True)
        raise


def _record(conn, path: Path, cs: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO schema_migrations (filename, checksum, applied_at)
            VALUES (%s, %s, now())
            ON CONFLICT (filename) DO UPDATE
              SET checksum = EXCLUDED.checksum, applied_at = EXCLUDED.applied_at
            """,
            (path.name, cs),
        )


def _warn_changed(path: Path, old_cs: str, at, cs: str) -> None:
    print(
        f"\nWARNING: {_rel(path)} HAS CHANGED since it was applied on {at:%Y-%m-%d %H:%M}.\n"
        f"  recorded sha256 {old_cs}\n"
        f"  current  sha256 {cs}\n"
        "  A shipped migration should never change. It is applied again now (the\n"
        "  files are idempotent) - review what changed and why.\n",
        file=sys.stderr,
    )


def _dry_run(conn) -> int:
    for f in base_files():
        print(f"would apply {_rel(f)} (always applied)")
    try:
        applied = applied_migrations(conn)
    except psycopg2.Error:
        applied = {}
    for f, state, _cs in plan(migration_files(), applied):
        print(f"{'would apply' if state != 'applied' else 'up to date '} {_rel(f)} [{state}]")
    return 0


def _apply_migrations(conn) -> dict[str, int]:
    applied = applied_migrations(conn)
    counts = {"new": 0, "changed": 0, "applied": 0}
    for f, state, cs in plan(migration_files(), applied):
        counts[state] += 1
        if state == "applied":
            print(f"up to date {_rel(f)} (applied {applied[f.name][1]:%Y-%m-%d %H:%M})")
            continue
        if state == "changed":
            _warn_changed(f, applied[f.name][0], applied[f.name][1], cs)
        _execute_file(conn, f)
        _record(conn, f, cs)
        print(f"applied {_rel(f)}" + (" (re-applied: changed)" if state == "changed" else ""))
        _drain_notices(conn)
    return counts


def _print_pending(conn) -> None:
    pending = pending_reviews(conn)
    if not pending:
        return
    print(
        f"\n{len(pending)} corporate action(s) recorded by the collector are PENDING REVIEW "
        "and change no figure until approved:"
    )
    for sym, day, ratio in pending:
        print(f"  {sym} {day} ratio {ratio}")
    print("Review them with: python app/check_splits.py")


def run(conn, *, dry_run: bool = False) -> int:
    """Apply everything on an open connection. Returns the exit status."""
    # Autocommit because the files carry their own BEGIN/COMMIT; wrapping them
    # in another transaction would nest and warn.
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s, %s)", LOCK_KEY)
    try:
        if dry_run:
            return _dry_run(conn)
        for f in base_files():
            _execute_file(conn, f)
            print(f"applied {_rel(f)}")
            _drain_notices(conn)
        counts = _apply_migrations(conn)
        print(
            f"Schema refreshed; migrations: {counts['new']} applied, "
            f"{counts['applied']} already up to date"
            + (f", {counts['changed']} CHANGED (see above)" if counts["changed"] else "")
            + ". Safe to run again."
        )
        _print_pending(conn)
        return 0
    finally:
        # A file that fails inside its own BEGIN leaves the session in an
        # aborted transaction. Without this ROLLBACK the unlock below fails as
        # well, and its "current transaction is aborted" replaces the error
        # that says what went wrong.
        if conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", LOCK_KEY)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be applied (and, with a database, what is already recorded).")
    args = ap.parse_args(argv)

    if not sql_files():
        print(f"No SQL found under {SQL_DIR} — is this running with the app image?")
        return 1

    try:
        conn = _connect()
    except Exception as e:
        if args.dry_run:
            print(f"(no database: {e}) — listing files only")
            for f in sql_files():
                print(f"would apply {_rel(f)}")
            return 0
        raise
    try:
        return run(conn, dry_run=args.dry_run)
    except psycopg2.Error as e:
        print(f"FAILED: {str(e).strip()}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
