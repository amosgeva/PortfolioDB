-- 005: value constraints migration 003 did not cover.
--
--   corporate_actions_ratio_not_nan     ratio <> 'NaN'                 (audit D-4)
--   corporate_actions_kind_ratio        SPLIT > 1, REVERSE_SPLIT < 1,
--                                       NONE = 1                       (audit D-4)
--   price_snapshots_last_price_positive last_price > 0                 (audit F-15)
--
-- PostgreSQL's numeric NaN sorts above every number, so `ratio > 0` admitted
-- it — the hole 003 closed for every other numeric column. Nothing tied the
-- kind to the ratio either. And `last_price >= 0` admitted the zero quote that
-- zeroed every value and time-weighted return computed across it; the
-- collector and the importer now refuse it, and so does this.
--
-- The same pattern as 004, for the same reason: each constraint is added NOT
-- VALID (new rows are checked at once), then validated against existing rows.
-- A row that fails leaves that one constraint unvalidated with a WARNING that
-- says how to find the rows, the upgrade continues, and every later
-- apply_schema.py run tries again (sql/schema.sql re-validates any unvalidated
-- constraint). For zero prices — which an older collector could store — inspect with
--   SELECT symbol, ts, last_price FROM price_snapshots WHERE last_price <= 0;
-- and delete those rows (a zero is not a price; readers already skip them).
--
-- The near-duplicate guard for corporate actions (audit F-16) is a trigger,
-- and lives in sql/schema.sql with the other CREATE OR REPLACE objects, which
-- reach an existing database every time that file is re-applied.

BEGIN;

DO $$
DECLARE
  spec RECORD;
  n BIGINT;
BEGIN
  FOR spec IN
    SELECT * FROM (VALUES
      ('corporate_actions', 'corporate_actions_ratio_not_nan',
       'CHECK (ratio <> ''NaN''::numeric)',
       'NOT (ratio <> ''NaN''::numeric)'),
      ('corporate_actions', 'corporate_actions_kind_ratio',
       'CHECK ((kind = ''SPLIT'' AND ratio > 1) OR (kind = ''REVERSE_SPLIT'' AND ratio < 1) OR (kind = ''NONE'' AND ratio = 1))',
       'NOT ((kind = ''SPLIT'' AND ratio > 1) OR (kind = ''REVERSE_SPLIT'' AND ratio < 1) OR (kind = ''NONE'' AND ratio = 1))'),
      ('price_snapshots', 'price_snapshots_last_price_positive',
       'CHECK (last_price > 0)',
       'NOT (last_price > 0)')
    ) AS t(tbl, conname, def, violation)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = spec.conname AND conrelid = spec.tbl::regclass) THEN
      EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s NOT VALID', spec.tbl, spec.conname, spec.def);
    END IF;
    BEGIN
      EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', spec.tbl, spec.conname);
    EXCEPTION WHEN check_violation THEN
      EXECUTE format('SELECT count(*) FROM %I WHERE %s', spec.tbl, spec.violation) INTO n;
      RAISE WARNING '% is enforced for new rows but NOT validated: % existing row(s) of % violate it (find them with: SELECT * FROM % WHERE %). Fix them and re-run apply_schema.py.',
        spec.conname, n, spec.tbl, spec.tbl, spec.violation;
    END;
  END LOOP;
END $$;

COMMIT;
