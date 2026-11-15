-- Read-only role for the MCP server (defense-in-depth: the MCP surface is
-- read-only by design, this makes it read-only by privilege too).
--
-- Usage: run `make ro-role` (or `python app/create_ro_role.py --generate`),
-- which executes this file with a throwaway password, sets the real one as a
-- bound parameter, and prints the two lines to add to the repo-root .env:
--   PORTFOLIODB_MCP_RO_USER=portfoliodb_ro
--   PORTFOLIODB_MCP_RO_PASSWORD=<generated>
-- The MCP server requires them (app/mcp/deps.py): without them it refuses to
-- connect, and the only alternative is the explicit, logged
-- PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1 opt-out, which also needs the
-- application's password handed to it.
--
-- Idempotent and re-runnable: an existing role keeps working and has its
-- grants reset to exactly the list below. Re-run it after upgrading — the
-- grant list is what changed in 1.8.0.

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'portfoliodb_ro') THEN
    CREATE ROLE portfoliodb_ro LOGIN PASSWORD 'CHANGE_ME';
  END IF;
END $$;

-- Server-side deadlines for every session the role opens. The MCP pool sets
-- the same values per connection; these hold even for a session that does not
-- (audit M-2). statement_timeout bounds one query; the idle-in-transaction
-- timeout closes a session a crashed request left holding a transaction.
ALTER ROLE portfoliodb_ro SET statement_timeout = '30s';
ALTER ROLE portfoliodb_ro SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE portfoliodb_ro SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE portfoliodb TO portfoliodb_ro;
GRANT USAGE ON SCHEMA public TO portfoliodb_ro;

-- Start from nothing, then grant exactly what the MCP reads (audit M-10).
-- Earlier versions granted SELECT on every table, present and future, which
-- included chat_log (the Advisor conversation history) that no tool,
-- resource or prompt reads. REVOKE first so a re-run narrows an existing
-- install instead of only adding to it.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM portfoliodb_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM portfoliodb_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE portfoliouser IN SCHEMA public
  REVOKE SELECT ON TABLES FROM portfoliodb_ro;

-- Each table and the MCP surface that reads it:
--   instruments, lots, price_snapshots, cash_snapshots, income,
--   corporate_actions, snapshot_runs  — the ledger and every tool over it
--   settings        — reporting timezone and the saved investor one-pager
--                     (cutoff, portfolio://philosophy, the prompts)
--   advisor_briefs  — portfolio://reports/executive/latest
--   fd_*            — the fundamentals tools and the executive report
--   corporate_actions_in_effect — read by lot_open_quantities() (1.8.0 ledger
--                     core), the SQL net-quantity function the active-symbol
--                     queries call; it runs as the caller, so the caller needs
--                     the view
-- A table missing from this database (the fd_* tables before enrichment's
-- schema was applied) is skipped rather than failing the whole script.
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'instruments', 'lots', 'price_snapshots', 'cash_snapshots', 'income',
    'corporate_actions', 'snapshot_runs', 'settings', 'advisor_briefs',
    'fd_company_facts', 'fd_financial_metrics', 'fd_financial_statements',
    'fd_earnings', 'fd_filings', 'fd_insider_trades',
    'fd_institutional_ownership', 'fd_news',
    'corporate_actions_in_effect'
  ] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('GRANT SELECT ON public.%I TO portfoliodb_ro', t);
    END IF;
  END LOOP;
END $$;

-- The SQL net-quantity function, when this database has it. Functions are
-- executable by PUBLIC by default; granted explicitly so a hardened database
-- that revoked that still serves the MCP.
DO $$ BEGIN
  IF to_regprocedure('public.lot_open_quantities(date)') IS NOT NULL THEN
    GRANT EXECUTE ON FUNCTION public.lot_open_quantities(date) TO portfoliodb_ro;
  END IF;
END $$;
