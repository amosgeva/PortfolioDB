# Importing history from CSV

`app/import_csv_history.py` bulk-loads trades **and** historical prices from CSV
files. It was written against Yahoo Finance portfolio exports, and any broker
export works once you rename a few columns.

There are no broker-specific adapters. This is on purpose: one documented column
set you map onto is easier to reason about than a dozen half-maintained parsers.

```bash
# Always dry-run first — it parses and reports without writing anything
docker compose run --rm dashboard python app/import_csv_history.py \
  --dir /app/import --default-account IBKR --dry-run

# Then for real
docker compose run --rm dashboard python app/import_csv_history.py \
  --dir /app/import --default-account IBKR
```

To make your files visible inside the container, mount them in your override:

```yaml
services:
  dashboard:
    volumes:
      - ./import:/app/import:ro
```

## Columns

One row per holding, as portfolio exports normally look. Nothing is
case-insensitive — header names must match exactly.

| Column | Required for | Meaning |
|---|---|---|
| `Symbol` | everything | Ticker. Uppercased on import. |
| `Trade Date` | lots | Purchase date as `YYYYMMDD` (e.g. `20260213`). |
| `Purchase Price` | lots | Price per share, in the instrument's currency. |
| `Quantity` | lots | Shares bought. Must be > 0. |
| `Commission` | lots (optional) | Fees for the trade; blank counts as 0. |
| `Comment` | lots (optional) | Free text. Also used for account mapping — see below. |
| `Date` | price snapshots | File's as-of date, e.g. `2026/02/20`. |
| `Time` | price snapshots | File's as-of time with a zone token, e.g. `16:00 EST`. |
| `Current Price` | price snapshots | Last price at that timestamp. |

Two independent things are imported:

- **A lot**, from any row that has `Trade Date` + `Purchase Price` + `Quantity`.
  Rows without all three are skipped for lots — which is what you want for a
  holding you never traded through this account.
- **A price snapshot** per row with a `Current Price`, stamped with the
  timestamp built from that file's `Date` + `Time`. **Each file is one instant**,
  so a directory of daily exports becomes a daily price series.

`Date`/`Time` are read as US-Eastern when the zone token is `EST`/`EDT`, and
converted to UTC before storage.

## Accounts

The ledger is per-account, and CSV exports rarely carry an account column, so:

- `--default-account NAME` (required) is used for every lot, unless
- `--tagged-accounts A,B` is given, in which case a lot whose `Comment`
  mentions one of those names (case-insensitive substring) is assigned to it.

```bash
# lots whose Comment mentions "IBKR" land in IBKR; everything else in BrokerA
--default-account BrokerA --tagged-accounts IBKR
```

Accounts are free-form labels — call them whatever your statements call them.

## Duplicates

Safe to re-run. Lots collide on the unique index
`(symbol, account, trade_date, quantity, price)` and snapshots on the primary
key `(symbol, ts)`; both insert with `ON CONFLICT DO NOTHING`.

The consequence worth knowing: **two genuinely separate fills of the same size
at the same price on the same day in the same account import as one lot.** It's
the same trade-off the manual `add-lot` path has. Split the quantity or add a
distinguishing `Comment` if you need both rows.

## A sample file

[`examples/sample_portfolio.csv`](examples/sample_portfolio.csv) is a two-symbol
export in the expected shape — copy its header row and fill in your own data:

```csv
Symbol,Trade Date,Purchase Price,Quantity,Commission,Comment,Date,Time,Current Price
AAPL,20260213,184.25,40,1.25,opening position,2026/05/20,16:00 EDT,195.10
VOO,20260220,505.10,12,0.00,IBKR core sleeve,2026/05/20,16:00 EDT,518.44
```

## Afterwards

```bash
make positions        # does the ledger look right?
```

Check a couple of positions against your broker before trusting the numbers —
cost basis is recomputed from the lots you just imported, so an import error
shows up as a wrong average cost rather than an error message. If something is
off, remember the ledger is append-only: fix it with a corrective entry (or
delete the specific lot row from the Manage page) rather than editing history in
place.

If your export has no `Trade Date` at all — common when a broker only gives you
current holdings — you have positions but no cost basis. Enter those few lots by
hand with `make add-lot` instead; guessing a purchase price silently corrupts
every P&L number downstream.
