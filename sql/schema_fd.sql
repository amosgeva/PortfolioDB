-- PortfolioDB Financial Datasets enrichment schema
-- Idempotent: safe to re-run. Mirrors what fd_weekly_enrichment.py persists from the
-- file cache under cache/financialdatasets/<SYMBOL>/<section>.json.
--
-- Conventions:
--   * Every row carries fetched_at so the dashboard/reports can render staleness.
--   * Hot columns (revenue, net_income, P/E, etc.) are extracted for cheap SQL filtering.
--   * raw JSONB always carries the full upstream payload, so we never lose fields
--     when FD changes shape.
--   * symbol always references instruments(symbol) — populate it before persisting.

BEGIN;

-- One row per symbol, overwritten by upsert on the latest /company/facts response.
CREATE TABLE IF NOT EXISTS fd_company_facts (
  symbol               TEXT PRIMARY KEY REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  name                 TEXT,
  sector               TEXT,
  industry             TEXT,
  exchange             TEXT,
  category             TEXT,
  cik                  TEXT,
  location             TEXT,
  website              TEXT,
  is_active            BOOLEAN,
  raw                  JSONB NOT NULL,
  fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (symbol, statement_type, accession_number). The same fiscal period can
-- appear across multiple filings (8-K preliminary, then 10-Q/10-K), so accession is
-- the natural disambiguator.
CREATE TABLE IF NOT EXISTS fd_financial_statements (
  symbol               TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  statement_type       TEXT NOT NULL CHECK (statement_type IN ('income_statement','balance_sheet','cash_flow_statement')),
  accession_number     TEXT NOT NULL,
  period               TEXT NOT NULL,                 -- 'quarterly' | 'annual' | 'ttm'
  report_period        DATE,
  fiscal_period        TEXT,
  currency             TEXT,
  revenue              NUMERIC(24,4),                 -- income_statement only
  net_income           NUMERIC(24,4),                 -- income_statement / cash_flow
  free_cash_flow       NUMERIC(24,4),                 -- cash_flow only
  total_assets         NUMERIC(24,4),                 -- balance_sheet only
  shareholders_equity  NUMERIC(24,4),                 -- balance_sheet only
  raw                  JSONB NOT NULL,
  fetched_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, statement_type, accession_number)
);

CREATE INDEX IF NOT EXISTS fd_financial_statements_period_idx
  ON fd_financial_statements(symbol, statement_type, report_period DESC);

-- Latest /financial-metrics/snapshot per symbol.
CREATE TABLE IF NOT EXISTS fd_financial_metrics (
  symbol                            TEXT PRIMARY KEY REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  market_cap                        NUMERIC(24,4),
  enterprise_value                  NUMERIC(24,4),
  pe_ratio                          NUMERIC(20,8),
  ps_ratio                          NUMERIC(20,8),
  pb_ratio                          NUMERIC(20,8),
  ev_ebitda                         NUMERIC(20,8),
  ev_revenue                        NUMERIC(20,8),
  peg_ratio                         NUMERIC(20,8),
  earnings_per_share                NUMERIC(20,8),
  book_value_per_share              NUMERIC(20,8),
  gross_margin                      NUMERIC(20,8),
  operating_margin                  NUMERIC(20,8),
  net_margin                        NUMERIC(20,8),
  return_on_equity                  NUMERIC(20,8),
  return_on_assets                  NUMERIC(20,8),
  return_on_invested_capital        NUMERIC(20,8),
  debt_to_equity                    NUMERIC(20,8),
  debt_to_assets                    NUMERIC(20,8),
  current_ratio                     NUMERIC(20,8),
  quick_ratio                       NUMERIC(20,8),
  free_cash_flow_yield              NUMERIC(20,8),
  payout_ratio                      NUMERIC(20,8),
  revenue_growth                    NUMERIC(20,8),
  earnings_growth                   NUMERIC(20,8),
  raw                               JSONB NOT NULL,
  fetched_at                        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- /earnings — one row per filing (accession). Same period may appear across multiple filings.
CREATE TABLE IF NOT EXISTS fd_earnings (
  symbol                       TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  accession_number             TEXT NOT NULL,
  fiscal_period                TEXT,
  report_period                DATE,
  filing_date                  DATE,
  filing_datetime              TIMESTAMPTZ,
  source_type                  TEXT,
  currency                     TEXT,
  eps_actual                   NUMERIC(20,8),
  eps_estimate                 NUMERIC(20,8),
  eps_surprise                 TEXT,                  -- 'BEAT' | 'MISS' | 'INLINE'
  eps_surprise_pct             NUMERIC(20,8),
  revenue_actual               NUMERIC(24,4),
  revenue_estimate             NUMERIC(24,4),
  revenue_surprise             TEXT,
  revenue_surprise_pct         NUMERIC(20,8),
  raw                          JSONB NOT NULL,
  fetched_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, accession_number)
);

