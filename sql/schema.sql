-- PortfolioDB schema v0.3
-- Append-only snapshots, lot-based cost basis, FIFO P&L

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
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
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
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Prevent accidental duplicate lot inserts during imports.
-- `side` MUST be part of the key: without it a BUY and a SELL sharing
-- symbol/account/date/qty/price collide and ON CONFLICT DO NOTHING silently
-- drops the second trade.
CREATE UNIQUE INDEX IF NOT EXISTS lots_dedupe_idx
  ON lots(symbol, COALESCE(account,''), side, trade_date, quantity, price);

CREATE TABLE IF NOT EXISTS price_snapshots (
  ts            TIMESTAMPTZ NOT NULL,
  symbol        TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  last_price    NUMERIC(20,8) NOT NULL CHECK (last_price >= 0),
  bid           NUMERIC(20,8) CHECK (bid >= 0),
  ask           NUMERIC(20,8) CHECK (ask >= 0),
  source        TEXT NOT NULL DEFAULT 'yfinance',
  session       TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
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
  PRIMARY KEY (account, ts)
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
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Best-effort dedupe guard for backfill imports (mirrors lots_dedupe_idx).
-- `kind` is part of the key so e.g. a DIVIDEND and an INTEREST payment with
-- the same symbol/account/date/amount don't collide.
CREATE UNIQUE INDEX IF NOT EXISTS income_dedupe_idx
  ON income(symbol, COALESCE(account,''), kind, pay_date, amount);

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
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One action per (symbol, kind, ex_date). Ratio is deliberately NOT in the key:
-- a correction should update the existing row, not insert a rival one.
CREATE UNIQUE INDEX IF NOT EXISTS corporate_actions_dedupe_idx
  ON corporate_actions(symbol, kind, ex_date);

-- Widen the kind constraint on databases created before 'NONE' existed.
ALTER TABLE corporate_actions DROP CONSTRAINT IF EXISTS corporate_actions_kind_check;
ALTER TABLE corporate_actions ADD CONSTRAINT corporate_actions_kind_check
  CHECK (kind IN ('SPLIT','REVERSE_SPLIT','NONE'));

-- Was this row confirmed against an authoritative source, or is it a machine
-- guess awaiting review? The collector records vendor-reported splits with
-- reviewed=FALSE; a human sets it TRUE after checking. Nothing keys off this
-- yet beyond the data-quality report, but an unreviewed row adjusting cost
-- basis is exactly the mistake worth surfacing.
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

COMMIT;
