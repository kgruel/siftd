# Design: `siftd serve` — HTTP team sync server

## Context

siftd has bidirectional peer-to-peer SQLite sync over SSH (`db push`/`db pull`). This works for personal multi-machine use but doesn't scale to teams: SSH requires shell access, there's no identity or attribution, and no way to query a shared DB remotely.

`siftd serve` adds an HTTP transport layer over the existing SQLite primitives. Same merge, slice, receive, search — just accessible over the network with authentication.

## Design principles

- **Reuse, don't rebuild.** The merge/slice/receive/search machinery is proven. The server is a thin HTTP shell around it.
- **Personal SQLite stays source of truth.** Team members push to the shared DB; they don't merge team data back into personal DBs (pull is for bootstrapping new machines or catching up on team sessions).
- **Provider-agnostic auth.** OIDC JWT validation or OAuth2 token introspection — the server validates tokens, it doesn't issue them.
- **Optional dependency.** `siftd[serve]` extra, same pattern as `siftd[embed]`. Core siftd has zero server deps.

## Architecture

```
┌─────────────┐     HTTPS + Bearer token     ┌──────────────────┐
│  siftd CLI  │ ──────────────────────────── │   siftd serve    │
│  (client)   │   POST /v1/push  (slice.db)  │                  │
│             │   GET  /v1/pull  (slice.db)  │  ┌────────────┐  │
│  db push    │   GET  /v1/search?q=...      │  │  SQLite DB  │  │
│  db pull    │   GET  /v1/query?...         │  │  (team.db)  │  │
└─────────────┘                              └──────────────────┘
```

Three transport backends, one interface:

```
sync_push() → _push_http()   if remote.path starts with http(s)://
             → _push_ssh()    if remote.host is set
             → _push_local()  if local path
```

## API surface

### `POST /v1/push`

Ingest a slice into the team DB.

- **Body:** binary SQLite slice (`application/octet-stream`)
- **Query:** `?rebuild_fts=false` (default false)
- **Auth:** Bearer token → extracts user identity
- **Action:** `receive_database(slice, team.db)` + attribution
- **Response:** `200 {"status": "created"|"merged", "conversations": N}`

### `GET /v1/pull`

Export a filtered slice from the team DB.

- **Query:** Full `FilterArgs` vocabulary — `?since=DATE&workspace=NAME&model=NAME&tag=TAG&all=true`
- **Auth:** Bearer token
- **Action:** `slice_database(team.db, filters...)` → stream binary
- **Response:** `200 application/octet-stream` + `X-Siftd-Conversations` and `X-Siftd-Size` headers

### `GET /v1/search`

Semantic + FTS search against the team DB.

- **Query:** `?q=QUERY&workspace=NAME&threshold=0.7&n=10&since=DATE&tag=TAG`
- **Auth:** Bearer token
- **Response:** `200` JSON (same shape as `siftd search --json`)

### `GET /v1/query`

List or detail conversations.

- **Query:** Full `FilterArgs` vocabulary + `?n=20&id=CONVERSATION_ID`
- **Auth:** Bearer token
- **Response:** `200` JSON (conversation list or detail)

### `GET /v1/health`

Health check for load balancers.

- **No auth required**
- **Response:** `200 {"status": "ok", "db_size_bytes": N, "conversations": N}`

## Authentication

Two modes, configured per-server. Both extract a user identity string for attribution.

### OIDC (local JWT validation)

Server fetches JWKS from the issuer's `.well-known/openid-configuration` (cached with TTL). Validates signature, expiry, audience. No per-request network call.

```toml
[serve.auth]
issuer = "https://auth.example.com"
audience = "siftd"
identity_claim = "sub"
```

### Token introspection (RFC 7662)

Server calls the OAuth provider's introspection endpoint per-request (cacheable with short TTL). For providers that issue opaque tokens without OIDC support.

```toml
[serve.auth]
introspection_url = "https://auth.example.com/oauth/introspect"
client_id = "siftd-server"
client_secret = "env:SIFTD_AUTH_SECRET"
identity_claim = "username"
```

### Comparison

