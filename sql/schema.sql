-- PortfolioDB schema v0.7
-- Append-only snapshots, lot-based cost basis, FIFO P&L
--
-- Every numeric ledger column carries a `<> 'NaN'` check besides its sign
-- check: PostgreSQL's numeric NaN sorts above every finite value, so
-- `quantity > 0` alone admits it (migration 003, audit F09). Infinity needs no
-- check — NUMERIC(20,8) cannot hold it.
--
-- This file is re-applied on every upgrade (app/apply_schema.py runs it before
-- the migrations), so everything in it is idempotent: tables and indexes are
-- IF NOT EXISTS, and the functions, views and triggers at the end are
-- CREATE OR REPLACE — they reach an existing database from here, with no
-- migration. Constraints on existing tables arrive by migration
-- (sql/migrations/), and are repeated inline below for fresh installs.

BEGIN;

CREATE TABLE IF NOT EXISTS instruments (
  symbol        TEXT PRIMARY KEY,
  name          TEXT,
  asset_type    TEXT NOT NULL DEFAULT 'stock',
  currency      TEXT NOT NULL DEFAULT 'USD',
  exchange      TEXT,
  watchlist     BOOLEAN NOT NULL DEFAULT FALSE,
  -- Collected for context, never held: index futures, volatility. Kept out of
  -- the portfolio by having no lots, and out of the watchlist rail and Data
  -- Health by this flag. See sql/migrations/002_market_benchmarks.sql.
  benchmark     BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- What a symbol may be: the pattern of app/symbol_paths.py
  -- (SYMBOL_FILENAME_RE). A symbol is rendered into the dashboard and becomes a
  -- file name; markup is not a ticker (audit A-3/D-2, migration 004).
  CONSTRAINT instruments_symbol_format CHECK (symbol ~ '^[A-Z0-9^][A-Z0-9.=^_-]{0,31}$')
);

-- Region allocation (Pick 5): nullable country code, seedable from
-- fd_company_facts.location. Additive + idempotent so re-applying is safe.
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS country TEXT;

-- Sector allocation: instrument-level sector so the dashboard reads all three
-- allocation dimensions (asset_type / sector / country) from one table.
-- Populated by app/enrich_instruments.py (yfinance). Additive + idempotent.
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS sector TEXT;

CREATE TABLE IF NOT EXISTS lots (
  id            BIGSERIAL PRIMARY KEY,
  symbol        TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  account       TEXT,
  side          TEXT NOT NULL DEFAULT 'BUY' CHECK (side IN ('BUY','SELL')),
  trade_date    DATE NOT NULL,
  quantity      NUMERIC(20,8) NOT NULL CHECK (quantity > 0),
  price         NUMERIC(20,8) NOT NULL CHECK (price >= 0),
  fees          NUMERIC(20,8) NOT NULL DEFAULT 0 CHECK (fees >= 0),
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT lots_quantity_not_nan CHECK (quantity <> 'NaN'::numeric),
  CONSTRAINT lots_price_not_nan    CHECK (price <> 'NaN'::numeric),
  CONSTRAINT lots_fees_not_nan     CHECK (fees <> 'NaN'::numeric),
  -- '(none)' is the label for account IS NULL everywhere it is read; an
  -- account by that name (any case or spacing) is refused (migration 007).
  CONSTRAINT lots_account_not_reserved
    CHECK (lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)')
);

-- Prevent accidental duplicate lot inserts during imports.
-- `side` MUST be part of the key: without it a BUY and a SELL sharing
-- symbol/account/date/qty/price collide and ON CONFLICT DO NOTHING silently
-- drops the second trade. Every writer reports a refused duplicate rather than
-- claiming success (app/ledger_writes.py; audit N-2/D-1).
CREATE UNIQUE INDEX IF NOT EXISTS lots_dedupe_idx
  ON lots(symbol, COALESCE(account,''), side, trade_date, quantity, price);

