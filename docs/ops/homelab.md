# Hosting siftd on a homelab — one human, many machines

A concrete runbook for the topology where:

- A homelab host runs `siftd serve` persistently behind a public DNS name with TLS.
- Each of the human's machines (laptop, desktop, work, ...) ingests its own AI-tool logs locally and pushes them to the homelab.
- All pushes attribute to the same `sub` claim, so the unified view sees every machine's conversations as one owner. Per-machine identity is preserved separately in `push_log` (source IP + `push_id`).

See [concepts/serve.md](../concepts/serve.md) for the conceptual model. This document is the recipe.

## Prerequisites

- A public DNS name you can point at the homelab (e.g. `siftd.example.com`).
- Port `:443` reachable from the public internet (firewall / port-forward / Cloudflare Tunnel — your call).
- An OIDC IdP you control or trust (Authentik, Authelia, Keycloak, Zitadel, Auth0, Google Workspace, GitHub OIDC, ...). The IdP needs to mint long-lived tokens (or refresh tokens) for service-like usage from CLI clients.
- `uv` or `pipx` for installing siftd on the host and each client.

## Server-side setup

### 1. Register an OIDC client

In your IdP, create a new application/client:

- **Audience / client ID**: `siftd` (this is what `serve.auth.audience` will check).
- **Token signing**: RS256 or ES256 (siftd validates both).
- **Allowed grants**: whatever path you'll use to mint per-machine tokens (refresh token, client credentials, or device code).
- **Identity claim**: siftd uses the `sub` claim by default (controllable via `[serve.auth] identity_claim = "..."`). The configured claim is **required** to be present and non-empty on every accepted token; a missing/empty claim now returns 401 rather than silently mapping to a synthetic "unknown" owner.

  This gives you two ways to make multiple machines share an owner:

  - **Same `sub` per machine** (the common case): mint each machine's token under the same human user account in your IdP. All machines authenticate as you; `sub=kyle` on every token. Simplest.
  - **Custom shared claim** (client-credentials / service-account flows where `sub` naturally differs per machine): configure your IdP to emit a custom claim like `owner` or `tenant` with the same value across machines, and set `identity_claim = "owner"` in `[serve.auth]`. Each machine still has a distinct `sub`, but `conversation_owners` rows use the shared `owner` claim.

Mint one initial long-lived token to confirm the loop works end-to-end. Long-lived tokens are simplest for headless clients; if you prefer rotation, drive each machine through a refresh-token flow via `token_command`.

### 2. Install siftd on the homelab

```bash
uv tool install 'siftd[serve]'
```

### 3. Server config

siftd reads its config from `$XDG_CONFIG_HOME/siftd/config.toml`, defaulting to `~/.config/siftd/config.toml`. For a daemon that runs as the `siftd` system user, there are two practical layouts:

- **Standard FHS layout** (recommended): set `Environment=XDG_CONFIG_HOME=/etc` in the systemd unit (shown in step 4). The lookup resolves to `/etc/siftd/config.toml` — FHS-conventional and easy for operators to find.
- **Per-user**: drop the config at `/home/siftd/.config/siftd/config.toml` and add no systemd override. Less standard for daemons but simpler.

Both are valid; pick one and stick with it. The examples below assume `XDG_CONFIG_HOME=/etc` and `/etc/siftd/config.toml`.

```toml
[serve]
host = "127.0.0.1"           # bind to loopback; Caddy fronts public traffic
port = 8484
db = "/var/lib/siftd/siftd.db"
fts_rebuild = "on_push"

[serve.auth]
issuer = "https://idp.example.com/realms/main"
audience = "siftd"
identity_claim = "sub"
required_scopes = ["siftd:read"]
write_scopes = ["siftd:write"]
```

Notes:

- `issuer` must match the `iss` claim on the JWT. Tokens from a different issuer are rejected (this is what the `iss` validation in `serve/auth.py` enforces).
- `required_scopes` is *all-of* — every read needs `siftd:read`.
- `write_scopes` is *any-of* — every push needs at least one of these. The CLI's `db push` is a write op and goes through `require_write`.

### 4. systemd unit

`/etc/systemd/system/siftd-serve.service`:

```ini
[Unit]
Description=siftd HTTP server
After=network.target

[Service]
Type=simple
User=siftd
Group=siftd
# siftd reads $XDG_CONFIG_HOME/siftd/config.toml. With this set, the daemon
# reads /etc/siftd/config.toml — the FHS-conventional location.
Environment=XDG_CONFIG_HOME=/etc
ExecStart=/usr/local/bin/siftd serve
WorkingDirectory=/var/lib/siftd
Restart=on-failure
RestartSec=5s
# Hardening (tune as needed)
ProtectSystem=strict
ReadWritePaths=/var/lib/siftd
ProtectHome=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /usr/sbin/nologin siftd
sudo mkdir -p /var/lib/siftd /etc/siftd
sudo chown -R siftd:siftd /var/lib/siftd
sudo systemctl daemon-reload
sudo systemctl enable --now siftd-serve
sudo systemctl status siftd-serve
```

