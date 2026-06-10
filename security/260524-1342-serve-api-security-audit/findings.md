# Findings — siftd serve + API/serialization

Ranked by severity. Each finding carries a file:line, attack scenario, code evidence, and mitigation. Confidence: **Confirmed** (code path clearly allows it), **Likely** (guard exists but incomplete), **Possible** (depends on config/runtime).

Refuted/clean checks (XSS, SQLi, JWT, SSRF, CSRF, owner-spoof) are recorded in [owasp-coverage.md](./owasp-coverage.md) and the iteration log — they were tested, not skipped.

---

## [CRITICAL] Finding 1 — Event detail endpoint has no owner authorization (IDOR) {#finding-1}

- **OWASP:** A01 Broken Access Control · **STRIDE:** Information Disclosure / Elevation of Privilege
- **Location:** `src/siftd/serve/routes.py:253` → `src/siftd/api/events.py:267` (`get_event`) + `:101` (`resolve_event_row`)
- **Confidence:** Confirmed
- **Status:** ✅ **Fixed** (owner gate in `get_event` + route wiring + 4 regression tests — see [fix-log.md](./fix-log.md))

**Description.** Every other read route binds the query to the authenticated identity via `_effective_owner()` and the storage layer's `owner_predicate`. `GET /api/v1/events/{id}` does not. The handler explicitly discards the request (`del request  # unused; auth middleware enforces read access`) and calls `get_event(event_id, db_path=…, include_neighbors=…)` — which has **no `owner` parameter and no owner filter**. The comment is wrong: the auth middleware authenticates (proves *who* you are) but performs no per-resource authorization (*may you see this row*). That authorization is each route's responsibility, and this route omits it.

This is made trivially exploitable by the prefix resolver: `resolve_event_row` runs `SELECT … FROM events WHERE id LIKE ? ORDER BY id LIMIT 1` for any input shorter than a full ULID. An attacker doesn't need to know a single event ID — short prefixes (`0`, `01`, …) deterministically return the lexicographically-first matching event across **all owners**. With `?neighbors=true`, each hit also returns adjacent events, widening the window.

**Attack scenario.**
1. Attacker obtains any valid token (their own legitimate account, or a compromised agent token).
2. `GET /api/v1/events/0?neighbors=true` → returns some other user's event with full `content_blocks` (prompt text, tool input/output) + neighbors.
3. Iterate prefixes `00`,`01`,…,`zz` to walk the entire `events` table regardless of `conversation_owners`.
4. Conversation content commonly contains secrets/keys/source → corpus-level disclosure.

**Code evidence.**
```python
# routes.py:253
@get("/api/v1/events/{event_id:str}")
async def event_detail_route(request: Request, event_id: str, db_path: Path,
    neighbors: bool = Parameter(query="neighbors", default=False)) -> dict | Response:
    from siftd.api.events import get_event
    del request  # unused; auth middleware enforces read access   <-- FALSE
    detail = get_event(event_id, db_path=db_path, include_neighbors=neighbors)
    ...

# events.py:267 — no `owner` parameter exists
def get_event(id, *, db_path=None, conn=None, include_content=True, include_neighbors=False): ...
```

**Mitigation.** Scope the event to the caller. Thread `owner=_effective_owner(request, None)` into `get_event` and add an owner predicate via the `conversation_owners` join (the conversation that owns the event), returning `None` (→404) when the owner doesn't match. See [recommendations.md#1](./recommendations.md). The prefix-`LIMIT 1` resolver should additionally raise `AmbiguousPrefix` like the conversation resolver does (`events.py` already flags this as a known concern).

---

## [CRITICAL] Finding 2 — Auth fails open on non-loopback bind {#finding-2}

- **OWASP:** A05 Security Misconfiguration · **STRIDE:** Spoofing / Elevation of Privilege
- **Location:** `src/siftd/cli/serve.py:38-69`, `src/siftd/serve/app.py:65-69`, `src/siftd/serve/auth.py:45-50`, `Dockerfile` CMD
- **Confidence:** Confirmed
- **Status:** ✅ **Fixed** (fail-closed bind guard + `--unsafe-public-no-auth` + 4 regression tests — see [fix-log.md](./fix-log.md))

