# Scheduled jobs

Two services run the scheduled jobs with
[supercronic](https://github.com/aptible/supercronic) (cron built for
containers: logs to stdout, no root, refuses to start on a bad crontab), and
both start with the rest of the stack:

```bash
docker compose up -d                  # postgres + dashboard + scheduler + scheduler-llm
docker compose logs -f scheduler      # the price collector
docker compose logs -f scheduler-llm  # the brief and the enrichment
```

| Service | Job | Schedule | Notes |
|---|---|---|---|
| `scheduler` | `snapshot_prices.py` | every 5 min | Exits immediately outside the collector window — the cron line does not know the window |
| `scheduler` | `snapshot_prices.py --benchmarks` | every 15 min, Sun–Fri | The Markets strip (index futures, volatility). **Deliberately ignores the collector window**: futures quote while your market is shut, which is the only reason to collect them. Records no row in `snapshot_runs`, so Data Health and the MCP cutoff keep meaning "the portfolio's prices" |
| `scheduler-llm` | `advisor.py brief` | 07:00 Mon–Fri | Needs an LLM API key; without one it logs and moves on |
| `scheduler-llm` | `fd_weekly_enrichment.py` | 06:00 Sat | Needs `FINANCIAL_DATASETS_API_KEY`; cache-first, so cheap |

Times are read in the container's timezone (`PORTFOLIODB_TZ`, default UTC).

### Why two schedulers

The collector fetches and parses third-party web pages every five minutes; the
brief and the enrichment hold API keys that cost money and identify you. Before
1.8.0 they shared one container, and so one environment: a flaw in a quote
parser ran next to the LLM and Financial Datasets keys. Now they are separate
containers built from the same image (`docker/crontab` and
`docker/crontab-llm`):

- **`scheduler`** gets no `.env` at all. Compose hands it an explicit allowlist —
  the database login, the timezone, the collector window and the collector's
  own tunables (`PORTFOLIODB_MARKET_OVERVIEW`, `PORTFOLIODB_COLLECTOR_DEADLINE_S`,
  `PORTFOLIODB_QUOTE_MAX_AGE_MIN`, `PORTFOLIODB_CONNECT_TIMEOUT`). A key you add
  to `.env` later does not reach it either. If you need something else there
  (an HTTP proxy, say), add it to the service's `environment:` in
  `docker-compose.override.yml`.
- **`scheduler-llm`** gets `.env` like the dashboard, minus the secrets that
  belong to other processes (the MCP token and role, pgAdmin's login, the
  dashboard password).

A per-job wrapper that scrubbed variables inside one container would not have
been a boundary: every job in a container runs as the same user as supercronic,
which can read its environment from `/proc/1/environ`.

## The collector window

**Change the window in Settings, not in the crontab.** The cron line ticks
every five minutes and `app/market_window.py` decides whether there is work to
do, so the window is one setting read by the collector, the dashboard's
freshness warning, and the Settings page alike:

| Setting | Env var | Default |
|---|---|---|
| Window start | `PORTFOLIODB_MARKET_START` | `13:30` |
| Window end | `PORTFOLIODB_MARKET_END` | `21:15` |
| Days | `PORTFOLIODB_MARKET_WEEK` | `1-5` (Mon–Fri) |

Times are `HH:MM` in the reporting timezone, both ends inclusive, and the
window may wrap past midnight (`22:00`–`04:00`) — which is what you need when
your zone is far from the market you follow.

The defaults cover US regular hours plus a post-close tail, expressed in UTC.
Reading in `Asia/Jerusalem`? `15:15`–`23:15` is the same window in local terms.

To collect once regardless of the window:

```bash
docker compose run --rm dashboard python app/snapshot_prices.py --ignore-window
```

## Running only some jobs

Edit `docker/crontab` (collector) or `docker/crontab-llm` (brief, enrichment)
and rebuild, or comment a line out and restart the service. The jobs are
independent: the snapshot collector is the only one most installs need, and the
other two no-op without their API keys — so an install with no keys can stop
the keyed scheduler altogether (`docker compose stop scheduler-llm`, or
`scale: 0` for it in `docker-compose.override.yml`). Keep keyed jobs out of
`docker/crontab`: the collector's container has no keys by design.

## Upgrading an existing install — read this once

The collector used to be started by a host scheduler that applied its own
window, and `snapshot_prices.py` collected whenever it was invoked. It now
enforces the window itself, so **after upgrading, the effective window is the
narrower of your host scheduler's rule and the setting above** — and if you
never set the window, that is the default `13:30`–`21:15`.

That default is written for UTC. If your reporting timezone is not UTC, the
same numbers land somewhere else on your clock and can silently cut the end of
your trading day, taking the closing snapshot with it. Set the window
explicitly the first time you upgrade:

```env
# e.g. the equivalent of a weekday 15:15–23:15 Asia/Jerusalem window
PORTFOLIODB_MARKET_START=15:15
PORTFOLIODB_MARKET_END=23:15
PORTFOLIODB_MARKET_WEEK=1-5
```

Check what is actually in force before trusting it:

```bash
docker compose run --rm dashboard sh -c 'cd /app/app && python -c "
import market_window; print(market_window.describe())"'
```

## Migrating from a host scheduler

If you previously ran the collector from a host scheduler (Windows Task
Scheduler, systemd timer, crontab), run both for a few days and compare — every
run lands in `snapshot_runs`, so duplicate or missing collection is visible:

```sql
SELECT date_trunc('hour', ts_start) AS hour, count(*), min(ts_start), max(ts_start)
FROM snapshot_runs
WHERE ts_start > now() - interval '3 days'
GROUP BY 1 ORDER BY 1 DESC;
```

Snapshots are keyed `(symbol, ts)` and inserted `ON CONFLICT DO NOTHING`, and
each run stamps its own start time, so an overlap costs a few duplicate API
calls — it cannot corrupt the series. Disable the host job once you trust the
container.