### 5. Caddy reverse proxy + TLS

`/etc/caddy/Caddyfile`:

```
siftd.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8484
}
```

```bash
sudo systemctl reload caddy
```

Caddy obtains and renews the LE cert automatically. To smoke-test from a third machine:

```bash
curl -i https://siftd.example.com/api/v1/health
# Expect: 200 OK + {"status": "ok", ...}  (this route is opt-out from auth)
```

Then check an authenticated route with a freshly minted token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://siftd.example.com/api/v1/stats
```

If you get `401 Missing bearer token` from `/api/v1/stats` without the header — auth is working. If you get `401 Invalid token` with a valid token, check the `iss`, `aud`, and `identity_claim` match your `[serve.auth]` block — see the Troubleshooting section below for a local JWT decode recipe (don't use web-based decoders).

## Client-side setup (per machine)

Repeat for each of your machines.

### 1. Install siftd

```bash
uv tool install siftd
```

> **Heads-up — uv-tool drift.** The `siftd` binary on PATH is a uv-tool *snapshot* of the package, not a live link to the repo. After upgrading the package (or pulling new code if you're tracking `main`), reinstall with `uv tool install --force siftd` or the binary will keep running an old version. This sibling-traps anyone doing post-release smoke tests.

### 2. Mint a per-machine token

How you mint depends on your IdP. The principle: a long-lived token (or a refresh-token-backed shell command) that carries `sub=<your-human-id>` and scopes `siftd:read siftd:write`. Each machine gets its *own* token so you can revoke one without affecting the others.

Store it somewhere your shell can read:

```bash
# Option A: in an env var
echo 'export SIFTD_TOKEN="..."' >> ~/.zshenv

# Option B: in a password store (1Password / pass / etc.) — pulled via token_command
pass insert siftd/laptop  # then refer to it via token_command below
```

**Option C — interactive device-code login (`siftd auth login`).** Best for a box
you SSH into: authorize on your phone/laptop browser, nothing needs a localhost
browser on the target machine. Requires a **public** (no-secret) device-code
client registered at your IdP. Configure the client-side acquisition namespace
(distinct from `serve.auth.*`, which is *server* validation config) and log in:

```bash
siftd config set auth.issuer    https://your-idp.example.com/application/o/siftd/
siftd config set auth.client_id <public-device-code-client-id>
# auth.scope defaults to "openid offline_access"; offline_access is what gets
# you a refresh token so the CLI can auto-refresh instead of re-prompting.

siftd auth login     # prints a URL + code; poll completes once you authorize
siftd auth status    # show where the token lives + its expiry
siftd auth logout     # delete the stored credential
```

The acquired token is stored `0600` under `~/.local/state/siftd/credentials/`,
keyed by issuer, and presented automatically on delegated reads. It is refreshed
proactively when near expiry and reactively on a `401`. This is the recommended
path once `[auth].issuer` is set.

### 3. Client config

The CLI has **two distinct auth pipelines** that don't share config. Both draw
*client* send-tokens from the `[auth]` namespace — never from `serve.auth.*`,
which is the server's *validation* config:

- **Delegated reads** (`siftd query`, `siftd search`, etc.) — token resolved by `serve/client.py:_resolve_bearer_token`, in precedence order, with the source tag in parens: (1) `SIFTD_SERVE_TOKEN` / `SIFTD_SERVE_DELEGATION_TOKEN` env vars (`env`); (2) a device-code credential acquired via `siftd auth login` (auto-refreshed near expiry — see below), active only when `[auth].issuer` is configured (`device-code`); (3) a static `[auth].token` (`env:`/`file:`/literal) (`static`). On a `401`, **only** a `device-code` token is refreshed once and retried — an `env`/`static` token that 401s is never swapped for an unrelated credential. **`token_command` is not honored here** — that's a sync-push affordance.
- **Sync push** (`siftd db push`) — token resolved by `api/auth.py:acquire_token` from a named remote's `[sync.remotes.<name>.auth]` block. Accepts: `token_command`, `token = "env:VAR"`, `token = "file:..."`, or a literal.

For one machine that does both reads and pushes, you need both configs. `~/.config/siftd/config.toml`:

```toml
# --- Delegated read auth (client SENDS from [auth]) ---
[serve]
url = "https://siftd.example.com"
delegate = true

