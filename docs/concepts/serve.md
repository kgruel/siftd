# Serve

`siftd serve` runs an HTTP server over a shared SQLite database. It wraps the same slice/receive/search primitives as SSH sync, but exposes them over HTTP with authentication and attribution.

## Why serve

SSH sync works for personal multi-machine use. For teams, it falls short:

- Everyone needs shell access to the host
- No identity — you can't tell who pushed what
- No remote query — you have to pull the whole DB to search it

`siftd serve` adds HTTP transport with bearer token auth, push attribution, and remote search/query endpoints.

## Setting up the server

Install with the `[serve]` extra:

```bash
pip install siftd[serve]
```

Start with defaults:

```bash
siftd serve
```

```
siftd serve — listening on 0.0.0.0:8484
  db: /home/deploy/.local/share/siftd/siftd.db
  auth: disabled (--no-auth)
```

Override via CLI flags or config:

```bash
siftd serve --db /data/team.db --port 9000 --no-auth
```

```toml
# ~/.config/siftd/config.toml
[serve]
db = "/data/siftd/team.db"
host = "0.0.0.0"
port = 8484
fts_rebuild = "on_push"    # "on_push" | "scheduled" | "off"
```

## Authentication

Two modes, both provider-agnostic. The server validates tokens — it doesn't issue them.

### OIDC (JWT validation)

Validates JWTs locally using the issuer's JWKS (cached, no per-request network call):

```toml
[serve.auth]
issuer = "https://auth.example.com"
audience = "siftd"
identity_claim = "sub"
```

### Token introspection (RFC 7662)

Calls an OAuth introspection endpoint per-request (with 60s TTL cache). For providers that issue opaque tokens:

```toml
[serve.auth]
introspection_url = "https://auth.example.com/oauth/introspect"
client_id = "siftd-server"
client_secret = "env:SIFTD_AUTH_SECRET"
identity_claim = "username"
```

### Development mode

`--no-auth` disables token validation entirely. The health endpoint always bypasses auth.

## Client setup

Add an HTTP remote — same command as SSH, just use a URL:

```bash
siftd db remote add team https://siftd.example.com
```

Configure token acquisition:

```toml
# ~/.config/siftd/config.toml
[sync.remotes.team.auth]
token_command = "gh auth token"       # run a command, read stdout
# or
token = "env:SIFTD_TOKEN"            # environment variable
# or
token = "file:~/.config/siftd/team.token"  # file path
```

`token_command` is most flexible — delegates to whatever CLI your auth provider ships.

## Push and pull

Same commands as SSH sync. HTTP transport is auto-detected from the URL:

```bash
siftd db push team       # POST slice to /v1/push
siftd db pull team       # GET slice from /v1/pull
```

All the same filters work:

```bash
siftd db push team --since 7d -w myproject
siftd db pull team --all --dry-run
```

## API endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/health` | GET | Health check (no auth) |
| `/v1/push` | POST | Receive a slice into the team DB |
| `/v1/pull` | GET | Export a filtered slice |
| `/v1/query` | GET | List or detail conversations |
| `/v1/search` | GET | Semantic + FTS search (requires `siftd[embed]` on server) |

Pull and query accept filter params: `workspace`, `since`, `before`, `model`, `tag`, `n`.

## Attribution

Every push records an entry in the `push_log` table: who pushed, when, how many conversations, from what IP. The user identity comes from the auth token (or the `X-Siftd-Identity` header in `--no-auth` mode).

```bash
# Query the push log directly
sqlite3 /data/team.db "SELECT user_identity, pushed_at, conversations FROM push_log ORDER BY pushed_at DESC LIMIT 5"
```

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

Run behind a reverse proxy (Caddy, nginx, Traefik) for TLS termination. The server speaks plain HTTP.

## HTTP vs. SSH

Both transports coexist. Use whichever fits:

| | SSH | HTTP |
|---|---|---|
| Use case | Personal homelab sync | Team shared DB |
| Auth | SSH keys | Bearer tokens (OIDC/introspection) |
| Remote query | No (pull the DB first) | Yes (`/v1/query`, `/v1/search`) |
| Attribution | None | Push log + identity |
| Setup | `siftd` on both machines | `siftd[serve]` on server only |

A single config can have both SSH and HTTP remotes:

```toml
[sync.remotes.alcove]
host = "deploy@192.168.1.44"
path = "/data/siftd/kyle.db"

[sync.remotes.team]
path = "https://siftd.example.com"

[sync.remotes.team.auth]
token_command = "gh auth token"
```
