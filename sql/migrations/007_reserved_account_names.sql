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
-- pdb_account_fold is the fold app/account_names.py defines: NFKC, then every
-- Unicode White_Space character, U+001C-U+001F and the invisible format
-- characters removed, then A-Z lowercased (COLLATE "C"). '(NONE)', '( none )'
-- and a '(none)' padded with no-break spaces all fold to '(none)'. The
-- definition below is the one in sql/schema.sql (a test holds the two, and the
-- character class, to the Python), repeated so this file stands alone. It
-- used to be [[:space:]], which is locale-dependent and missed NBSP, figure
-- space and narrow NBSP: such a name passed this migration while the readers
-- still treated it as the label (QA of PR #133, B1). NULL — no account —
-- passes, as it should.
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
--   UPDATE lots           SET account = 'Old none' WHERE pdb_account_fold(account) = '(none)';
--   UPDATE income         SET account = 'Old none' WHERE pdb_account_fold(account) = '(none)';
--   UPDATE cash_snapshots SET account = 'Old none' WHERE pdb_account_fold(account) = '(none)';
--
-- (If those lots were meant to have no account, set account = NULL in lots and
-- income instead; a cash balance always names an account.) The function exists
-- once sql/schema.sql has been applied, which apply_schema.py does first.
--
-- Needs a UTF8 database (normalize() does), which is what the official image
-- creates; refused otherwise.
--
-- Idempotent: the function is CREATE OR REPLACE, each constraint is added only
-- if it is missing (or replaced if an earlier draft of this file defined it
-- without pdb_account_fold), and the check for offending rows finds none once
-- they are in place. sql/schema.sql carries the same three constraints inline
-- for fresh installs.

BEGIN;

DO $$
BEGIN
  IF current_setting('server_encoding') <> 'UTF8' THEN
    RAISE EXCEPTION 'migration 007 refused, nothing changed: the database encoding is %, and the account-name rule needs UTF8 (Unicode normalisation).', current_setting('server_encoding');
  END IF;
END $$;

CREATE OR REPLACE FUNCTION pdb_account_fold(account_name text) RETURNS text
  LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  AS $fold$
    SELECT lower(regexp_replace(normalize(account_name, NFKC),
                                '[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u00AD\u1680\u180E\u2000-\u200D\u2028-\u2029\u202F\u205F-\u2060\u3000\uFEFF]',
                                '', 'g') COLLATE "C")
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
           (SELECT string_agg(format('U+%s', upper(lpad(to_hex(ascii(ch)), 4, '0'))), ' ')
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
    RAISE EXCEPTION 'migration 007 refused, nothing changed: the account name ''(none)'' is reserved for lots recorded without an account, and these rows use it (any case or spacing counts): %. Rename that account in lots, income and cash_snapshots (see sql/migrations/007_reserved_account_names.sql), then re-run apply_schema.py.', offending
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
