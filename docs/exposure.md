# Exposing PortfolioDB safely

**The dashboard has no login of its own.** Anyone who can reach port 8501 can
read your entire ledger; the write pages (Manage, Advisor) sit behind one shared
password, `PORTFOLIODB_DASHBOARD_PASSWORD`, which `make init` / `.\pdb.ps1 init`
generates. That password is a second layer, not a reason to publish the port:
the network is still your main access control.

So **every port is bound to the host's loopback interface by default**:

| Surface | Default binding | Reachable from |
|---|---|---|
| Dashboard | `127.0.0.1:8501` | the host only |
| Postgres | `127.0.0.1:54320` | the host only |
| MCP server | `127.0.0.1:8765` | the host only |
| pgAdmin (`tools` profile) | `127.0.0.1:58080` | the host only |

Before 1.8.0 the dashboard and Postgres bound to every interface (`0.0.0.0`).
If you relied on that — opening the dashboard from your phone, say — that stops
working on upgrade until you choose a way back in: prefer the tunnel or tailnet
in the next sections, or read [Reaching it from your
LAN](#reaching-it-from-your-lan).

## From another machine, without publishing anything

Reach the loopback-bound dashboard over SSH:

```bash
ssh -N -L 8501:127.0.0.1:8501 you@your-host
# now open http://localhost:8501
```

The same works for Postgres (`-L 54320:127.0.0.1:54320`) and the MCP server
(`-L 8765:127.0.0.1:8765`).

## Reaching it from your LAN

An explicit opt-in, through `docker-compose.override.yml` (copy
`docker-compose.override.yml.example`, whose last block is this one, commented
out):

```yaml
services:
  dashboard:
    ports: !override
      - "8501:8501"
```

That binds the dashboard to **every interface on the host**. On a home machine
behind a router that is "your LAN". On a VPS, a machine with a public address,
or a host that is also on a VPN, it is every one of those networks at once — a
wildcard bind is only as private as the host's least private interface. So:
**never port-forward 8501 on your router**, and do not do this at all if any
network the host sits on has guests or devices you don't control. Binding to
one interface's address instead of all of them (`"192.168.1.20:8501:8501"`)
narrows it to that network.

The `!override` tag is not decoration. Compose merges an override file's
`ports` *into* the base file's list rather than replacing it, and it treats a
different host IP as a different entry — so without the tag the new mapping
lands **beside** the inherited loopback one instead of replacing it. Going the
other way (tightening a wildcard to loopback) the same mistake left the
dashboard open while the file said "localhost only", which is how this repo
found out. `!override` replaces the list (Compose 2.24.4 or newer;
`docker compose version` tells you). A test in this repository renders every
example on this page and fails if the result is not exactly what the block
says.

Postgres is the same, and needs it even less: it has a password, but the
dashboard and the scheduler reach it over the compose network whatever you do
here, so publishing it to the LAN only serves a database client on another
machine.

```yaml
services:
  postgres:
    ports: !override
      - "54320:5432"
```

## Remote access, in order of preference

### Tailscale (or any WireGuard mesh)

The least you can get wrong. Install Tailscale on the host and on your phone or
laptop; reach the dashboard at the host's tailnet address. No port is ever
exposed to the internet, and access is authenticated by the mesh.

With the default loopback binding, `tailscale serve` publishes it to your
tailnet only, over HTTPS, without opening it to the LAN:

```bash
tailscale serve --bg 8501
```

### A reverse proxy that authenticates

If you want a real hostname and TLS, terminate both in a proxy and require a
login *in the proxy* — the app will not do it for you. Caddy, with basic auth:

```caddyfile
portfolio.example.com {
    basic_auth {
        # generate with: docker run --rm caddy caddy hash-password
        you $2a$14$replace-with-your-own-hash
    }
    reverse_proxy 127.0.0.1:8501
}
```

Streamlit uses websockets; Caddy proxies them without extra configuration. On
nginx you must pass `Upgrade`/`Connection` headers through explicitly, which is
the usual reason a proxied dashboard loads and then hangs on "Connecting".

Whatever you use: keep the dashboard on its default `127.0.0.1` binding so the
proxy is the only path in, and prefer an identity-aware proxy (Cloudflare
Access, Authelia, oauth2-proxy) over basic auth if more than one person could
plausibly find the hostname.

### Straight to the internet

Don't. The read-only views have no login, there is no rate limiting, and the
write pages are protected by one shared password and nothing else.

## The MCP server

It binds to `127.0.0.1` and requires a bearer token
(`PORTFOLIODB_MCP_TOKEN`). It connects to the database as a **read-only role**
that holds `SELECT` and nothing else — `make ro-role` creates it and prints
the two `.env` lines (`PORTFOLIODB_MCP_RO_USER` / `PORTFOLIODB_MCP_RO_PASSWORD`).
The role is required: without it the server refuses to connect and says how to
create it. That makes "read-only" a property of the database rather than a
promise of the code — a session setting can be switched off by any statement,
a missing grant cannot. The escape hatch for a deliberate operator is
`PORTFOLIODB_MCP_ALLOW_RW_FALLBACK=1`, which runs the server on the
application's read-write credentials and logs a warning on every start.

The MCP container also receives only the environment it needs (the database's
address, its own settings) rather than the whole `.env`: the LLM keys and the
vendor API key have no business in a process that answers an LLM over the
network — and neither does the application's read-write password, which the
`mcp` service is not given at all. The read-only role is the only login it
holds. If you take the fallback, you have to hand it the password yourself,
in `docker-compose.override.yml`:

```yaml
services:
  mcp:
    environment:
      PORTFOLIODB_MCP_ALLOW_RW_FALLBACK: "1"
      PORTFOLIODB_PASSWORD: ${POSTGRES_PASSWORD}
```

Without that block the fallback refuses to start and says which line is
missing, rather than running on a password it was quietly handed.

The role's grants are an explicit list — exactly the tables the server reads,
including `settings` (for the reporting timezone and the saved one-pager) and
not `chat_log` — with no default grant on tables created later. **Re-run
`make ro-role` after an upgrade** that changes that list; it revokes and
re-grants, so an existing role is narrowed to the current list. Run it with
`python app/create_ro_role.py --password <current>` to keep the password.

What else guards it:

- **Token strength.** The server refuses to start with a token under 32
  characters or with fewer than 10 distinct characters (`make init` generates
  40). `PORTFOLIODB_MCP_ALLOW_WEAK_TOKEN=1` is the logged opt-out.
- **Failed-auth throttle.** Ten 401s from one client address within 60 seconds
  get HTTP 429 until the window passes (per server process).
- **Host header.** Every request's `Host` must be a loopback name or one listed
  in `PORTFOLIODB_MCP_ALLOWED_HOSTS`; anything else gets 421. This is what
  stops a DNS-rebinding page from reaching the server through a browser. If an
  agent on another machine connects by name or LAN address, list it there.
- **Deadlines.** Every query has a statement timeout and every connection a
  connect timeout, so one slow query cannot hold the server.
- **Untrusted text.** Free text from people or vendors — lot and cash notes,
  collector errors, news titles, vendor names — is returned as
  `{"untrusted_text": ...}`, capped. Clients should treat it as data to report,
  never as instructions; the server's instructions tell the model so.
- **Masked errors.** A client sees a category ("Could not reach the database
  server"), never the driver's message with hosts and role names; the detail
  is in the server log.

To let an agent on another machine reach it, tunnel rather than publish:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@your-host
```

If you must publish it, put it behind the same authenticating proxy as the
dashboard and treat the token as one factor, not the only one.

## Postgres

Port 54320 is published on loopback so host tools and host-run Python can reach
it at `127.0.0.1:54320`. If nothing outside Docker needs it, remove the mapping
in your override — the other services talk to `postgres` over the compose
network regardless:

```yaml
services:
  postgres:
    ports: !reset []
```

An empty list without the `!reset` tag removes nothing: Compose merges it
with the base file's list and the port stays published. Check the result with
`docker compose config` — the `postgres` service should show no `ports` key.

## What the containers may do

Network binding decides who can reach a service; these decide what a
compromised one could do next. They are on by default in `docker-compose.yml`:

| Service | Linux capabilities | `no-new-privileges` | Read-only root | Healthcheck |
|---|---|---|---|---|
| dashboard, scheduler, scheduler-llm, mcp | none | yes | yes (`/tmp` and the home directory are tmpfs) | yes |
| postgres | `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID` — what its entrypoint needs to hand the data directory to the `postgres` user | yes | no — `docker compose cp`, which the Windows backup and restore depend on, does not work on a read-only container | yes |
| pgadmin (`tools`) | none (listens on 8080 inside the container for that reason) | yes | no — its entrypoint writes its config on start | no |

The scheduled work is split by what it holds. The price collector (`scheduler`),
which fetches and parses third-party pages every five minutes, gets none of
`.env`: only an allowlist of the database login and the settings it reads, so no
API key — not even one added to `.env` later — is in its environment. The
keyed jobs run in `scheduler-llm`, which is handed the `.env` keys they use —
the LLM key for the morning brief, the Financial Datasets key for the weekly
enrichment — with the secrets that belong to other processes (the MCP token and
role, pgAdmin's login, the dashboard password) blanked.
[scheduling.md](scheduling.md#why-two-schedulers) has the reasoning. The
dashboard gets the `scheduler-llm` treatment, minus its own password. The MCP
server gets none of `.env` at all (see above).

Every one of these services runs the image's `runtime` stage, which has no pip:
the package installer and the dependencies it vendors are build-time tools, so
they are in the `dev` stage that `make lock` and `make test` use and not in
anything that runs.

If you relax any of these in an override, say why in a comment there; the
defaults are the ones that were tested.

## A short checklist

- [ ] Nothing forwarded from the router to 8501 or 54320
- [ ] Ports still on `127.0.0.1` unless you opted in — `docker compose config`
      shows `host_ip: 127.0.0.1` on every published port, or exactly the
      interface you chose
- [ ] `PORTFOLIODB_DASHBOARD_PASSWORD` set (`make init` / `.\pdb.ps1 init`
      generates one)
- [ ] Remote access via tailnet or an authenticating proxy, never raw
- [ ] `PORTFOLIODB_MCP_TOKEN` is long and random (`make init` / `.\pdb.ps1 init`
      generates one)
- [ ] `portfoliodb_ro` role created, so the MCP surface can't write — and
      re-run after upgrading (`make ro-role`)
- [ ] `PORTFOLIODB_MCP_ALLOWED_HOSTS` lists every name other machines use to
      reach the MCP server, and nothing else
- [ ] `.env` is not world-readable (`chmod 600 .env`)
- [ ] Backups exist and live somewhere other than this host
      (see [operations.md](operations.md))
