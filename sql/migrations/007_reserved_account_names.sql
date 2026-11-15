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
--   pdb_account_fold(account) <> '(none)'
--
-- pdb_account_fold is the fold app/account_names.py defines: delete every
-- character that is not an ASCII letter or ( or ), then lowercase A-Z. It uses
-- no Unicode tables and no locale, so Python and PostgreSQL agree by
-- construction, whatever their versions. '(NONE)', '( none )' and a '(none)'
-- padded with no-break or zero-width spaces all fold to '(none)'; a name made
-- of other letters (fullwidth, mathematical, outlined) does not, on either
-- side. Two earlier drafts of this file used [[:space:]] and then NFKC, and
-- each let a name through that the readers took for the label (QA of PR #133,
-- B1 and B2). The definition below is the one in sql/schema.sql (a test holds
-- the two, and the Python, together), repeated so this file stands alone.
-- NULL - no account - passes, as it should.
--
-- Existing rows are NOT rewritten, and the constraint is NOT left unvalidated
-- with a warning the way 004 and 005 do it: an account by that name makes MCP
-- figures silently wrong, not merely unrenderable, so this migration REFUSES
-- (raises, changes nothing, and is not recorded) while any such row exists,
-- and names every one of them with its code points. Rename the account - to a
-- name it does not already share with another account's rows on the same day,
-- or the dedupe index / primary key will say so - in all three tables, then
-- re-run `python app/apply_schema.py`:
--
--   UPDATE lots           SET account = 'Old none' WHERE pdb_account_fold(account) = '(none)';
--   UPDATE income         SET account = 'Old none' WHERE pdb_account_fold(account) = '(none)';
--   UPDATE cash_snapshots SET account = 'Old none' WHERE pdb_account_fold(account) = '(none)';
--
-- (If those lots were meant to have no account, set account = NULL in lots and
-- income instead; a cash balance always names an account.) The function exists
-- once sql/schema.sql has been applied, which apply_schema.py does first.
--
-- Idempotent: the function is CREATE OR REPLACE, each constraint is added only
-- if it is missing (or replaced if an earlier draft of this file defined it
-- without pdb_account_fold), and the check for offending rows - which runs
-- with the current function, so it also catches a row an earlier draft's fold
-- let through - finds none once they are in place. sql/schema.sql carries the
-- same three constraints inline for fresh installs. This file is ASCII, so it
-- runs on a database of any encoding.

BEGIN;

CREATE OR REPLACE FUNCTION pdb_account_fold(account_name text) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  AS $fold$
    SELECT translate(regexp_replace(account_name COLLATE "C", '[^A-Za-z()]', '', 'g'),
                     'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
  $fold$;

DO $$
DECLARE
  offending TEXT;
BEGIN
  SELECT string_agg(format('%s: %s row(s) with account %L (%s)', tbl, n, account, codepoints), '; '
                    ORDER BY tbl, account)
    INTO offending
  FROM (
    SELECT tbl, account, n,
           (SELECT string_agg('U+' || upper(CASE WHEN length(to_hex(ascii(ch))) < 4
                                                 THEN lpad(to_hex(ascii(ch)), 4, '0')
                                                 ELSE to_hex(ascii(ch)) END), ' ')
              FROM regexp_split_to_table(account, '') AS ch) AS codepoints
    FROM (
      SELECT 'lots' AS tbl, account, count(*) AS n FROM lots
       WHERE pdb_account_fold(account) = '(none)' GROUP BY account
      UNION ALL
      SELECT 'income', account, count(*) FROM income
       WHERE pdb_account_fold(account) = '(none)' GROUP BY account
      UNION ALL
      SELECT 'cash_snapshots', account, count(*) FROM cash_snapshots
       WHERE pdb_account_fold(account) = '(none)' GROUP BY account
    ) found
  ) s;
  IF offending IS NOT NULL THEN
    RAISE EXCEPTION 'migration 007 refused, nothing changed: the account name ''(none)'' is reserved for lots recorded without an account, and these rows use it (any case, spacing or punctuation counts): %. Rename that account in lots, income and cash_snapshots (see sql/migrations/007_reserved_account_names.sql), then re-run apply_schema.py.', offending
      USING ERRCODE = 'check_violation',
            HINT = 'UPDATE lots SET account = ''<another name>'' WHERE pdb_account_fold(account) = ''(none)''; and the same for income and cash_snapshots.';
  END IF;
END $$;

DO $$
DECLARE
  t TEXT;
  c TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['lots', 'income', 'cash_snapshots'] LOOP
    c := t || '_account_not_reserved';
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = c AND conrelid = to_regclass(t)
                     AND pg_get_constraintdef(oid) LIKE '%pdb_account_fold%') THEN
      EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', t, c);
      EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I CHECK (pdb_account_fold(account) <> ''(none)'')', t, c);
    END IF;
  END LOOP;
END $$;

COMMIT;
