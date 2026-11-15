# Migrations

Numbered, forward-only migrations for databases that predate a schema change.
`sql/schema.sql` aims to describe the full current schema (and stays
`IF NOT EXISTS`-idempotent), but a fresh database is installed the same way as
an existing one: `apply_schema.py` runs and records every migration there too,
and some still add something on a fresh database (003, for example, adds the
named NaN check on `price_snapshots.volume`). Install with `apply_schema.py`,
not with `schema.sql` alone.

## Applying them

```bash
docker compose run --rm dashboard python app/apply_schema.py
```

`apply_schema.py` applies `sql/schema.sql` and `sql/schema_fd.sql` every time,
then each migration in filename order **once**: it records every file it
applies in the `schema_migrations` table (filename, sha256 of the text with
line endings normalised, applied_at) and skips recorded ones on the next run.
A recorded file whose text has since changed is reported loudly on stderr and
applied again. A shipped migration should never change; when one does,
someone should look.

Applying the files by hand still works — every file is idempotent:

```bash
docker exec -i portfoliodb-postgres psql -U portfoliouser -d portfoliodb < sql/schema.sql
docker exec -i portfoliodb-postgres psql -U portfoliouser -d portfoliodb < sql/migrations/004_symbol_check.sql
```

It just leaves `schema_migrations` empty, and the next `apply_schema.py` run
applies and records them. (CI applies them this way, then the database tests
run `apply_schema.py` against the result.)

## Writing one

- **Idempotent.** Re-running the file must be a no-op: `IF NOT EXISTS`, or a
  `DO` block that checks `pg_constraint` / `information_schema` first.
- **Make the same change in `sql/schema.sql`** in the same commit (inline, for
  fresh installs), and bump the schema version header there plus
  `SCHEMA_VERSION` in `app/mcp/services/cutoff.py`.
- **A CHECK on a table with existing rows:** add it `NOT VALID` (it is enforced
  for new rows at once), then `VALIDATE CONSTRAINT` inside an exception block
  that turns a `check_violation` into a `WARNING` saying how to find the
  offending rows. The upgrade then never fails on old data, and
  `sql/schema.sql` re-tries every unvalidated CHECK on each run until the rows
  are fixed. 004 and 005 are the pattern.
- **Unless an offending row makes figures silently wrong.** Then refuse: check
  for such rows first and `RAISE EXCEPTION` naming every one and how to fix
  it, so the file changes nothing and is not recorded until they are fixed.
  Never rewrite them silently. 007 is the pattern: an account named `(none)`
  is indistinguishable from "no account" in every reader, so a constraint left
  unvalidated would leave the MCP's figures wrong behind a warning.
- **Functions, views and triggers** go in `sql/schema.sql` as
  `CREATE OR REPLACE`, not in a migration: that file is re-applied on every
  upgrade, so they reach existing databases from there (005 explains this for
  the corporate-actions trigger).

## The files

| File | What it does |
|---|---|
| `001_settings.sql` | The `settings` table. |
| `002_market_benchmarks.sql` | `instruments.benchmark` for the market-overview strip. |
| `003_finite_numeric_checks.sql` | `<> 'NaN'` checks on every numeric ledger column. |
| `004_symbol_check.sql` | `instruments_symbol_format`: what a symbol may be (= `symbol_paths.SYMBOL_FILENAME_RE`). |
| `005_corporate_actions_ratio_finite.sql` | `corporate_actions` ratio not NaN and consistent with `kind`; `price_snapshots.last_price > 0`. |
| `006_price_alerts_and_lot_audit.sql` | `price_alerts` (Manage → Alerts) and `lot_deletions` (the audit row for each lot deleted from the Manage page). |
| `007_reserved_account_names.sql` | `*_account_not_reserved` on `lots`, `income`, `cash_snapshots`: no account folds to `(none)` — the label for `account IS NULL` — through `pdb_account_fold` (NFKC, Unicode whitespace and invisible format characters removed, A–Z lowercased; = `account_names.fold`). Refuses, changing nothing, while a row uses it. Needs a UTF8 database. |
