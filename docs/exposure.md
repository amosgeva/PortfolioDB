# Exposing PortfolioDB safely

**The dashboard has no authentication.** Anyone who can reach port 8501 can read
your entire ledger and add or delete trades. This is deliberate — it keeps a
single-user tool simple — and it means the network is your only access control.

The defaults bind to **every interface on the host**:

| Surface | Default binding | Reachable from |
|---|---|---|
| Dashboard | `0.0.0.0:8501` | every network the host is on |
| Postgres | `0.0.0.0:54320` | every network the host is on |
| MCP server | `127.0.0.1:8765` | the host only |
| pgAdmin (`tools` profile) | `127.0.0.1:58080` | the host only |

On a home machine behind a router that is "your LAN". On a VPS, a machine with
a public address, or a host that is also on a VPN, it is every one of those
networks at once — a wildcard bind is only as private as the host's least
private interface. Two things follow: **never port-forward 8501 on your
router**, and if any network the host sits on has guests or devices you don't
control, tighten the binding.

## Localhost only

Copy `docker-compose.override.yml.example` to `docker-compose.override.yml` and
bind to the loopback interface:

```yaml
services:
  dashboard:
    ports: !override
      - "127.0.0.1:8501:8501"
  postgres:
    ports: !override
      - "127.0.0.1:54320:5432"
```

The `!override` tag is not decoration. Compose merges an override file's
`ports` *into* the base file's list rather than replacing it, and it treats a
different host IP as a different entry — so without the tag the loopback
mapping lands **beside** the inherited `0.0.0.0:8501`, and the dashboard stays
open to the network while the file says otherwise. `!override` replaces the
list (Compose 2.24.4 or newer; `docker compose version` tells you). A test in
this repository renders these examples and fails if a wildcard mapping
survives, because the plain-list form looked right and was not.

Then reach it over SSH from another machine:

```bash
ssh -N -L 8501:127.0.0.1:8501 you@your-host
# now open http://localhost:8501
```

## Remote access, in order of preference

### Tailscale (or any WireGuard mesh)

The least you can get wrong. Install Tailscale on the host and on your phone or
laptop; reach the dashboard at the host's tailnet address. No port is ever
exposed to the internet, and access is authenticated by the mesh.

```yaml
# with tailscale on the host, keep the service bound to the LAN and
# let the tailnet address do the work — no reverse proxy needed
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

Whatever you use: bind the dashboard to `127.0.0.1` so the proxy is the only
path in, and prefer an identity-aware proxy (Cloudflare Access, Authelia,
oauth2-proxy) over basic auth if more than one person could plausibly find the
hostname.

### Straight to the internet

Don't. There is no login, no rate limiting, and no audit trail; the ledger is
writable by anyone who loads the page.

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

The MCP container also receives only the environment it needs (the database,
its own settings) rather than the whole `.env`: the LLM keys and the vendor
API key have no business in a process that answers an LLM over the network.

To let an agent on another machine reach it, tunnel rather than publish:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@your-host
```

If you must publish it, put it behind the same authenticating proxy as the
dashboard and treat the token as one factor, not the only one.

## Postgres

Port 54320 is published so host tools and the CLIs can reach it. If nothing
outside Docker needs it, remove the mapping in your override — the other
services talk to `postgres` over the compose network regardless:

```yaml
services:
  postgres:
    ports: !reset []
```

An empty list without the `!reset` tag removes nothing: Compose merges it
with the base file's list and the port stays published. Check the result with
`docker compose config` — the `postgres` service should show no `ports` key.

## A short checklist

- [ ] Nothing forwarded from the router to 8501 or 54320
- [ ] Dashboard bound to `127.0.0.1` if the LAN isn't trusted — and
      `docker compose config` confirms no `0.0.0.0` mapping is left beside it
- [ ] Remote access via tailnet or an authenticating proxy, never raw
- [ ] `PORTFOLIODB_MCP_TOKEN` is long and random (`make init` / `.\pdb.ps1 init`
      generates one)
- [ ] `portfoliodb_ro` role created, so the MCP surface can't write
- [ ] `.env` is not world-readable (`chmod 600 .env`)
- [ ] Backups exist and live somewhere other than this host
      (see [operations.md](operations.md))
