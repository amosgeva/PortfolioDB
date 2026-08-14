-- 002: benchmark instruments for the market overview strip.
--
-- A benchmark is a symbol collected for context, never held: index futures,
-- volatility, whatever the operator wants to see before the open. The flag is
-- what keeps them out of the portfolio: they have no lots, so they cannot reach
-- the P&L engines, and this column keeps them out of the watchlist rail and out
-- of Data Health's per-symbol scope too.
--
-- The set of symbols is a setting (`market_overview_symbols`), not a table:
-- app/market_overview.py syncs this flag to match it on every collection, so the
-- setting stays the single source of truth and removing a symbol stops both the
-- collection and the display.

BEGIN;

ALTER TABLE instruments
  ADD COLUMN IF NOT EXISTS benchmark BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index: the collector asks "which symbols are benchmarks" on every
-- benchmark run, and there will be a handful of them among all instruments.
CREATE INDEX IF NOT EXISTS idx_instruments_benchmark
  ON instruments(symbol) WHERE benchmark;

COMMIT;