-- `IF NOT EXISTS` never replaces an index that exists under the same name, so
-- a database created before `side` joined the key kept the old one — and kept
-- dropping the second of a same-day BUY/SELL pair (audit D-3). Rebuild it when
-- its definition lacks `side`; adding a column only loosens a unique key, so
-- the rebuild cannot fail on existing rows.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE schemaname = current_schema() AND indexname = 'lots_dedupe_idx'
               AND indexdef NOT LIKE '%side%') THEN
    DROP INDEX lots_dedupe_idx;
    CREATE UNIQUE INDEX lots_dedupe_idx
      ON lots(symbol, COALESCE(account,''), side, trade_date, quantity, price);
    RAISE NOTICE 'lots_dedupe_idx rebuilt with side in the key';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS price_snapshots (
  ts            TIMESTAMPTZ NOT NULL,
  symbol        TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  -- Strictly positive: a zero quote is a vendor fault, and one stored zero
  -- zeroed every value and return computed across it (audit F-15, migration 005).
  last_price    NUMERIC(20,8) NOT NULL CONSTRAINT price_snapshots_last_price_positive CHECK (last_price > 0),
  bid           NUMERIC(20,8) CHECK (bid >= 0),
  ask           NUMERIC(20,8) CHECK (ask >= 0),
  source        TEXT NOT NULL DEFAULT 'yfinance',
  session       TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT price_snapshots_last_price_not_nan CHECK (last_price <> 'NaN'::numeric),
  CONSTRAINT price_snapshots_bid_not_nan        CHECK (bid <> 'NaN'::numeric),
  CONSTRAINT price_snapshots_ask_not_nan        CHECK (ask <> 'NaN'::numeric),
  PRIMARY KEY (symbol, ts)
);

CREATE INDEX IF NOT EXISTS price_snapshots_ts_idx ON price_snapshots(ts);

-- Session volume, recorded so that liquidity history starts accumulating. No
-- metric is built on it yet and none should be until there is enough history:
-- collection began 2026-08-12, so any average-daily-volume figure is
-- meaningless for months.
--
-- IMPORTANT — this is **cumulative session volume at the moment of the
-- snapshot**, not a daily total. It grows through the trading day, so a
-- mid-session row is a partial count. Anything computing ADV must take the
-- LAST snapshot per reporting-timezone day (the same DISTINCT ON ... ORDER BY ts
-- DESC pattern the daily price series already uses) and must skip days whose
-- last snapshot falls before the close.
--
-- NUMERIC rather than BIGINT: yfinance returns floats for some instruments,
-- and an index fund's volume can exceed what an INTEGER would hold.
ALTER TABLE price_snapshots ADD COLUMN IF NOT EXISTS volume NUMERIC(20,2)
  CHECK (volume IS NULL OR volume >= 0);

-- Matches the live table: natural key (account, ts), no surrogate id.
CREATE TABLE IF NOT EXISTS cash_snapshots (
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  account       TEXT NOT NULL,
  cash          NUMERIC(20,8) NOT NULL CHECK (cash >= 0),
  currency      TEXT NOT NULL DEFAULT 'USD',
  note          TEXT,
  PRIMARY KEY (account, ts),
  CONSTRAINT cash_snapshots_cash_not_nan CHECK (cash <> 'NaN'::numeric),
  CONSTRAINT cash_snapshots_account_not_reserved
    CHECK (lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)')
);

CREATE INDEX IF NOT EXISTS cash_snapshots_ts_idx ON cash_snapshots(ts);

