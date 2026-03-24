# Configuration Reference

_Auto-generated from `src/siftd/config.py`._

Config file: `~/.config/siftd/config.toml`

All keys can be managed via `siftd config set <key> <value>`.

## [db]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `path` | string | `~/.local/share/siftd/siftd.db` | Override default database path |

## [tools]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `limit` | int | `20` | Default result limit for tool-search |

## [query]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `limit` | int | `20` | Default conversation list limit |
| `chars` | int | `200` | Max characters per turn in list view |
| `tool_chars` | int | `120` | Max characters for tool content in detail view |

## [ingestion]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `filter_binary` | bool | `true` | Skip binary content blobs during ingest |

## [serve]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `delegate` | bool | `true` | CLI delegates read ops to running serve instance |
| `url` | string | — | Explicit serve URL for delegation (skips auto-discovery) |
| `db` | string | — | Database path for serve (overrides db.path) |
| `host` | string | `0.0.0.0` | Bind address |
| `port` | int | `8484` | Listen port |
| `fts_rebuild` | string | `on_push` | When to rebuild FTS index: on_push, scheduled, off |

## [serve.auth]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `static_token` | string | — | Static bearer token for auth (supports env:VAR syntax) |
| `identity` | string | `local` | User identity for static_token mode |
| `issuer` | string | — | OIDC issuer URL for JWT validation |
| `audience` | string | `siftd` | OIDC audience claim |
| `identity_claim` | string | `sub` | Token claim to use as user identity |
| `jwks_url` | string | — | JWKS URL (auto-discovered from issuer if omitted) |
| `introspection_url` | string | — | RFC 7662 token introspection endpoint |
| `client_id` | string | — | Client ID for introspection auth |
| `client_secret` | string | — | Client secret for introspection (supports env:VAR syntax) |
| `required_scopes` | list[string] | — | Scopes the token must have for any access (all-of) |
| `write_scopes` | list[string] | — | Additional scopes required for write operations (any-of) |

## [adapters.*]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `locations` | list[string] | — | Override discovery paths for a specific adapter |

## [sync.ssh]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `options` | list[string] | — | Extra SSH options passed to asyncssh connect |
| `connect_timeout_s` | int | `10` | SSH connection timeout in seconds |

## [sync.remotes.*]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `host` | string | — | SSH host for a named remote |
| `path` | string | — | Remote database path |
| `last_push` | string | — | Timestamp of last push (managed by siftd) |
| `last_pull` | string | — | Timestamp of last pull (managed by siftd) |

## [sync.remotes.*.ssh]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `options` | list[string] | — | Per-remote SSH options (overrides sync.ssh.options) |

## [update]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `check` | bool | `true` | Check PyPI for updates after commands (24h interval) |

## Examples

```bash
# Set static auth token
siftd config set serve.auth.static_token mytoken123
siftd config set serve.auth.identity kaygee

# Configure OIDC
siftd config set serve.auth.issuer https://your-idp.example.com

# Override adapter discovery paths
siftd config append adapters.claude_code.locations ~/.claude/projects

# Disable update checks
siftd config set update.check false
```
