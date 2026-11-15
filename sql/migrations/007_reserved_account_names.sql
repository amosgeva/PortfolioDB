-- 007: '(none)' is not an account name (rescore NEW-1, audit X-3 / X-8).
--
-- A lot recorded without an account has account IS NULL. Every report, the
-- dashboard and the MCP server label that bucket '(none)', and the MCP server
-- accepts '(none)' back as the filter for it (account IS NULL). Nothing
-- stopped an account from actually being *named* '(none)', and one that was
-- could not be selected, merged into the unassigned bucket in the account
-- listing, disappeared from allocation by account (a $30 book allocated as
-- $10), and lent its cash to the unassigned bucket.
--
-- The writers now refuse the name (app/account_names.py), and this adds the
-- same rule to the three tables that hold an account, for any writer that
-- does not come through them:
--
--   lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)'
--
-- Case and whitespace are ignored: '(NONE)' and '( none )' read as the same
-- label to a person. NULL — no account — passes, as it should.
--
-- Existing rows are NOT rewritten, and the constraint is NOT left unvalidated
-- with a warning the way 004 and 005 do it: an account by that name makes MCP
-- figures silently wrong, not merely unrenderable, so this migration REFUSES
-- (raises, changes nothing, and is not recorded) while any such row exists,
-- and names every one of them. Rename the account — to a name it does not
-- already share with another account's rows on the same day, or the dedupe
-- index / primary key will say so — in all three tables, then re-run
-- `python app/apply_schema.py`:
--
--   UPDATE lots           SET account = 'Old none' WHERE lower(regexp_replace(account, '[[:space:]]', '', 'g')) = '(none)';
--   UPDATE income         SET account = 'Old none' WHERE lower(regexp_replace(account, '[[:space:]]', '', 'g')) = '(none)';
--   UPDATE cash_snapshots SET account = 'Old none' WHERE lower(regexp_replace(account, '[[:space:]]', '', 'g')) = '(none)';
--
-- (If those lots were meant to have no account, set account = NULL in lots and
-- income instead; a cash balance always names an account.)
--
-- Idempotent: each constraint is added only if it is missing, and the check
-- for offending rows finds none once they are in place. sql/schema.sql
-- carries the same three constraints inline for fresh installs.

BEGIN;

DO $$
DECLARE
  offending TEXT;
BEGIN
  SELECT string_agg(format('%s: %s row(s) with account %L', tbl, n, account), '; '
                    ORDER BY tbl, account)
    INTO offending
  FROM (
    SELECT 'lots' AS tbl, account, count(*) AS n FROM lots
     WHERE lower(regexp_replace(account, '[[:space:]]', '', 'g')) = '(none)' GROUP BY account
    UNION ALL
    SELECT 'income', account, count(*) FROM income
     WHERE lower(regexp_replace(account, '[[:space:]]', '', 'g')) = '(none)' GROUP BY account
    UNION ALL
    SELECT 'cash_snapshots', account, count(*) FROM cash_snapshots
     WHERE lower(regexp_replace(account, '[[:space:]]', '', 'g')) = '(none)' GROUP BY account
  ) s;
  IF offending IS NOT NULL THEN
    RAISE EXCEPTION 'migration 007 refused, nothing changed: the account name ''(none)'' is reserved for lots recorded without an account, and these rows use it: %. Rename that account in lots, income and cash_snapshots (see sql/migrations/007_reserved_account_names.sql), then re-run apply_schema.py.', offending
      USING ERRCODE = 'check_violation',
            HINT = 'UPDATE lots SET account = ''<another name>'' WHERE lower(regexp_replace(account, ''[[:space:]]'', '''', ''g'')) = ''(none)''; and the same for income and cash_snapshots.';
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'lots_account_not_reserved' AND conrelid = 'lots'::regclass) THEN
    ALTER TABLE lots ADD CONSTRAINT lots_account_not_reserved
      CHECK (lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'income_account_not_reserved' AND conrelid = 'income'::regclass) THEN
    ALTER TABLE income ADD CONSTRAINT income_account_not_reserved
      CHECK (lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'cash_snapshots_account_not_reserved'
                   AND conrelid = 'cash_snapshots'::regclass) THEN
    ALTER TABLE cash_snapshots ADD CONSTRAINT cash_snapshots_account_not_reserved
      CHECK (lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)');
  END IF;
END $$;

COMMIT;
