# PortfolioDB app (v0.1)

## Prereqs
- Postgres container running (see `docker-compose.yml` at the repo root)
- Python deps:
  - `pip install psycopg2-binary yfinance pandas python-dotenv`

## Configure
Set env var `PORTFOLIODB_PASSWORD` in the shell before running.

(Optionally) copy `.env.template` to `.env` and load it in your shell.

## Commands
### Add a BUY lot
```powershell
$env:PORTFOLIODB_PASSWORD = '<secret>'
python add_lot.py --symbol NVDA --account IBKR --trade-date 2026-02-13 --side BUY --qty 1 --price 184.00
```

### Add a SELL lot
```powershell
$env:PORTFOLIODB_PASSWORD = '<secret>'
python sell_lot.py --symbol NVDA --account IBKR --trade-date 2026-03-01 --qty 1 --price 200.00 --fees 2.50
```

### Show positions (FIFO)
```powershell
$env:PORTFOLIODB_PASSWORD = '<secret>'
python positions.py
```

### Collect a snapshot
```powershell
$env:PORTFOLIODB_PASSWORD = '<secret>'
python snapshot_prices.py
```

## GUI (MVP)
Install streamlit:
```powershell
pip install -r requirements.txt
```

Run (set browser email prompt off):
```powershell
$env:PORTFOLIODB_PASSWORD = '<secret>'
$env:STREAMLIT_SERVER_HEADLESS = 'true'
streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Then open the URL it prints (LAN-only).

## Cash (manual for now)
We don’t pull cash automatically yet. You can store a manual cash snapshot:
```powershell
$env:PORTFOLIODB_PASSWORD = '<secret>'
python set_cash.py --cash 1000 --account IBKR --note "manual update"
```

## Next
- Charts per symbol (time series)
- Snapshot status + run logs
- Automate cash snapshots from IBKR later (optional)