[auth]
# Recommended: device-code login (siftd auth login).
issuer = "https://idp.example.com/application/o/siftd/"
client_id = "siftd"
# Or a static shared secret the CLI sends — match serve.auth.static_token
# on the server side:
# token = "env:SIFTD_TOKEN"   # also accepts file:... or a literal

# --- Sync push auth (named remote) ---
[sync.remotes.homelab]
path = "https://siftd.example.com"

[sync.remotes.homelab.auth]
# Any one of these works:
token_command = "pass show siftd/laptop"
# token = "env:SIFTD_TOKEN"
# token = "file:~/.config/siftd/token"
```

With the named remote in place, `siftd db push homelab` (not the bare URL form) goes through the sync auth pipeline and supplies `Authorization: Bearer ...` from `token_command` output. Pushing by URL (`siftd db push https://...`) bypasses the named-remote auth lookup and will go to a public OIDC server without a token — don't do that.

`delegate = true` + an explicit `serve.url` is what bypasses the auto-loopback gate in `serve/delegation.py` and routes reads to the homelab.

### 4. First-loop verification

```bash
# Ingest local AI-tool logs into the local DB.
siftd ingest

# Push the local slice to the homelab via the named remote (auth via [sync.remotes.homelab.auth]).
# Large databases (>400 MB after the 0.8 safety factor on a 500 MB cap) are split
# into time-ordered windows automatically — no flags required. The cursor advances
# per window, so an interrupted push can be resumed by re-running the same command.
siftd db push homelab

# Expected: {"status": "created|merged", "conversations": N, "owned": N}

# Read the homelab's view via the local CLI. Reads delegate over HTTP via [serve.auth].
siftd query
siftd query <id> --json
siftd search "foo"
```