-- Income ledger (Pick 1): dividends / interest / cap-gain distributions.
-- Append-only and kept OUT of `lots` so the FIFO engine is untouched; income
-- contributes to total return as a separate additive term, never to basis.
CREATE TABLE IF NOT EXISTS income (
  id            BIGSERIAL PRIMARY KEY,
  symbol        TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  account       TEXT,
  kind          TEXT NOT NULL CHECK (kind IN ('DIVIDEND','INTEREST','CAP_GAIN_DIST')),
  ex_date       DATE,
  pay_date      DATE NOT NULL,
  amount        NUMERIC(20,8) NOT NULL CHECK (amount >= 0),   -- gross cash received
  currency      TEXT NOT NULL DEFAULT 'USD',
  tax_withheld  NUMERIC(20,8) NOT NULL DEFAULT 0 CHECK (tax_withheld >= 0),
  per_share     NUMERIC(20,8),
  source        TEXT NOT NULL DEFAULT 'manual',
  notes         TEXT,
  CONSTRAINT income_amount_not_nan       CHECK (amount <> 'NaN'::numeric),
  CONSTRAINT income_tax_withheld_not_nan CHECK (tax_withheld <> 'NaN'::numeric),
  CONSTRAINT income_per_share_not_nan    CHECK (per_share <> 'NaN'::numeric),
  CONSTRAINT income_account_not_reserved
    CHECK (lower(regexp_replace(account, '[[:space:]]', '', 'g')) <> '(none)'),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Best-effort dedupe guard for backfill imports (mirrors lots_dedupe_idx).
-- `kind` is part of the key so e.g. a DIVIDEND and an INTEREST payment with
-- the same symbol/account/date/amount don't collide.
CREATE UNIQUE INDEX IF NOT EXISTS income_dedupe_idx
  ON income(symbol, COALESCE(account,''), kind, pay_date, amount);

-- Same self-repair as lots_dedupe_idx, for a database created before `kind`
-- joined this key.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE schemaname = current_schema() AND indexname = 'income_dedupe_idx'
               AND indexdef NOT LIKE '%kind%') THEN
    DROP INDEX income_dedupe_idx;
    CREATE UNIQUE INDEX income_dedupe_idx
      ON income(symbol, COALESCE(account,''), kind, pay_date, amount);
    RAISE NOTICE 'income_dedupe_idx rebuilt with kind in the key';
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS income_pay_date_idx ON income(pay_date);

-- Corporate actions, and the register of investigated price discontinuities.
-- Source of truth for split adjustment, applied at *read* time — `lots` and
-- `price_snapshots` are never rewritten, so the append-only invariant holds and
-- an action stays reversible by deleting its row.
--
-- `kind = 'NONE'` records a discontinuity that was investigated and found NOT to
-- be a corporate action — a real one-day crash looks exactly like a split in a
-- price series, and without somewhere to write down "we checked, it wasn't one"
-- the heuristic scanner re-reports it forever. Such rows carry ratio 1, which is
-- the identity factor, so they can never adjust anything.
--
-- `ratio` is new shares per old share: a 2:1 split is 2, a 1:10 reverse is 0.1.
--
-- The two flags are independent because they answer different questions:
--   adjust_prices — was the *quote series* rebased at ex_date? Almost always
--                   TRUE; this is what stops TWR reading a 2:1 split as -50%.
--   adjust_lots   — were extra shares actually credited to the account? FALSE
--                   when the recorded quantity is already correct (e.g. a
--                   broker that reports post-split units, or a position whose
--                   entry was made after the fact).
-- Setting adjust_lots wrongly silently corrupts cost basis, so it defaults to
-- TRUE and must be turned off deliberately, with a note saying why.
CREATE TABLE IF NOT EXISTS corporate_actions (
  id            BIGSERIAL PRIMARY KEY,
  symbol        TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  kind          TEXT NOT NULL CHECK (kind IN ('SPLIT','REVERSE_SPLIT','NONE')),
  ex_date       DATE NOT NULL,
  -- Exact instant the action took effect (the ex-date's regular-session open).
  -- Optional: readers of *daily* series only need ex_date. Raw-timestamp
  -- readers (drawdown, portfolio value history) need this, or they mis-classify
  -- the pre-open snapshots on the ex-date itself, which still carry the prior
  -- close. Falls back to reporting-local midnight when NULL.
  ex_ts         TIMESTAMPTZ,
  ratio         NUMERIC(20,8) NOT NULL CHECK (ratio > 0),
  adjust_prices BOOLEAN NOT NULL DEFAULT TRUE,
  adjust_lots   BOOLEAN NOT NULL DEFAULT TRUE,
  source        TEXT NOT NULL DEFAULT 'manual',
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- NaN satisfies `ratio > 0` (it sorts above every number), and the kind has
  -- to agree with the ratio: a SPLIT adds shares, a REVERSE_SPLIT removes
  -- them, NONE is the identity (audit D-4, migration 005).
  CONSTRAINT corporate_actions_ratio_not_nan CHECK (ratio <> 'NaN'::numeric),
  CONSTRAINT corporate_actions_kind_ratio CHECK (
    (kind = 'SPLIT' AND ratio > 1) OR (kind = 'REVERSE_SPLIT' AND ratio < 1)
    OR (kind = 'NONE' AND ratio = 1))
);

-- One action per (symbol, kind, ex_date). Ratio is deliberately NOT in the key:
-- a correction should update the existing row, not insert a rival one.
CREATE UNIQUE INDEX IF NOT EXISTS corporate_actions_dedupe_idx
  ON corporate_actions(symbol, kind, ex_date);

-- Widen the kind constraint on databases created before 'NONE' existed. Only
-- when it is actually narrower: dropping and re-adding it on every run took an
-- exclusive lock and re-validated the table each time (audit D-3).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'corporate_actions_kind_check'
                   AND pg_get_constraintdef(oid) LIKE '%NONE%') THEN
    ALTER TABLE corporate_actions DROP CONSTRAINT IF EXISTS corporate_actions_kind_check;
    ALTER TABLE corporate_actions ADD CONSTRAINT corporate_actions_kind_check
      CHECK (kind IN ('SPLIT','REVERSE_SPLIT','NONE'));
  END IF;
