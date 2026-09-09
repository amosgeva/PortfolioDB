-- 003: reject NaN in every numeric ledger column.
--
-- PostgreSQL's numeric type has a NaN, and it sorts ABOVE every finite value:
-- 'NaN'::numeric > 0 is true. So the positivity checks this schema has always
-- had — quantity > 0, price >= 0, cash >= 0 — are satisfied by NaN, and a NaN
-- that reached an INSERT was stored. The write paths now refuse non-finite
-- input before it gets here (app/ledger_numbers.py); this is the backstop for
-- any writer that does not come through them.
--
-- Infinity needs no constraint: NUMERIC(20,8) already rejects it ("cannot hold
-- an infinite value"). NaN is the one that gets through.
--
-- Idempotent: each constraint is added only if a constraint of that name is
-- not already on the table, so re-running is a no-op. Adding a CHECK validates
-- existing rows; a NaN already in the ledger fails this migration on purpose —
-- it is a defect to look at, not something to grandfather in.

BEGIN;

DO $$
DECLARE
  spec RECORD;
BEGIN
  FOR spec IN
    SELECT * FROM (VALUES
      ('lots',            'quantity'),
      ('lots',            'price'),
      ('lots',            'fees'),
      ('price_snapshots', 'last_price'),
      ('price_snapshots', 'bid'),
      ('price_snapshots', 'ask'),
      ('price_snapshots', 'volume'),
      ('cash_snapshots',  'cash'),
      ('income',          'amount'),
      ('income',          'tax_withheld'),
      ('income',          'per_share')
    ) AS t(tbl, col)
  LOOP
    IF EXISTS (
         SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = spec.tbl AND column_name = spec.col
       )
       AND NOT EXISTS (
         SELECT 1 FROM pg_constraint
         WHERE conname = spec.tbl || '_' || spec.col || '_not_nan'
       )
    THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT %I CHECK (%I <> ''NaN''::numeric)',
        spec.tbl, spec.tbl || '_' || spec.col || '_not_nan', spec.col
      );
    END IF;
  END LOOP;
END $$;

COMMIT;
