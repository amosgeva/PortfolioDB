# Contributing

Issues are welcome — bug reports, "this was confusing", and questions about how
something works are all useful. For code, **open an issue first** if the change
is more than a fix: this is one person's ledger as much as it is a project, and
some things are deliberate that look like omissions (no positions table, no
broker sync, manual cash entry).

## Getting a dev environment

```bash
make init                      # .env with generated secrets
make up                        # postgres + dashboard + scheduler
make schema
make demo-seed                 # fictional data — never use your real ledger to develop
make test                      # builds the image from your working tree, runs the fast suites in it
```

`make` on its own lists every target. `make shell` drops you into the app
container. `make up` runs the *published* image; to run the stack from your
checkout instead, `make build` then `make dev-up` (the `docker-compose.dev.yml`
overlay).

`make test` tests what is in your working tree: it builds the image from it
first (cached, so only changed layers rebuild) and runs the suites in that
build. It runs them with a blank database password, so it never touches the
stack's database even when one is running — the database-backed tests run in
CI, and below.

**On Windows:** the Makefile assumes a POSIX shell — its recipes use `sed`,
`base64`, `gzip` and `/dev/urandom` — so a `make.exe` alone will not run it.
[docs/commands.md](docs/commands.md) gives the `docker compose` line behind every
target, and `pdb.ps1` covers `init`, `backup` and `restore`. For the contributor
workflow specifically, where you want `make test` and `make build` too, develop
inside **WSL2** with the clone in the Linux filesystem (`~/portfoliodb`, not
`/mnt/c`). Docker Desktop's own backend is WSL2, so it is almost certainly
already installed.

