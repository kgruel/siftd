# Configuration Reference

_Auto-generated from `src/siftd/config.py`._

Config file: `~/.config/siftd/config.toml`

All keys can be managed via `siftd config set <key> <value>`.

## [db]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `path` | string | `~/.local/share/siftd/siftd.db` | Override default database path |

## [query]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `limit` | int | `20` | Default conversation list limit |
| `chars` | int | `200` | Max characters per turn in list view |
| `tool_chars` | int | `120` | Max characters for tool content in detail view |

## [ui]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `theme` | string | `siftd` | Terminal colour theme (values: siftd, nord); terminal only — does not affect the web UI |

## [embed]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | — | Embedding backend: voyage\|openai\|gemini\|jina\|mistral\|ollama\|fastembed\|custom\|off |
| `api_key` | string | — | Remote-backend API key (supports env:/file:/literal via the credentials grammar) |
| `model` | string | — | Embedding model name (overrides the preset default) |
| `dimensions` | int | — | Output dimensions (provider matryoshka truncation; overrides preset default) |
| `base_url` | string | — | OpenAI-compatible embeddings base URL (custom/self-hosted override) |
| `auto_index` | bool | `true` | Incrementally embed new conversations at the end of ingest (steady-state only; the first-run backlog goes through an explicit 'siftd embed') |
| `db_path` | string | — | Override the embeddings database path (mirrors db.path) |
| `query_prefix` | string | — | Prefix prepended to queries for prefix-style models (ollama/custom) |
| `document_prefix` | string | — | Prefix prepended to documents for prefix-style models (ollama/custom) |

## [ingestion]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `filter_binary` | bool | `true` | Skip binary content blobs during ingest |

## [search]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `log` | bool | `true` | Capture executed searches (query, fingerprint, result IDs) for recent-searches UX and behavioral ground truth. Local-only, owner-scoped; queries can contain sensitive strings |

## [serve]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `delegate` | bool | `true` | CLI delegates read ops to running serve instance |
| `url` | string | — | Explicit serve URL for delegation (skips auto-discovery) |
| `db` | string | — | Database path for serve (overrides db.path) |
| `host` | string | `127.0.0.1` | Bind address |
| `port` | int | `8484` | Listen port |
| `fts_rebuild` | string | `on_push` | When to rebuild FTS index: on_push, scheduled, off |
| `request_max_body_size` | int or size string | `500MB` | Maximum request body size (e.g. '500MB', '1GB', bytes as int). Uses SI prefixes (1 MB = 1 000 000 bytes) matching Caddy. Must be changed in lockstep with Caddyfile request_body max_size. |

## [serve.auth]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `static_token` | string | — | Static bearer token the SERVER validates against (supports env:VAR syntax) |
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
| `browser_client_id` | string | — | PUBLIC OAuth client ID the browser UI uses for auth-code+PKCE login (usually the same value as auth.client_id). Empty disables browser SSO. |
| `browser_scopes` | list[string] | — | Scopes the browser requests at login as a TOML array; offline_access yields a refresh token. Defaults to ['openid','profile','email','offline_access']. |

## [auth]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `token` | string | — | Static bearer the CLI SENDS to serve (supports env:/file:/literal). For a shared-secret setup, match serve.auth.static_token. |
| `issuer` | string | — | OIDC issuer URL the CLI acquires tokens from (`siftd auth login`) |
| `client_id` | string | — | PUBLIC device-code client ID (NOT serve.auth.client_id, the confidential introspection client) |
| `scope` | string | `openid offline_access` | Space-delimited scopes requested at login (e.g. 'openid offline_access') |
| `device_authorization_endpoint` | string | — | Device authorization endpoint (auto-discovered from issuer if omitted) |
| `token_endpoint` | string | — | Token endpoint (auto-discovered from issuer if omitted) |

## [adapters.*]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `locations` | list[string] | — | Override discovery paths for a specific adapter |
| `enabled` | bool | `true` | Enable/disable an adapter (ingest, peek, doctor) |

## [sync]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `connect_timeout_s` | int | `30` | TCP/SSH handshake timeout in seconds |
| `command_timeout_s` | int | `600` | Total operation timeout (transfer + remote processing) |
| `strategy` | string | `incremental` | Default sync strategy: incremental or full |

## [sync.ssh]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `options` | list[string] | — | Extra SSH options passed to asyncssh connect |
| `connect_timeout_s` | int | `30` | SSH connection timeout in seconds |
| `command_timeout_s` | int | `600` | SSH command timeout in seconds |

## [sync.http]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `connect_timeout_s` | int | `30` | HTTP connection timeout in seconds |
| `command_timeout_s` | int | `600` | HTTP request timeout in seconds |

## [sync.remotes.*]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `host` | string | — | SSH host for a named remote |
| `path` | string | — | Remote database path |
| `last_push` | string | — | Timestamp of last confirmed push (managed by siftd) |
| `last_pull` | string | — | Timestamp of last pull (managed by siftd) |
| `last_sent` | string | — | Timestamp of last staged delivery (managed by siftd) |
| `connect_timeout_s` | int | — | Per-remote connection timeout override |
| `command_timeout_s` | int | — | Per-remote command timeout override |
| `strategy` | string | — | Per-remote sync strategy override |

## [sync.remotes.*.ssh]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `options` | list[string] | — | Per-remote SSH options (overrides sync.ssh.options) |
| `connect_timeout_s` | int | — | Per-remote SSH connection timeout |
| `command_timeout_s` | int | — | Per-remote SSH command timeout |

## [sync.remotes.*.http]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `connect_timeout_s` | int | — | Per-remote HTTP connection timeout |
| `command_timeout_s` | int | — | Per-remote HTTP request timeout |

## [sync.remotes.*.filters]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `workspace` | string | — | Default workspace filter for this remote |
| `tag` | list[string] | — | Only sync conversations with these tags |
| `no_tag` | list[string] | — | Exclude conversations with these tags |
| `owner` | string | — | Default owner filter for this remote |

## [update]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `check` | bool | `true` | Check PyPI for updates after commands (24h interval) |

## [tag_prefixes]

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `*` | string | — | User-defined tag-prefix conventions (e.g. research = "research:") |

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
