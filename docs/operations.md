# Running it: backups, upgrades, health

Commands are written as `make` targets throughout. That is a macOS/Linux
shorthand — every one of them wraps a single `docker compose` invocation, and
[commands.md](commands.md) lists both forms side by side. On Windows use the
compose form or `pdb.ps1`; the two exceptions where the translation is *not*
mechanical are backup and restore, called out below.

## Backups

The whole point of this project is that the data outlives the software, so this
is the section that matters most.

```bash
make backup                       # -> backups/portfoliodb-YYYYmmdd-HHMMSS.sql.gz
make backup ARGS=/mnt/nas/pdb     # somewhere else
```
```powershell
.\pdb.ps1 backup                  # Windows — same output, same layout
.\pdb.ps1 backup D:\nas\pdb
```

That's a `pg_dump` of the entire database — schema, ledger, price history,
settings, briefs — gzipped. It runs against the live container; no downtime, and
no need to stop the scheduler.

Both runners write to `backups/` by default and take any other destination as an
argument. **If you do it by hand instead, read
[commands.md](commands.md#backup) first** — the POSIX one-liner cannot be reused
verbatim on Windows, and the naive translation produces a corrupt gzip that only
fails on the day you try to restore it.

**A backup on the same machine is not a backup.** Copy it off:

```bash
make backup && rsync -a backups/ you@nas:/backups/portfoliodb/
```

A weekly cron entry on the host is enough for most people:

```cron
0 3 * * 0  cd /path/to/PortfolioDB && make backup && rsync -a backups/ you@nas:/backups/portfoliodb/
```

On Windows, a Scheduled Task doing the same thing. Register it once:

```powershell
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\path\to\PortfolioDB\pdb.ps1 backup D:\nas\pdb' `
  -WorkingDirectory 'C:\path\to\PortfolioDB'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3am
Register-ScheduledTask -TaskName 'PortfolioDB Backup' -Action $action -Trigger $trigger
```

Set `-WorkingDirectory` to the folder holding `docker-compose.yml`; `docker
compose` resolves the project from the working directory, and a task that starts
anywhere else will not find the stack. Avoid a colon in the task name.

Your investor one-pager is inside the dump when you saved it through the
dashboard; it is a separate file only if you mounted `philosophy.md` instead.
Keep at least one copy of `.env` (and that file, if you use it) alongside the
dumps —
neither is in the database, and neither is in git. Restoring a dump without the
password in `.env` gets you a database you can't open.

### Restoring

```bash
make restore ARGS=backups/portfoliodb-20260813-030000.sql.gz
```
```powershell
.\pdb.ps1 restore .\backups\portfoliodb-20260813-030000.sql.gz
```

It refuses to run unless the target database is empty, because restoring over a
live ledger is how you lose data twice. To rebuild from scratch:

```bash
make down
docker volume rm portfoliodb_pgdata      # destroys the current database
make up
make restore ARGS=backups/portfoliodb-20260813-030000.sql.gz
```

Without a runner, [commands.md](commands.md#restore) has the `docker compose`
form for both platforms, including how to check the target is empty first.

Test this once, on purpose, before you need it. An untested backup is a
hypothesis.

## Upgrading

```bash
git pull
make build          # rebuild the app image
make schema         # apply any new migrations (idempotent)
make restart
```

Take a backup first. `make schema` is safe to re-run: `schema.sql` is
`IF NOT EXISTS` throughout and each file in `sql/migrations/` is idempotent.

Two things to check after an upgrade:

- **The collector window** — `docs/scheduling.md` explains why an upgrade can
  narrow it. Print what's in force:
  `docker compose run --rm dashboard sh -c 'cd /app/app && python -c "import market_window; print(market_window.describe())"'`
- **The release notes** for anything about `.env` keys, if the version changed
  more than patch-level.

## Is it working?

```bash
make ps                    # services up?
make logs ARGS=scheduler   # is the collector running and skipping/collecting as expected?
```

The dashboard's **Data Health** page is the real answer: per-symbol freshness,
missing cost basis, orphaned sells, suspected splits. It judges freshness against
the collector's own runs (`snapshot_runs`) rather than wall-clock age, because a
weekend gap is legitimately ~64 hours and a threshold that tolerates it can't
detect a collector that died on Tuesday.

If prices look stale:

1. `make logs ARGS=scheduler` — is it inside the window at all?
2. Is the window right for your timezone? (see above)
3. `make snapshot` — force one run; if that works, the schedule is the problem,
   not the collector.

## Housekeeping

- **Ticker logos** are fetched on demand and cached; refresh with
  `docker compose run --rm dashboard python app/fetch_ticker_logos.py`.
- **Fundamentals enrichment** (optional) needs `FINANCIAL_DATASETS_API_KEY` and
  runs weekly. Without the key it logs that it's disabled and exits cleanly.
- **The advisor** stores each brief in `advisor_briefs` and chat in `chat_log`;
  both grow slowly and are included in backups.
- **Splits** are recorded in `corporate_actions` and applied at read time.
  `docker compose run --rm dashboard python app/check_splits.py` scans for
  unrecorded ones and cross-checks each hit against the vendor — a price ratio
  alone cannot tell a split from a crash.