END $$;

-- Was this row confirmed against an authoritative source, or is it a machine
-- guess awaiting review? The collector records vendor-reported splits with
-- reviewed=FALSE, and such a row APPLIES TO NOTHING — neither prices nor lots —
-- until a human confirms it (check_splits.py --approve; audit F-3). Rows
-- entered by hand (source = 'manual') are the operator's own record and apply
-- as entered. See corporate_actions_in_effect below.
ALTER TABLE corporate_actions ADD COLUMN IF NOT EXISTS reviewed BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS snapshot_runs (
  id              BIGSERIAL PRIMARY KEY,
  ts_start        TIMESTAMPTZ NOT NULL,
  ts_end          TIMESTAMPTZ,
  status          TEXT NOT NULL CHECK (status IN ('running','ok','partial','failed')),
  symbols_total   INTEGER,
  symbols_ok      INTEGER,
  symbols_failed  INTEGER,
  error           TEXT
);

CREATE INDEX IF NOT EXISTS snapshot_runs_ts_idx ON snapshot_runs(ts_start DESC);

-- Advisor: append-only chat history + structured briefs
CREATE TABLE IF NOT EXISTS chat_log (
  id              BIGSERIAL PRIMARY KEY,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content         TEXT NOT NULL,
  conversation_id TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS chat_log_recent_idx
  ON chat_log(conversation_id, ts DESC);

CREATE TABLE IF NOT EXISTS advisor_briefs (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind        TEXT NOT NULL CHECK (kind IN ('morning','eod','adhoc')),
  total_value NUMERIC(20,2),
  payload     JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS advisor_briefs_ts_idx
  ON advisor_briefs(ts DESC);

-- Runtime settings (added in migrations/001_settings.sql). One row per
-- setting; read through app/settings.py with precedence DB → env → default.
-- Secrets never live here — they stay in .env (the dashboard has no auth).
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Persisted price alerts (added in migrations/006_price_alerts_and_lot_audit.sql).
-- Written from Manage → Alerts; deactivated, never deleted, from the page.
CREATE TABLE IF NOT EXISTS price_alerts (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL REFERENCES instruments(symbol),
  direction   TEXT NOT NULL CHECK (direction IN ('above', 'below')),
  level       NUMERIC(20,8) NOT NULL CHECK (level > 0 AND level <> 'NaN'),
  note        TEXT,
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS price_alerts_active_symbol_idx
  ON price_alerts (symbol) WHERE active;

-- Audit trail for lots deleted from the dashboard (added in migration 006):
-- the deleted row in full, when, and why — written in the same statement as
-- the DELETE (app/native_writes.py::delete_lot).
CREATE TABLE IF NOT EXISTS lot_deletions (
  id          BIGSERIAL PRIMARY KEY,
  lot_id      BIGINT NOT NULL,
  lot         JSONB NOT NULL,
  reason      TEXT NOT NULL CHECK (btrim(reason) <> ''),
  deleted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lot_deletions_deleted_at_idx
  ON lot_deletions (deleted_at DESC);
-- Migration bookkeeping, written by app/apply_schema.py: which file under
-- sql/migrations/ was applied, when, and the sha256 of its text (line endings
-- normalised). A file whose checksum changes after it was applied is reported
-- loudly on the next run (audit D-3). Applying the files by hand with psql
-- still works — they stay idempotent — it just leaves this table empty, and
-- the next apply_schema.py run applies and records them.
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename    TEXT PRIMARY KEY,
  checksum    TEXT NOT NULL,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ───────────── corporate actions in effect, and the one SQL net quantity ─────────────
--
-- Everything below is CREATE OR REPLACE, so it reaches an existing database
-- whenever this file is re-applied. Each mirrors Python and a database test
-- (app/tests/test_ledger_sql.py) holds the two to the same answers:
--   corporate_actions_in_effect  ⇔  corporate_actions.effective_actions()
--   lot_open_quantities()        ⇔  holdings.net_quantities(ledger_inputs.load())

-- A recorded action takes effect when it is reviewed or was entered by hand,
-- and is not a second record of an event a better-ranked row describes (same
-- symbol, kind and ratio within 7 days — corporate_actions.DUPLICATE_WINDOW_DAYS;
-- ranked reviewed, then manual, then earlier ex-date). Audit F-3, F-16.
CREATE OR REPLACE VIEW corporate_actions_in_effect AS
SELECT a.*
FROM corporate_actions a
WHERE (a.reviewed OR a.source = 'manual')
  AND NOT EXISTS (
    SELECT 1 FROM corporate_actions b
    WHERE b.id <> a.id
      AND (b.reviewed OR b.source = 'manual')
      AND b.symbol = a.symbol AND b.kind = a.kind AND b.ratio = a.ratio
      AND a.ratio <> 1
      AND abs(b.ex_date - a.ex_date) <= 7
      AND (CASE WHEN b.reviewed THEN 0 ELSE 1 END,
           CASE WHEN b.source = 'manual' THEN 0 ELSE 1 END,
           b.ex_date)
        < (CASE WHEN a.reviewed THEN 0 ELSE 1 END,
           CASE WHEN a.source = 'manual' THEN 0 ELSE 1 END,
           a.ex_date)
  );

-- Exact product of split ratios (numeric_mul is numeric * numeric).
CREATE OR REPLACE AGGREGATE pdb_product(numeric) (
  SFUNC = numeric_mul, STYPE = numeric, INITCOND = '1'
);

-- Open quantity per (symbol, account) by the engines' rules (audit R-1, F-1):
-- lots restated into post-split units by the actions in effect (dated on or
-- before as_of, when given); processed in ledger_core.processing_key order —
-- date, every BUY before any SELL that day, then id; and a SELL truncated at
-- the open quantity, as FIFO does (shorts are unsupported). The truncated walk
-- q_n = max(0, q_{n-1} + d_n) is computed in closed form as
-- S_n - min(0, min_k S_k) over the plain running sums S_k. Closed streams are
-- omitted. Sum by symbol for the per-symbol figure:
--   SELECT symbol, SUM(quantity) FROM lot_open_quantities() GROUP BY symbol
CREATE OR REPLACE FUNCTION lot_open_quantities(as_of date DEFAULT NULL)
RETURNS TABLE (symbol text, account text, quantity numeric)
LANGUAGE sql STABLE AS $fn$
  WITH acts AS (
    SELECT e.symbol, e.ex_date, e.ratio
    FROM corporate_actions_in_effect e
    WHERE e.adjust_lots AND (as_of IS NULL OR e.ex_date <= as_of)
  ), prepared AS (
    SELECT l.id, l.symbol, l.account, l.trade_date, l.side,
           l.quantity * COALESCE(
             (SELECT pdb_product(a.ratio) FROM acts a
              WHERE a.symbol = l.symbol AND l.trade_date < a.ex_date), 1) AS qty
    FROM lots l
    WHERE as_of IS NULL OR l.trade_date <= as_of
  ), walked AS (
    SELECT p.symbol, p.account,
           CASE WHEN p.side = 'BUY' THEN p.qty ELSE -p.qty END AS delta,
           SUM(CASE WHEN p.side = 'BUY' THEN p.qty ELSE -p.qty END) OVER (
             PARTITION BY p.symbol, p.account
             ORDER BY p.trade_date, (p.side = 'SELL'), p.id
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running
    FROM prepared p
  )
  SELECT w.symbol, w.account, SUM(w.delta) - LEAST(0, MIN(w.running))
  FROM walked w
  GROUP BY w.symbol, w.account
  HAVING SUM(w.delta) - LEAST(0, MIN(w.running)) <> 0
$fn$;

-- One split recorded twice is applied twice; refuse the second record at the
-- door (audit F-16). Same window and matching as the view above. NONE rows
-- (ratio 1) are inert and exempt.
CREATE OR REPLACE FUNCTION corporate_actions_refuse_near_duplicate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
  other RECORD;
BEGIN
  IF NEW.ratio = 1 THEN
    RETURN NEW;
  END IF;
  SELECT c.id, c.ex_date INTO other
  FROM corporate_actions c
  WHERE c.id <> NEW.id AND c.symbol = NEW.symbol AND c.kind = NEW.kind
    AND c.ratio = NEW.ratio AND abs(c.ex_date - NEW.ex_date) <= 7
  LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION '% % % on % repeats the corporate action on % (id %): one split recorded twice would be applied twice',
      NEW.symbol, NEW.kind, NEW.ratio, NEW.ex_date, other.ex_date, other.id
      USING ERRCODE = 'unique_violation',
            HINT = 'Correct the existing row instead of adding another.';
  END IF;
  RETURN NEW;
END
$fn$;

DROP TRIGGER IF EXISTS corporate_actions_near_duplicate ON corporate_actions;
CREATE TRIGGER corporate_actions_near_duplicate
  BEFORE INSERT OR UPDATE OF symbol, kind, ex_date, ratio ON corporate_actions
  FOR EACH ROW EXECUTE FUNCTION corporate_actions_refuse_near_duplicate();

-- A migration that adds a CHECK to a table with existing rows adds it NOT
-- VALID (enforced for new rows at once) and then validates; when an existing
-- row fails, the constraint is left unvalidated with a warning (migrations
-- 004, 005). Migrations run once, so the retry lives here, in the file every
-- upgrade re-applies: each run tries again, and keeps saying which constraint
-- still has offending rows until they are fixed.
DO $$
DECLARE
  c RECORD;
BEGIN
  FOR c IN
    SELECT conrelid::regclass AS tbl, conname
    FROM pg_constraint
    WHERE contype = 'c' AND NOT convalidated
      AND connamespace = current_schema()::regnamespace
  LOOP
    BEGIN
      EXECUTE format('ALTER TABLE %s VALIDATE CONSTRAINT %I', c.tbl, c.conname);
      RAISE NOTICE 'constraint % on % is now validated', c.conname, c.tbl;
    EXCEPTION WHEN check_violation THEN
      RAISE WARNING 'constraint % on % is enforced for new rows but existing rows still violate it; fix them and re-run apply_schema.py',
        c.conname, c.tbl;
    END;
  END LOOP;
END $$;

COMMIT;
