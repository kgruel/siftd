# Attack Surface Map — siftd serve

## Entry points

### JSON API (`src/siftd/serve/routes.py`)

| Route | Auth | Owner-scoped? | Notes |
|-------|------|---------------|-------|
| `GET /api/v1` | yes | n/a | static endpoint list |
| `GET /api/v1/health` | **no_auth** | n/a | DB status; used by container healthcheck |
| `GET /api/v1/sync/status` | **no_auth** | n/a | redacts inbox `error` strings ✓ |
| `GET /api/v1/stats` | yes | yes (`_effective_owner`) | |
| `GET /api/v1/workspaces` | yes | yes | |
| `GET /api/v1/tags` | yes | yes | |
| `GET /api/v1/conversations` | yes | yes | filters parameterized |
| `GET /api/v1/conversations/{id}` | yes | yes (`resolve_entity_id(owner=)`) | prefix match |
| `GET /api/v1/events/{id}` | yes | **NO — F1 (Critical IDOR)** | `del request`; no owner passed |
| `GET /api/v1/search` | yes | yes | FTS `q` parameterized |
| `GET /api/v1/export` | yes | yes | |
| `GET /api/v1/pull` | yes (write) | yes | streams sliced DB |
| `POST /api/v1/push` | **write** | server-stamps owner ✓ | untrusted SQLite (Boundary 4) |
| `POST /api/v1/tag` | **write** | yes | **no audit trail — F6** |
| `POST /api/v1/sessions/{id}/tags` | **write** | n/a (live) | |

### htmx UI (`src/siftd/serve/html_routes.py`)

| Route | Auth | Notes |
|-------|------|-------|
| `GET /` | **no_auth** | full page shell; serves login form; loads CDN scripts (F3) |
| `GET /meta` `GET /query` `GET /search` `GET /stats` | yes | fragments; output escaped ✓ |
| `GET /peek` `GET /follow` | yes | **read server-host session files — F8b**; bypass DB/owner |
| `POST /tag` | **write** | tag mutation; **no audit — F6** |
| `GET /tags/suggest` `GET /export` | yes | |

## Data flows

```
push:    client SQLite ──stream(≤500MB)──> tempfile ──_validate_sqlite──> run_preflight
         ──merge_database──> team DB  ──_stamp_ownership(authed identity)──> push_log
         [Boundary 4: untrusted file parsed by sqlite3; owner stamped server-side ✓]

read:    bearer token ──auth middleware (authN)──> route ──_effective_owner(sub)──>
         api layer ──owner_predicate / wb.owner()──> SQLite (parameterized)
         [F1: events route skips owner_predicate entirely]

render:  DB rows ──serialization/serve_fmt (JSON) | output/html_fmt (escape)
         ──mistune(escape=True) for markdown── browser
         [XSS sinks all escaped; markdown link schemes neutralized — verified]

auth:    Bearer ──static(hmac.compare_digest) | OIDC(JWKS,RS256/ES256,exp/iss/aud)
         | introspection(RFC7662, cached by raw-token key, F5)
         [no rate limit on any path — F4]
```

## Abuse paths

1. **Corpus exfiltration via event IDOR (F1+enumeration):** any authed token → iterate
   `GET /api/v1/events/0…z?neighbors=true` → prefix resolver returns first match →
   walk every owner's events including tool I/O. No ID knowledge required.
2. **Open data store via misconfig (F2):** deploy container (`--host 0.0.0.0`) without
   `[serve.auth]` mounted → entire corpus readable AND writable unauthenticated by anyone
   who reaches :8484.
3. **Token theft via CDN compromise (F3):** unpkg.com or pinned htmx/prism version
   compromised → arbitrary JS in the authed UI (no CSP, no SRI) → read
   `sessionStorage.siftd_token` → full API access as the victim.
4. **Credential brute force (F4):** unlimited guesses against `static_token` (esp. short
   dev tokens) or introspection — no lockout, no throttle.
5. **Silent tag tampering (F6):** user B (or A's compromised token) deletes/renames A's
   tags — no audit record links the change to an actor.
