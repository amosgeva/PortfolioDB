# Every command, with and without `make`

The `Makefile` is a convenience, not a dependency. Every target is a thin wrapper
around one `docker compose` invocation, and this page lists both side by side.

Use this page if you are on **Windows**, or anywhere without a POSIX shell. The
Makefile's recipes reach for `sed`, `base64`, `gzip` and `/dev/urandom`, so
installing a `make.exe` on Windows is not enough by itself — but the compose
commands below are identical on all three platforms, because Docker takes the
same arguments everywhere.

Three targets carry real logic rather than wrapping a single command, and those
are the ones `pdb.ps1` implements for Windows: [`init`](#init),
[`backup`](#backup) and [`restore`](#restore). Read those sections rather than
improvising — **the naive Windows translation of `backup` silently corrupts the
dump.**

---

## The stack

| `make` | Runs |
|---|---|
| `up` | `docker compose up -d` |
| `down` | `docker compose down` |
| `restart` | `docker compose up -d --force-recreate dashboard scheduler` |
| `pull` | `docker compose pull` |
| `ps` | `docker compose ps` |
| `logs ARGS=scheduler` | `docker compose logs -f scheduler` |
| `mcp` | `docker compose --profile mcp up -d mcp` |
| `tools` | `docker compose --profile tools up -d pgadmin` |

## Database

| `make` | Runs |
|---|---|
| `schema` | `docker compose run --rm dashboard python app/apply_schema.py` |
| `psql` | `docker compose exec postgres psql -U portfoliouser -d portfoliodb` |
| `demo-seed` | `docker compose run --rm dashboard python app/demo_seed.py --yes` |
| `ro-role` | `docker compose run --rm dashboard python app/create_ro_role.py --generate` |

## Ledger

Anything you would pass through `ARGS="…"` goes at the end of the compose command
instead.

| `make` | Runs |
|---|---|
| `positions` | `docker compose run --rm dashboard python app/positions.py` |
| `positions ARGS="--symbol NVDA"` | `docker compose run --rm dashboard python app/positions.py --symbol NVDA` |
| `add-lot ARGS="…"` | `docker compose run --rm dashboard python app/add_lot.py …` |
| `sell-lot ARGS="…"` | `docker compose run --rm dashboard python app/sell_lot.py …` |
| `set-cash ARGS="…"` | `docker compose run --rm dashboard python app/set_cash.py …` |
| `watchlist ARGS="NVDA AMD"` | `docker compose run --rm dashboard python app/set_watchlist.py NVDA AMD` |

## Jobs and reports

| `make` | Runs |
|---|---|
| `snapshot` | `docker compose run --rm dashboard python app/snapshot_prices.py --ignore-window` |
| `brief` | `docker compose run --rm dashboard python app/advisor.py brief` |
| `ask ARGS="…"` | `docker compose run --rm dashboard python app/advisor.py ask "…"` |
| `report` | `docker compose run --rm dashboard python app/report_portfolio_db.py` |

## Development

| `make` | Runs |
|---|---|
| `shell` | `docker compose run --rm --entrypoint sh dashboard` |
| `build` | `docker compose -f docker-compose.yml -f docker-compose.dev.yml build` |
| `dev-up` | `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d` |
| `lock` | `docker compose -f docker-compose.yml -f docker-compose.dev.yml build dashboard`, then `docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm --no-deps dashboard pip freeze --exclude-editable` written into `app/constraints.txt` below its comment header. Regenerates the tested dependency lock after a `requirements.txt` change; run it in the image, never from a host interpreter, or CI rejects the result. On Windows PowerShell 5.1 redirect with `\| Out-File -Encoding utf8`, not `>`, which writes UTF-16. |
| `test` | `docker compose run --rm dashboard sh -c 'pip install --user --quiet pytest && export PATH=$PATH:/home/appuser/.local/bin && cd /app/app && python -m pytest tests/ -q && cd /app && python -m pytest app/mcp/tests/ -m "not slow" -q'` |

`make build` also stamps the image with your short commit SHA
(`PORTFOLIODB_VERSION`); without it a locally built image reports its version as
`dev`, which is cosmetic.

---

## init

Creates `.env` from `.env.template`, fills in the secrets that are empty, and
locks the file down. **Safe to re-run — it never rotates a secret you already
have.**

- macOS / Linux: `make init`
- Windows: `.\pdb.ps1 init`

By hand, if you would rather see exactly what it does: copy `.env.template` to
`.env`, then set **`POSTGRES_PASSWORD` and `PORTFOLIODB_PASSWORD` to the same
value**. Compose hands the first to Postgres and the app reads the second, so if
they differ you get a database that starts perfectly and an application that
cannot connect to it. That single mistake is most of why `init` exists.

Optionally set `PORTFOLIODB_MCP_TOKEN` to a long random string if you plan to let
AI agents query the ledger, then restrict the file:

```bash
chmod 600 .env                                                    # macOS/Linux
```
```powershell
icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)"      # Windows
```

**On Windows, write `.env` as UTF-8 without a BOM.** Windows PowerShell 5.1's
`>` and `Out-File` default to UTF-16, which Docker Compose cannot parse — it
reports the variables as unset rather than as malformed, which is a confusing
way to spend an evening. Notepad's default "UTF-8" is correct; "UTF-8 with BOM"
is not.

## backup

`pg_dump` of the whole database — schema, ledger, price history, settings,
briefs — gzipped. Runs against the live container, so no downtime and no need to
stop the scheduler.

- macOS / Linux: `make backup`, or `make backup ARGS=/mnt/nas/pdb` for another
  destination
- Windows: `.\pdb.ps1 backup`, or `.\pdb.ps1 backup C:\backups\pdb`

By hand on **macOS / Linux**:

```bash
set -o pipefail   # without it a failed pg_dump still leaves a valid, empty gzip
out=backups/portfoliodb-$(date +%Y%m%d-%H%M%S).sql.gz
docker compose exec -T postgres pg_dump -U portfoliouser -d portfoliodb | gzip > "$out" \
  && gzip -t "$out" \
  && gunzip -c "$out" | grep -q 'PostgreSQL database dump complete' \
  || { rm -f "$out"; echo "backup failed — nothing kept"; }
```

By hand on **Windows**, which is *not* the same command:

```powershell
docker compose exec -T postgres bash -o pipefail -c 'pg_dump -U portfoliouser -d portfoliodb | gzip -c > /tmp/pdb.sql.gz && gzip -t /tmp/pdb.sql.gz'
docker compose cp postgres:/tmp/pdb.sql.gz .\backups\portfoliodb-20260903-030000.sql.gz
docker compose exec -T postgres rm /tmp/pdb.sql.gz
```

Both forms check more than "a file appeared", and the runners do the same. A
shell reports a pipeline's exit status as its *last* command's, so a `pg_dump`
that died — wrong password, container still starting, disk full — still handed
`gzip` a perfectly valid archive of nothing, and a size check passed it.
`pipefail` makes the producer's failure the pipeline's; `gzip -t` proves the
archive decompresses; and the completion marker is the line `pg_dump` writes
last, which a dump killed mid-way never reaches. The runners build the file
under a `.part` name and rename it only after all three pass.

Two reasons the POSIX one-liner cannot simply be reused, and both produce a file
that looks fine until the day you try to restore it:

1. **There is no `gzip` on Windows**, so the host cannot do the compressing.
2. **Windows PowerShell 5.1 decodes a native command's output as text** before
   writing it, so redirecting a binary dump through `>` re-encodes and corrupts
   it — even when the container did the gzipping. Measured on this project: the
   same dump came out 5.6 MB and unreadable through `>` in 5.1, against 2.9 MB
   and valid. `gzip -t` rejects it; nothing else warns you.

PowerShell 7 does *not* corrupt that redirect, so if you know you are on 7 the
one-liner works. The form above works on both, which is why it is the one
documented — compress *inside* the container and move a finished file out with
`docker compose cp`, so no binary data crosses the host shell at all.

Whatever route you take, **verify the file rather than trusting it.** Any of
these prove it is readable, and all of them are cheap:

```powershell
docker compose exec -T postgres sh -c 'gzip -t < /dev/stdin' < .\backups\portfoliodb-20260903-030000.sql.gz
```
```bash
gzip -t backups/portfoliodb-20260903-030000.sql.gz && echo OK    # macOS/Linux, or WSL
```

**A backup on the same machine is not a backup.** Copy it off — see
[operations.md](operations.md) for the scheduled-copy recipes.

Keep a copy of `.env` (and `philosophy.md`, if you mounted a file rather than
saving it through the dashboard) alongside the dumps. Neither is in the database
and neither is in git, and a dump without the password in `.env` is a database
you cannot open.

## restore

**Refuses to run unless the target database is empty**, because restoring over a
live ledger is how data gets lost twice. Keep that guard if you do it by hand.

- macOS / Linux: `make restore ARGS=backups/portfoliodb-20260903-030000.sql.gz`
- Windows: `.\pdb.ps1 restore .\backups\portfoliodb-20260903-030000.sql.gz`

By hand on **macOS / Linux**:

```bash
gzip -t backups/portfoliodb-20260903-030000.sql.gz && gunzip -c backups/portfoliodb-20260903-030000.sql.gz \
  | docker compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 --single-transaction -U portfoliouser -d portfoliodb
```

By hand on **Windows** — copy the file in, decompress inside the container:

```powershell
docker compose cp .\backups\portfoliodb-20260903-030000.sql.gz postgres:/tmp/pdb.sql.gz
docker compose exec -T postgres bash -o pipefail -c 'gzip -t /tmp/pdb.sql.gz && gunzip -c /tmp/pdb.sql.gz | psql -X -q -v ON_ERROR_STOP=1 --single-transaction -U portfoliouser -d portfoliodb'
docker compose exec -T postgres rm /tmp/pdb.sql.gz
```

The two `psql` flags are what make "restored" mean restored. By default `psql`
prints a SQL error, **carries on with the next statement, and exits 0**, so a
dump that failed halfway left a database with some tables and a success
message. `-v ON_ERROR_STOP=1` makes the first error fatal (exit code 3), and
`--single-transaction` rolls the whole load back so the target is as empty as
you found it and a retry needs no cleanup. `gzip -t` first, so a truncated or
corrupt archive fails before a byte of it reaches the database. The runners do
all three.

One error that the old defaults hid and the new flags surface: if the by-hand
restore stops at `schema "public" already exists`, the dump was taken from a
database whose `public` schema had been dropped and recreated, so it carries a
`CREATE SCHEMA public;` of its own. The target is empty (you checked), so drop
its schema and run the restore again — `DROP SCHEMA public;` — or use a runner,
which does this for you inside the same transaction.

To check the target is empty first — this must print `0`:

```bash
docker compose exec -T postgres psql -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" -U portfoliouser -d portfoliodb
```

To rebuild from scratch before restoring:

```bash
docker compose down
docker volume rm portfoliodb_pgdata     # destroys the current database
docker compose up -d
```

Test a restore once, on purpose, before you need it. An untested backup is a
hypothesis.