**Description.** Authentication is installed **only if** `auth_config` is truthy (`app.py`: `if auth_config:`). `cmd_serve` populates `auth_config` from the `[serve.auth]` config table; if that table is absent (or `--no-auth` is passed), the server starts with **no middleware at all**. There is no guard that couples the bind address to the auth state — `--host 0.0.0.0` is accepted with no auth, and the only signal is a stderr line `auth: disabled (no [serve.auth] config)`.

The production `Dockerfile`'s `CMD` runs with `--host 0.0.0.0` (so a separate Caddy host can reach it). The deployment's entire security therefore rests on the operator remembering to mount `/etc/siftd/config.toml` with a populated `[serve.auth]`. If they don't, the container exposes the full multi-user corpus — **readable and writable** — to anyone who can reach :8484, with no warning beyond a log line.

The write side compounds it: `require_write()` returns silently (allows) whenever `request.user` access raises — which is exactly the no-middleware case. So no-auth mode is unauthenticated **read and write** (push, tag, delete).

**Attack scenario.**
1. Operator deploys the image, forgets the config mount (or copies a dev `--no-auth` invocation).
2. Server binds `0.0.0.0:8484` unauthenticated.
3. Any host on the network: `GET /api/v1/export?format=json` → full corpus; `POST /api/v1/tag {"action":"delete",…}` → destroys tags; `POST /api/v1/push` → injects data.

**Code evidence.**
```python
# app.py
middleware = []
if auth_config:                       # falsy → NO auth middleware, any bind address
    middleware.append(create_auth_middleware(auth_config))

# serve.py — no relationship enforced between host and auth_config
host = getattr(args, "host", None) or str(get_config("serve.host") or "127.0.0.1")
auth_config = None
if not args.no_auth:
    auth_config = get_config_table("serve.auth")   # may be None → fail open
```