Enable the pre-commit hook once per clone (hooks aren't cloned):

```bash
git config core.hooksPath .githooks
```

It rejects unused imports in staged Python files, runs ruff on them when ruff is
installed, and runs the suites. `git commit --no-verify` bypasses it for work in
progress.

For a host-Python setup that matches CI, install the image's exact package set
plus the pinned test tools, on Python 3.14 (the image's):

```bash
pip install --only-binary :all: -c app/constraints.txt -r app/requirements.txt -r tools/requirements-ci.txt
ruff check --config tools/ruff.toml .     # the same lint CI's `Lint (ruff)` job runs
```

## Tests

Two suites, and **they need different working directories** — this trips
everyone up once:

```bash
cd app && pytest tests/        # engines, settings, market window: bare imports
pytest app/mcp/tests/ -m "not slow"   # MCP: from the REPO ROOT
```

Running the MCP suite from `app/` puts the local `app/mcp/` package on the path
as top-level `mcp`, shadowing the official `mcp` SDK that fastmcp needs. It
fails as a confusing *"FastMCP server support is not installed"*.

The `slow` marker covers tests that run the real services against a live
database (~30s). Run them separately, not in the same process as the fast suite:

```bash
pytest app/mcp/tests/ -m slow
```

Other tests talk SQL to a real database without a marker — the SQL integration
modules, the Streamlit AppTest — and skip when no database is configured. CI's
`Test suites (live database)` job runs the **whole** app suite and the whole MCP
fast suite against a disposable PostgreSQL with `PORTFOLIODB_TESTS_REQUIRE_DB=1`,
which turns any such skip into a failure, so a database-backed test you add
anywhere runs there without being listed. It also measures coverage and prints
the report (shown, not gated).

To do the same locally, against a throwaway database — never your real one;
these tests write and delete sentinel rows:

```bash
docker run -d --rm --name pdb-test -e POSTGRES_DB=portfoliodb -e POSTGRES_USER=portfoliouser \
  -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
export PORTFOLIODB_HOST=127.0.0.1 PORTFOLIODB_PORT=55432 PORTFOLIODB_PASSWORD=test PORTFOLIODB_TZ=UTC
for f in sql/schema.sql sql/schema_fd.sql sql/migrations/*.sql; do
  docker exec -i pdb-test psql -q -U portfoliouser -d portfoliodb < "$f"; done
python app/demo_seed.py --yes
python app/create_ro_role.py --generate     # export the two lines it prints
pytest app/mcp/tests/ -m slow
(cd app && PORTFOLIODB_TESTS_REQUIRE_DB=1 pytest tests/)
PORTFOLIODB_TESTS_REQUIRE_DB=1 pytest app/mcp/tests/ -m "not slow"
docker stop pdb-test
```

Or just `make test`, which runs the fast suites in an image built from your
working tree.

## What the code expects of you

- **Money is `Decimal`** end to end inside the engines. Convert to `float` only
  when handing data to a DataFrame for display.
- **The ledger is append-only.** Corrections are new rows, and split adjustment
  happens at read time (`app/corporate_actions.py`) — nothing rewrites `lots` or
  `price_snapshots`. The one deletion path, the Manage page, records the whole
  row and a reason in `lot_deletions` in the same statement.
- **There is no positions table.** Open quantity, cost basis and realized P&L are
  recomputed from `lots` on every read, through
  `app/portfolio.py::compute_fifo_merged`. Keep its output contract stable.
- **Readers load lots through `app/ledger_inputs.py`**, which restates them and
  their prices by the splits in effect; a pending (unreviewed) split adjusts
  nothing. Lots are processed in `ledger_core.processing_key` order — date, then
  every BUY before any SELL that day — and "what is held" is
  `holdings.net_quantities` (or `lot_open_quantities()` in SQL), not a fresh
  sum. A reader that does its own thing disagrees with the others the day a
  split is recorded; the source scan in `app/tests/test_ledger_inputs.py` lists
  the deliberate exceptions.
- **Dashboard writes go through the password gate.** A new write path on a
  native page calls `native_auth.require_unlocked()` immediately before it
  writes; the CLIs and the scheduler do not use the password.
- **Symbols are uppercase** on the way in.
- **Credentials come from `app/db.py::load_config`.** Never read a password
  directly, never hardcode one, not even in a throwaway script. The one
  exception is a process that brings its own role and must not hold the
  application's login: it reads only where the database is, through
  `db.load_target()`, as the MCP server does on its read-only role.
- **Settings go through `app/settings.py`** (DB → env → default), so a value can
  be changed from the Settings page. Secrets stay in `.env` only.
- Match the surrounding style, including comment density. Comments here explain
  *why* something is the way it is — especially where the obvious approach was
  tried and abandoned.

## Schema changes

Add a numbered file under `sql/migrations/` **and** make the same change in
`sql/schema.sql` in the same commit, then bump the version header there and
`SCHEMA_VERSION` in `app/mcp/services/cutoff.py`. Fresh installs read
`schema.sql`; existing ones apply migrations in order through
`app/apply_schema.py` (`make schema`), which records each one in
`schema_migrations` with a checksum and reports a migration whose text changed
after it was applied — so fix a shipped migration with a new one. If the MCP
server must read the new table, add it to the read-only role's grant list
(`sql/create_ro_role.sql`) and say in the changelog that `make ro-role` needs
re-running. See `sql/migrations/README.md`.

## Cutting a release

Write the entry into `[Unreleased]` as you go. When you cut it into a numbered
section, **re-read what you are freezing for present-tense claims about anything
that floats.**

A changelog entry may name a version — that is the file's whole job, which is why
`CHANGELOG.md` is exempt from CI's pin grep. **It may not say what a floating tag
*currently* resolves to.** The tag moves; the frozen entry cannot follow it.

This is not hypothetical. The 1.1.3 entry said `:1` "resolves to the current
1.1.2". It was true when written into `[Unreleased]`, and the release cut that
froze it was the release that falsified it — `:1` resolves to 1.1.3, the version
that sentence is printed inside. Name the line (`the current 1.1 line`) or the
release, not the resolution.

Two conventions for fixing an entry after it ships:

- **A stale factual aside is edited in place.** Nobody acted on it, and leaving a
  wrong number to preserve a record of a wrong number helps no one.
- **An instruction a reader may already have followed is annotated, not
  rewritten** — a dated `> Correction, YYYY-MM-DD` note underneath. Quietly
  editing a step someone has already run is the failure this file exists to
  prevent. Where an instruction contains a stale version, bound the version and
  leave the action alone.

None of this is enforced. A guard that could catch it would have to forbid the
file from naming versions, which would break it. This is documentation, and
documentation is weaker than CI — the test counts and the image pins are guarded
because they could be.

## Commits and PRs

Explain *why* in the message, not just what — the diff already says what. Keep
one concern per PR where you can. CI runs both suites on Linux; a green run and
a description of how you verified the change by hand is plenty.

`main` is protected. A pull request merges only when it is up to date with
`main` and these checks pass: `Test suites (no database)`, `Lint (ruff)`,
`Test suites (live database)`, `Application image (build and self-test)`,
`Windows runner (init, both PowerShell hosts)` and `Windows runner
(backup/restore round trip)`. When `main` moves under your branch, merge it in
rather than force-pushing.

### Merging without publishing: `[skip publish]`

Every push to `main` publishes the image as `latest` (`publish.yml`). When a
release is assembled from several pull requests and `latest` should not move
until the last one lands, put **`[skip publish]`** anywhere in the merge
commit's message — for a squash merge, in the title or the body you merge with.
That push then runs CI as usual and publishes nothing.

A version tag always publishes, whatever its commit message says, so the
release that ends such a series ships even if its last merge carried the
marker. The weekly rebuild is not affected either: it rebuilds the commit
`latest` already points at, not `main`'s head, so it never ships a skipped
merge.

### Dependency and image changes

- A Python dependency changes in `app/requirements.txt`, then `make lock`
  regenerates `app/constraints.txt` and the hashed `app/requirements.lock`
  together, and proves the image installs from them. Commit all three.
- The base image, Postgres and pgAdmin move by Dependabot pull request. When one
  bumps Postgres in `docker-compose.yml`, update the `postgres` service image in
  `.github/workflows/ci.yml` in the same pull request — a test fails until the
  two match, so CI tests the database an install gets.

### One note on this repository's history

The history starts at a single squashed commit in August 2026, and commits before
that date do not exist here. That is not a rewrite: development happened in a
private repository whose history carries real trade dates and holdings, so this
one was started clean rather than by publishing that. The private repo is now an
archive and holds nothing but that history and planning notes.

**Development happens here.** Pull requests are reviewed and merged here, and your
commits stay yours.
