# Configuration Reference

_Auto-generated from `src/siftd/config.py`._

Config file: `~/.config/siftd/config.toml`

Set values with `siftd config set <key> <value>` or edit the file directly.

The `[serve.auth]` section is a TOML table (not a flat key) — edit the file directly.

## [db]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `db.path` | string | `~/.local/share/siftd/siftd.db` | Override default database path |

## [tools]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `tools.limit` | int | `20` | Default result limit for tool-search |

## [query]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `query.limit` | int | `20` | Default conversation list limit |
| `query.chars` | int | `200` | Max characters per turn in list view |
| `query.tool_chars` | int | `120` | Max characters for tool content in detail view |

## [ingestion]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ingestion.filter_binary` | bool | `true` | Skip binary content blobs during ingest |

## [serve]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `serve.delegate` | bool | `true` | CLI delegates read ops to running serve instance |
| `serve.url` | string | — | Explicit serve URL for delegation (skips auto-discovery) |
| `serve.db` | string | — | Database path for serve (overrides db.path) |
| `serve.host` | string | `0.0.0.0` | Bind address |
| `serve.port` | int | `8484` | Listen port |
| `serve.fts_rebuild` | string | `on_push` | When to rebuild FTS index: on_push, scheduled, off |

## [adapters]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `adapters.*.locations` | list[string] | — | Override discovery paths for a specific adapter |

## [sync]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sync.ssh.options` | list[string] | — | Extra SSH options passed to asyncssh connect |
| `sync.ssh.connect_timeout_s` | int | `10` | SSH connection timeout in seconds |
| `sync.remotes.*.host` | string | — | SSH host for a named remote |
| `sync.remotes.*.path` | string | — | Remote database path |
| `sync.remotes.*.last_push` | string | — | Timestamp of last push (managed by siftd) |
| `sync.remotes.*.last_pull` | string | — | Timestamp of last pull (managed by siftd) |
| `sync.remotes.*.ssh.options` | list[string] | — | Per-remote SSH options (overrides sync.ssh.options) |

## [update]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `update.check` | bool | `true` | Check PyPI for updates after commands (24h interval) |

## [serve.auth]

Authentication for `siftd serve`. Omit this section to disable auth.
Use `--no-auth` flag to skip even when configured.

### Static token (simplest)

```toml
[serve.auth]
static_token = "your-secret-token"
identity = "username"  # optional, defaults to "local"
```

Supports `env:VAR_NAME` syntax: `static_token = "env:SIFTD_TOKEN"`

### OIDC (JWT)

```toml
[serve.auth]
issuer = "https://your-idp.example.com"
audience = "siftd"          # optional, defaults to "siftd"
identity_claim = "email"    # optional, defaults to "sub"
# jwks_url = "..."         # optional, auto-discovered from issuer
```

### Token introspection (RFC 7662)

```toml
[serve.auth]
introspection_url = "https://your-idp.example.com/introspect"
client_id = "siftd"
client_secret = "env:SIFTD_CLIENT_SECRET"
identity_claim = "username"  # optional, defaults to "username"
```