**Mitigation.** Fail closed: refuse to start when the bind address is non-loopback and auth is unconfigured, unless an explicit `--unsafe-public-no-auth` flag is given. See [recommendations.md#2](./recommendations.md). This is a CLI-semantics decision (flag name/behavior) — flagged for your sign-off before implementing.

---

## [MEDIUM] Finding 3 — No CSP + third-party CDN scripts without SRI + token in sessionStorage {#finding-3}

- **OWASP:** A08 Software/Data Integrity Failures + A05 Misconfiguration · **STRIDE:** Tampering
- **Location:** `src/siftd/serve/html_routes.py:108-110, 168-169, 175-213`; no headers middleware anywhere in `src/siftd/serve/`
- **Confidence:** Confirmed

**Description.** Three weaknesses form one exploit chain:
1. The page shell loads `htmx.org@2.0.4` and `prismjs@1.30.0` from `unpkg.com` with **no `integrity=` (SRI)** attribute (`grep -c integrity= → 0`).
2. The app sets **no `Content-Security-Policy`** (nor `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`) — confirmed no headers middleware exists.
3. The bearer token is held in `sessionStorage` and injected into every request as an `Authorization` header by inline JS.

Individually each is "defense in depth." Chained: if unpkg.com (or the pinned version's published artifact) is compromised, attacker JS runs in the authenticated origin with no CSP to stop it, reads `sessionStorage.getItem('siftd_token')`, and exfiltrates it — full API access as the victim. The absence of CSP also means *any* future reflected/stored XSS (none found today) would be unmitigated.

**Code evidence.**
```html
<script src="https://unpkg.com/htmx.org@2.0.4"></script>            <!-- no integrity -->
<script src="https://unpkg.com/prismjs@1.30.0/components/prism-core.min.js"></script>
```
```js
var token = sessionStorage.getItem('siftd_token');
document.body.setAttribute('hx-headers', JSON.stringify({"Authorization":"Bearer "+token}));
```

**Mitigation.** Add SRI hashes to all CDN `<script>`/`<link>` (or vendor them under `/static`), and add a security-headers layer (`after_request` hook or middleware) emitting a CSP that pins script sources. HSTS is fine to leave to Caddy. See [recommendations.md#3](./recommendations.md).

---

## [MEDIUM] Finding 4 — No rate limiting or lockout on authentication {#finding-4}

- **OWASP:** A04 Insecure Design / A07 Auth Failures · **STRIDE:** Denial of Service
- **Location:** `src/siftd/serve/auth.py` (all `_validate_*` paths); no throttling in `app.py`
- **Confidence:** Confirmed (control absent)

**Description.** None of the three auth modes throttle attempts or lock out after failures. `static_token` is compared with `hmac.compare_digest` (constant-time ✓), but an attacker can still brute-force it offline-speed online — especially the short tokens typical of dev/static setups. OIDC and introspection likewise accept unlimited malformed/forged tokens, each triggering a JWKS lookup or an outbound introspection POST (amplification). There is no per-IP or per-token limiter.

**Attack scenario.** `for t in candidate_tokens: GET /api/v1/stats -H "Authorization: Bearer $t"` — unbounded, no lockout. Against a 16–32 char hand-set static token this is feasible; against introspection it doubles as an amplification DoS on the IdP.

**Mitigation.** Add a rate-limit middleware (Litestar `RateLimitConfig`) on auth-bearing routes, or document that the reverse proxy (Caddy) MUST enforce it and ship that Caddy snippet. Prefer long, randomly-generated static tokens (≥32 bytes). See [recommendations.md#4](./recommendations.md).

---

## [MEDIUM] Finding 5 — Unbounded introspection token cache keyed by raw token {#finding-5}

- **OWASP:** A04 Insecure Design · **STRIDE:** Denial of Service / Info Disclosure
- **Location:** `src/siftd/serve/auth.py:139, 241-283`
- **Confidence:** Confirmed

**Description.** `_introspection_cache: dict[str, tuple[dict, float]]` is a class-level dict keyed by the **raw bearer token string**, with entries only evicted lazily on a *subsequent read of the same key* past its TTL. Tokens that are never seen again are never evicted. Under token churn (each new token is a new key) the dict grows without bound → memory exhaustion. Secondarily, every active+expired token's full introspection body sits in process memory indefinitely keyed by the secret itself.

**Code evidence.**
```python
_introspection_cache: dict[str, tuple[dict, float]] = {}   # class attr, never size-bounded
...
SiftdAuthMiddleware._introspection_cache[token] = (body, cache_deadline)
```

**Mitigation.** Bound the cache (LRU with a max size, e.g. `functools.lru_cache`-style or `cachetools.TTLCache(maxsize=…)`), and key by a hash of the token (`sha256`) rather than the raw secret. See [recommendations.md#5](./recommendations.md).

---

## [MEDIUM] Finding 6 — Tag mutations leave no audit trail {#finding-6}

- **OWASP:** A09 Security Logging & Monitoring Failures · **STRIDE:** Repudiation
- **Location:** `src/siftd/api/tags.py` (no audit/log writes), routes `POST /api/v1/tag`, `POST /tag`
- **Confidence:** Confirmed

**Description.** `push` records provenance in `push_log` (identity, conversations, size, push_id), but tag `apply`/`remove`/`rename`/`delete` write nothing auditable — `grep -niE "audit|log\.|record_|history" src/siftd/api/tags.py` returns nothing. In a shared multi-user corpus, one user (or a compromised token) can `{"action":"delete","tag_name":…}` or rename another owner's tags with no record of who did it or when. Destructive, repudiable, undetectable.

**Mitigation.** Write an audit row (actor identity, action, target, timestamp) for every state-changing tag operation, mirroring `push_log`. At minimum, structured `log.info` on mutations. See [recommendations.md#6](./recommendations.md).

---

## [LOW] Finding 7 — Live-session endpoints bypass owner scoping and the DB {#finding-7}

- **OWASP:** A01 Broken Access Control · **STRIDE:** Information Disclosure
- **Location:** `src/siftd/serve/html_routes.py:534-651` (`/peek`, `/follow`) → `siftd.api.peek`
- **Confidence:** Confirmed

**Description.** `/peek` and `/follow` read live session files directly from the **server host's** filesystem (adapter `DEFAULT_LOCATIONS` — `~/.claude`, `~/.codex`, …), rendering full exchanges *with thinking content* (`read_session_detail(..., include_thinking=True)`). They are auth-gated but apply no owner scoping — any authenticated user sees any session present on the server host. In the intended container topology the host has no user sessions, so impact is low; but if `siftd serve` is ever run on a shared workstation, it leaks every local coding session to any authenticated user. (No path traversal: `find_session_file` only `.startswith()`-matches pre-discovered files, it does not build a path from input — verified.)

**Mitigation.** Either disable `/peek`/`/follow` when running as a server (config gate), or document that these endpoints expose server-host sessions and must not run on shared/multi-user hosts. See [recommendations.md#7](./recommendations.md).

---

## [LOW] Finding 8 — Verbose errors leak internal filesystem paths {#finding-8a}

- **OWASP:** A05 Misconfiguration · **STRIDE:** Information Disclosure
- **Location:** `src/siftd/serve/routes.py:77, 240-241, 322-326` and similar `str(e)` returns
- **Confidence:** Confirmed

**Description.** Several handlers return `str(e)` for `FileNotFoundError`/`ValueError` directly to the client, e.g. `{"error": "Database not found: /var/lib/siftd/siftd.db"}`, disclosing internal absolute paths. The generic `Exception` catch in `_dispatch` is good (returns `"{path} failed"`), but these typed branches bypass that hygiene. Low impact (path disclosure only) but trivially fixable.

**Mitigation.** Return a generic message for not-found/server-side errors; log the detail server-side. See [recommendations.md#8](./recommendations.md).

---

## [LOW] Finding 8b — push_log records proxy IP, not client IP {#finding-8b}

- **OWASP:** A09 Logging Failures · **STRIDE:** Repudiation
- **Location:** `src/siftd/serve/routes.py:835-849` (`_record_push_log`, `source_ip=request.client.host`)
- **Confidence:** Confirmed

**Description.** Behind Caddy, `request.client.host` is always the proxy address (127.0.0.1 / the proxy's IP). The push audit log therefore captures a useless source IP and never the real client. There is no `X-Forwarded-For` handling (with a trusted-proxy allowlist). The provenance record is weaker than it appears.

**Mitigation.** Honor `X-Forwarded-For`/`Forwarded` *only* from a configured trusted-proxy list, else document `source_ip` as proxy-only and drop it. See [recommendations.md#8b](./recommendations.md).

---

## [LOW] Finding 9 — First push adopts an untrusted SQLite file as the team DB {#finding-9}

- **OWASP:** A08 Data Integrity · **STRIDE:** Tampering
- **Location:** `src/siftd/api/receive.py` (`_create_from_source` branch when target DB absent)
- **Confidence:** Possible

**Description.** When the target DB does not yet exist, `receive_database` builds the team DB wholesale from the uploaded slice via `_create_from_source` (then stamps ownership). `_validate_sqlite` + `run_preflight` check structure, but the bootstrap path means the very first authenticated push defines the entire corpus from attacker-controlled bytes. Narrow (bootstrap-only, requires write auth) but a trust gap worth noting.

**Mitigation.** Require an explicit init step (`siftd serve` creates an empty schema-only DB at startup) so push always merges into a server-created DB rather than adopting a client file. See [recommendations.md#9](./recommendations.md).

---

## [INFO] Finding 10 — 500 MB default request body cap is large {#finding-10}

- **OWASP:** A04 Insecure Design · **STRIDE:** Denial of Service
- **Location:** `src/siftd/serve/app.py:48`, `push` stream `routes.py:451`
- **Confidence:** Confirmed (cap works; default is generous)

**Description.** `request_max_body_size` defaults to 500 MB. The cap **is** enforced on the streaming push path (verified against Litestar 2.21: early 413 on `Content-Length`, per-chunk counting for chunked transfers) — so it is not decorative. But 500 MB per request, written to a tempfile with no concurrency limit, means N concurrent pushes can pressure `/tmp`/disk. Acceptable for a trusted-team deployment; tighten if exposure widens.

**Mitigation.** Lower the default to a realistic slice ceiling (e.g. 50–100 MB) via config, and/or cap concurrent pushes. See [recommendations.md#10](./recommendations.md).