CREATE INDEX IF NOT EXISTS fd_earnings_period_idx
  ON fd_earnings(symbol, report_period DESC, filing_date DESC);

-- /filings list (recent SEC filings).
CREATE TABLE IF NOT EXISTS fd_filings (
  symbol                       TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  accession_number             TEXT NOT NULL,
  filing_type                  TEXT,
  filing_date                  DATE,
  report_date                  DATE,
  url                          TEXT,
  raw                          JSONB NOT NULL,
  fetched_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, accession_number)
);

CREATE INDEX IF NOT EXISTS fd_filings_date_idx
  ON fd_filings(symbol, filing_date DESC);

-- /insider-trades. No unique upstream key, so we PK on a deterministic SHA-256 of the
-- identifying fields (symbol + transaction_date + name + transaction_type + shares + price + security_title).
CREATE TABLE IF NOT EXISTS fd_insider_trades (
  symbol                              TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  payload_hash                        TEXT NOT NULL,
  filing_date                         DATE,
  transaction_date                    DATE,
  name                                TEXT,
  title                               TEXT,
  is_board_director                   BOOLEAN,
  security_title                      TEXT,
  transaction_type                    TEXT,
  transaction_shares                  NUMERIC(24,8),
  transaction_price_per_share         NUMERIC(20,8),
  transaction_value                   NUMERIC(24,4),
  shares_owned_after_transaction      NUMERIC(24,8),
  raw                                 JSONB NOT NULL,
  fetched_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, payload_hash)
);

CREATE INDEX IF NOT EXISTS fd_insider_trades_date_idx
  ON fd_insider_trades(symbol, transaction_date DESC, filing_date DESC);

-- /institutional-ownership.
CREATE TABLE IF NOT EXISTS fd_institutional_ownership (
  symbol                       TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  investor                     TEXT NOT NULL,
  report_period                DATE NOT NULL,
  security_type                TEXT NOT NULL DEFAULT 'common_stock',
  shares                       NUMERIC(24,8),
  market_value                 NUMERIC(24,4),
  price                        NUMERIC(20,8),
  raw                          JSONB NOT NULL,
  fetched_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, investor, report_period, security_type)
);

CREATE INDEX IF NOT EXISTS fd_institutional_ownership_period_idx
  ON fd_institutional_ownership(symbol, report_period DESC, market_value DESC);

-- /news. URL is the natural key; titles can be near-duplicates.
CREATE TABLE IF NOT EXISTS fd_news (
  symbol                       TEXT NOT NULL REFERENCES instruments(symbol) ON UPDATE CASCADE ON DELETE RESTRICT,
  url                          TEXT NOT NULL,
  published_at                 TIMESTAMPTZ,
  source                       TEXT,
  title                        TEXT,
  sentiment                    TEXT,
  raw                          JSONB NOT NULL,
  fetched_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, url)
);

CREATE INDEX IF NOT EXISTS fd_news_published_idx
  ON fd_news(symbol, published_at DESC);

COMMIT;
