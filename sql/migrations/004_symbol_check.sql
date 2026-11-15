-- 004: constrain what a symbol may be (audit A-3 / D-2).
--
-- instruments.symbol was free TEXT, and every writer but the Manage form only
-- stripped and uppercased it, so a CSV import could plant markup as a "symbol"
-- that the dashboard then rendered. The writers now validate with
-- app/symbol_paths.py (normalize_symbol); this is the same pattern as the
-- database's own rule, for any writer that does not come through them:
--
--   ^[A-Z0-9^][A-Z0-9.=^_-]{0,31}$        (= symbol_paths.SYMBOL_FILENAME_RE)
--
-- 1-32 characters: A-Z, 0-9 and . - = ^ _, starting with a letter, a digit or
-- ^ — NVDA, BRK.B, BRK-B, ^GSPC, ES=F, 0700.HK, TA35.TA all pass.
--
-- Idempotent and safe on any database. The constraint is added NOT VALID, so
-- new and updated rows are checked at once, then validated against the rows
-- already there. If an existing symbol fails, validation is skipped with a
-- WARNING listing it and the upgrade carries on; every later
-- `python app/apply_schema.py` tries again (sql/schema.sql re-validates any
-- unvalidated constraint) — rename or remove the listed symbols and re-run it. (A symbol that fails is one the dashboard
-- cannot safely render; it is a defect to look at, not to grandfather in.)

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'instruments_symbol_format'
                   AND conrelid = 'instruments'::regclass) THEN
    ALTER TABLE instruments ADD CONSTRAINT instruments_symbol_format
      CHECK (symbol ~ '^[A-Z0-9^][A-Z0-9.=^_-]{0,31}$') NOT VALID;
  END IF;
END $$;

DO $$
DECLARE
  bad TEXT;
BEGIN
  ALTER TABLE instruments VALIDATE CONSTRAINT instruments_symbol_format;
EXCEPTION WHEN check_violation THEN
  SELECT string_agg(quote_literal(symbol), ', ' ORDER BY symbol) INTO bad
  FROM (SELECT symbol FROM instruments
        WHERE symbol !~ '^[A-Z0-9^][A-Z0-9.=^_-]{0,31}$' LIMIT 20) s;
  RAISE WARNING 'instruments_symbol_format is enforced for new rows but NOT validated: these existing symbols do not match it: %. Fix them and re-run apply_schema.py.', bad;
END $$;

COMMIT;