| | OIDC (JWT) | Introspection (opaque) |
|---|---|---|
| Validation | Local, cryptographic | Network call to auth server |
| Latency | None (cached JWKS) | ~50ms per request (cacheable) |
| Config | `issuer` + `audience` | `introspection_url` + credentials |
| Standard | OpenID Connect | RFC 7662 |

### Client-side token acquisition

The client resolves tokens independently. Three strategies, checked in order:

```toml
[sync.remotes.team.auth]
token_command = "gh auth token"              # run command, read stdout
# or
token = "env:SIFTD_TOKEN"                   # env var
# or
token = "file:~/.config/siftd/team.token"   # file path
```

`token_command` is most flexible — delegates to whatever CLI the auth provider ships.

## Attribution

### Push log table (server-side)

```sql
CREATE TABLE push_log (
    push_id TEXT PRIMARY KEY,        -- ULID
    user_identity TEXT NOT NULL,     -- from JWT/introspection
    pushed_at TEXT NOT NULL,         -- ISO 8601
    conversations INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_ip TEXT
);
```

Append-only audit trail. No schema changes to core tables.

### Conversation tagging

Each push tags incoming conversations with `pushed_by:<identity>` using existing tag machinery. Attribution is queryable through normal search/query:

```bash
siftd query -l pushed_by:alice
siftd search -l pushed_by:alice "auth redesign"
```

Slices from the team DB remain standard siftd databases — they work with any siftd instance regardless of whether it knows about serve.

## Client remote config

HTTP remotes are detected by URL:

```bash
siftd db remote add team https://siftd.example.com
```

```toml
[sync.remotes.team]
path = "https://siftd.example.com"
last_push = "2026-02-25T10:00:00+00:00"
last_pull = "2026-02-25T10:05:00+00:00"

[sync.remotes.team.auth]
token_command = "gh auth token"
```

No `host` field — for HTTP remotes the URL is the full address. SSH remotes (`host` + `path`) coexist unchanged.

## Server config & CLI

```bash
siftd serve                          # uses [serve] from config.toml
siftd serve --db /data/team.db       # override DB path
siftd serve --port 8484              # override port
siftd serve --no-auth                # dev mode, skip token validation
```

```toml
[serve]
db = "/data/siftd/team.db"
host = "0.0.0.0"
port = 8484
fts_rebuild = "on_push"              # "on_push" | "scheduled" | "off"
```

FTS rebuild strategies:
- `on_push` — rebuild after each push (simple, correct, slight push latency)
- `scheduled` — skip on push, rebuild periodically (better push latency, search lags)
- `off` — no FTS on the team DB

## Module structure

```
src/siftd/
├── serve/
│   ├── __init__.py          # create_app() factory
│   ├── routes.py            # 4 route handlers
│   ├── auth.py              # OIDC + introspection middleware
│   └── dependencies.py      # DB connection, config injection
├── cli_serve.py             # thin CLI dispatcher
```

Litestar is an optional dependency via `siftd[serve]` extra. Same gating pattern as `siftd[embed]`.

## Deployment

### Docker

```dockerfile
FROM python:3.12-slim
RUN pip install siftd[serve]
EXPOSE 8484
CMD ["siftd", "serve"]
```

### systemd

```ini
[Unit]
Description=siftd team server

[Service]
ExecStart=/usr/local/bin/siftd serve
Environment=SIFTD_AUTH_SECRET=<secret>
Restart=always

[Install]
WantedBy=multi-user.target
```

### TLS

Run behind a reverse proxy (Caddy, nginx, Traefik) for TLS termination. The server itself speaks plain HTTP.

## What's new vs. reused

| Component | Status |
|-----------|--------|
| `receive_database()` | Reuse |
| `slice_database()` | Reuse |
| `merge_database()` | Reuse (via receive) |
| `search` / `query` | Reuse |
| `FilterArgs` / filter pipeline | Reuse |
| `config.toml` remote management | Extend (HTTP remotes, auth section) |
| HTTP server (`serve/`) | New (~300-400 lines) |
| Auth middleware | New (~100 lines) |
| HTTP transport (`_push_http`, `_pull_http`) | New (~80 lines) |
| Token acquisition | New (~20 lines) |
| Push log + attribution tagging | New (~30 lines) |
| `docs/concepts/serve.md` | New |
