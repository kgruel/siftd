# Threat Model — siftd serve + API/serialization

**Deployment context:** `siftd serve` is a Litestar HTTP daemon for the *homelab thin-client* topology (`docs/ops/homelab.md`). It runs in a container (non-root uid 10001) behind a Caddy reverse proxy that terminates TLS and performs OIDC. Multiple users push their personal conversation logs into one shared team SQLite DB; any authenticated user browses/searches the corpus through a JSON API and an htmx web UI.

> **Why the corpus is high-value:** Conversation logs routinely contain pasted secrets, API keys, source code, file paths, and full tool input/output. The [agent-consumer pattern](../../.config/claude/.../memory) means automated agents hold serve tokens and read the DB frequently — so a single leaked/abused token is corpus-level exposure, not a single-user concern.

## Assets

| Asset | Examples | Priority |
|-------|----------|----------|
| Team conversation DB | All users' prompts, responses, thinking, tool I/O, events | **Critical** |
| Auth secrets | static_token, OIDC client_secret, bearer tokens in flight + introspection cache | **Critical** |
| Owner attribution | `conversation_owners`, `push_log` provenance | High |
| API endpoints | 13 JSON routes + 11 htmx fragment routes | High |
| Live session files | Server-host `~/.claude`/`~/.codex`/… read by `/peek` `/follow` | Medium |
| Config | `/etc/siftd/config.toml` (`[serve.auth]`, issuer, secrets) | High |
| Outbound IdP calls | JWKS discovery, RFC 7662 introspection | Medium |

## Trust boundaries

```
Internet ─┬─> Caddy (TLS + OIDC)  ──>  uvicorn :8484  ──>  Litestar app
          │        (separate host)      (--host 0.0.0.0 in container)
          │
   [BOUNDARY 1] Browser ↔ Server         — bearer token in sessionStorage; htmx UI
   [BOUNDARY 2] Auth middleware ↔ routes  — authN only; per-resource authZ is each route's job
   [BOUNDARY 3] User A ↔ User B           — owner scoping in storage (conversation_owners)
   [BOUNDARY 4] Client ↔ team DB (push)   — untrusted SQLite slice merged into shared DB
   [BOUNDARY 5] Server ↔ IdP              — JWKS/introspection (admin-configured URLs)
   [BOUNDARY 6] Container ↔ host          — non-root, read-only config, named data volume
```

The audit's central finding cluster is at **Boundary 2** (the middleware authenticates but does not authorize per-resource — one route forgets to scope by owner) and at the **Caddy/uvicorn seam** (the app trusts that *something upstream* enforced auth, but nothing in the app guarantees it).

## STRIDE analysis

| Threat | Asset × Boundary | Finding |
|--------|------------------|---------|
| **S**poofing | Auth ↔ routes; first-push bootstrap | Fail-open on `0.0.0.0`+no-auth (F2); push owner-stamping is server-authoritative ✓ |
| **T**ampering | Browser ↔ server; CDN | CDN scripts w/o SRI + no CSP (F3); markdown XSS refuted ✓ |
| **R**epudiation | Tag mutations; push source IP | No audit log for tag delete/rename (F6); source_ip is proxy not client (F8c) |
| **I**nfo disclosure | Team DB ↔ users; errors | Event IDOR (F1) — corpus-level; verbose path leaks (F8a); peek/follow host leak (F8b) |
| **D**enial of Service | Auth; push; cache | No rate limiting (F4); unbounded introspection cache (F5); 500MB body default (F9) |
| **E**levation of Privilege | User A ↔ User B | Event IDOR (F1) is horizontal privesc; no-auth write (F2) |

## Attack surface summary

- **Entry points:** 13 JSON routes (`/api/v1/*`) + 11 htmx routes. Auth-exempt: `GET /` (shell), `/api/v1/health`, `/api/v1/sync/status`, `/static/*`.
- **State-changing:** `POST /api/v1/tag`, `POST /api/v1/sessions/{id}/tags`, `POST /api/v1/push`, `POST /tag` — all gated by `require_write()` (no-op when auth off).
- **Untrusted input sinks:** push body (SQLite file), search `q` (FTS), all query filters (parameterized SQL ✓), conversation content (escaped in HTML ✓).
- **Outbound:** issuer discovery + JWKS + introspection (admin-configured; jwks_uri origin-pinned ✓).

See [attack-surface-map.md](./attack-surface-map.md) for the full route inventory.
