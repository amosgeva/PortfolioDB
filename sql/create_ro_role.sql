-- Read-only role for the MCP server (defense-in-depth: the MCP surface is
-- read-only by design, this makes it read-only by privilege too).
--
-- Usage (replace CHANGE_ME with a generated password, then):
--   docker exec -i portfoliodb-postgres psql -U portfoliouser -d portfoliodb < sql/create_ro_role.sql
-- and add to the repo-root .env:
--   PORTFOLIODB_MCP_RO_USER=portfoliodb_ro
--   PORTFOLIODB_MCP_RO_PASSWORD=<the same password>
-- app/mcp/deps.py picks these up automatically; without them it falls back
-- to the full-privilege credentials (still session-read-only via
-- default_transaction_read_only).

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'portfoliodb_ro') THEN
    CREATE ROLE portfoliodb_ro LOGIN PASSWORD 'CHANGE_ME';
  END IF;
END $$;

GRANT CONNECT ON DATABASE portfoliodb TO portfoliodb_ro;
GRANT USAGE ON SCHEMA public TO portfoliodb_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO portfoliodb_ro;

-- Future tables created by portfoliouser are readable too.
ALTER DEFAULT PRIVILEGES FOR ROLE portfoliouser IN SCHEMA public
  GRANT SELECT ON TABLES TO portfoliodb_ro;
