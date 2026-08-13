# Migrations

Numbered, forward-only migrations for databases that predate a schema change.
Fresh installs never need these — `sql/schema.sql` always describes the full
current schema (and stays `IF NOT EXISTS`-idempotent).

Apply in filename order:

```bash
docker exec -i portfoliodb-postgres psql -U portfoliouser -d portfoliodb < sql/migrations/001_settings.sql
```

Each file is idempotent (safe to re-run). When adding a migration, make the
same change in `sql/schema.sql` in the same commit, and bump the schema
version header there plus `SCHEMA_VERSION` in `app/mcp/services/cutoff.py`.