Tail the server log (`journalctl -u siftd-serve -f`) while you run these — you should see one request per CLI command. If a command runs locally instead of round-tripping, either `serve.url` isn't being read, the loopback gate is mis-firing, or the command isn't in the delegation coverage set yet (see [Coverage limits](#coverage-limits) below).

### 5. Schedule ingest+push

Once-per-machine cron or systemd-timer:

```bash
# Crontab line (every 15 minutes). Push uses the named-remote auth from
# [sync.remotes.homelab.auth] — no env var injection needed when token_command
# is configured. Reads (if invoked from cron for any reason) would need
# SIFTD_TOKEN exported separately because [serve.auth] doesn't share auth
# with the sync pipeline.
*/15 * * * * /usr/local/bin/sh -c 'siftd ingest && siftd db push homelab' >> ~/.local/state/siftd/sync.log 2>&1
```

Or systemd-timer (per-user) — adapt to your preferred scheduler.

## Daily workflow

```bash
# Look at recent conversations across all machines
siftd query

# Drill into a specific one
siftd query 01HX... --json

# Search across the unified store
siftd search "migration failed"

# Per-machine push history (audit who pushed what)
sqlite3 /var/lib/siftd/siftd.db \
  "SELECT identity, source_ip, conversations, pushed_at \
   FROM push_log ORDER BY pushed_at DESC LIMIT 20"
```

The `push_log` table preserves per-machine attribution (`source_ip` + a stable `push_id` per call) even though all rows in `conversation_owners` are owned by the same `sub`.

## Coverage limits (current)

The wire-form dissolution closed the previously-deferred delegation gaps; the current picture is:

| Command | Delegates to remote? |
|---|---|
| `siftd query` (list mode) | Yes |
| `siftd query <id> --json` | Yes (correct anchor + fidelity) |
| `siftd query <id>` (non-JSON) | Yes (reconstructed `ConversationDetail` via `from_wire`) |
| `siftd search` | **Partial** — ranking and chunk retrieval run on the homelab; **but** the CLI exits before any delegation attempt if the local DB path doesn't exist (`cmd_search` requires it for FTS init), and after delegation returns, the CLI re-opens the local DB for metadata + file-ref enrichment and for `--around` context windows. A truly DB-less laptop cannot use `siftd search` today; with a local DB, ranking goes remote but enrichment is still local. Fully thin-client search is tracked as follow-up. |
| `siftd tag` (read + write) | Yes |
| `siftd stats` / `siftd db status` | Yes |
| `siftd workspaces` | Yes |
| `siftd export` | Yes (reconstructed `ExportArtifact` via `from_wire` against `/api/v1/export?format=...`) |
| `siftd ingest` | Local-only by design (parses local FS) |
| `siftd db push` | Yes (this is the upload itself) |
| `siftd peek` | Local-only by design (live session files on disk) |
| `siftd adapters` | Local-only by design (filesystem introspection) |

The "Partial" row on `search` is the one remaining thin-client gap. For all other rows, you can run the CLI on a DB-less machine and it will produce the same output it would against a local DB, sourced from the homelab.

## Troubleshooting

- **`401 Missing bearer token`**: the client isn't sending a token. Check `SIFTD_TOKEN` is exported and `token = "env:SIFTD_TOKEN"` is in `[auth]` (the client namespace — **not** `[serve.auth]`, which is server validation). The lookup is `SIFTD_SERVE_TOKEN` env > `SIFTD_SERVE_DELEGATION_TOKEN` env > `[auth].issuer` device-code credential > `[auth].token`. For a shared-secret setup, `[auth].token` (client) and `serve.auth.static_token` (server) must hold the same value.
- **`401 Invalid token`**: token's `iss` or `aud` doesn't match, or the configured `identity_claim` is missing/empty. Decode the JWT payload locally and compare to `[serve.auth]`:

  ```bash
  python -c 'import sys, base64, json; \
      payload = sys.stdin.read().split(".")[1] + "=="; \
      print(json.dumps(json.loads(base64.urlsafe_b64decode(payload)), indent=2))' < ~/.config/siftd/token
  ```

  Never paste real tokens into web tools you don't control — even briefly, even into ones that claim to do the decode "client-side" — they may be cached, logged, or indexed.
- **Reads still local even with `serve.url` set**: confirm `delegate = true` in `[serve]`, and confirm the URL is reachable (`curl https://siftd.example.com/api/v1/health`). Auto-loopback gate only fires if `serve.url` is absent.
- **Push returns 200 but the homelab doesn't see new rows**: the slice was empty. Run `siftd db status` locally to confirm the DB has the conversations you expect, and use `siftd db push --dry-run` to see what would go.
- **Cross-machine view shows split owners**: each machine's token is minting a different `sub` claim. Check the IdP — all machines need to authenticate as the same user.
- **JWKS not refreshing after IdP key rotation**: cache TTL is 1h. Restart `siftd-serve` to force a refresh, or wait. Note: rotating keys invalidates all outstanding tokens at the next refresh; rotating a single token in the IdP (without rotating keys) has no effect on siftd-serve until that token's `exp` — see the security checklist for the OIDC-revocation-latency tradeoff.

## Security checklist

### Auth & transport
- TLS terminated at Caddy (or your proxy of choice); siftd itself is plain HTTP on loopback.
- `--no-auth` is never set on the public-facing instance.
- `iss` + `aud` + `exp` claims all required and validated (siftd enforces; see `serve/auth.py:_validate_oidc`). The configured `identity_claim` must be present and non-empty — missing identity claims are rejected.
- Discovered JWKS URI must share the issuer's origin (scheme + host + port). A misconfigured or compromised issuer endpoint can't redirect siftd to an attacker-controlled JWKS.
- Per-machine tokens minted from the IdP, not shared. JWT-based OIDC validation **does not poll a revocation list** — a signed token remains valid against siftd-serve until its own `exp` claim expires, regardless of whether the IdP has "revoked" it. Practical implications:
  - Set short `exp` claims appropriate to a long-running daemon (minutes-to-an-hour for high-privilege machines; hours for low-privilege). The `exp` is your revocation latency.
  - If you need immediate revocation: use the **introspection** auth mode instead of pure JWT (`introspection_url` in `[serve.auth]`), which calls the IdP on each request (with a short cache). Rotation cost: more IdP load, slightly higher per-request latency.
  - Rotating the IdP's signing keys invalidates all outstanding tokens at the next JWKS cache refresh (1h default in `serve/auth.py:_get_jwks`); use `systemctl restart siftd-serve` to force an immediate refresh if you've just rotated keys.
- Refresh-token flows via `token_command` rotate the *access* token regularly without needing to rotate IdP keys. Recommended for laptops you want to be able to deauthorize quickly.

### Proxy hardening (Caddy / your reverse proxy)
- Cap request body size for `/api/v1/push` — a malformed slice can be large. Caddy: `request_body { max_size 500MB }` (tune to your largest expected push).
- Rate-limit failed auth attempts. Caddy with `caddy-ratelimit` or fronting with `fail2ban` on the access log. The siftd token-validation path doesn't sleep or back off on failure, so brute-force defense lives at the proxy.
- Scrub `Authorization` headers from access logs — Caddy by default does not log headers, but if you've enabled header logging, exclude `Authorization` explicitly.

### At-rest
- `/var/lib/siftd/siftd.db` owned by the `siftd` user, mode 0600. The DB contains conversation content — treat it like email.
- Periodic backups of the DB (it's append-mostly, so `sqlite3 .backup` or rsync to encrypted offsite storage works). Backup destination encrypted at rest.
