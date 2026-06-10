# Findings — serve security audit (round 2)

Second deep pass after the round-1 fixes (F1–F9) landed. This pass (a) **re-verifies** the round-1 fixes hold and (b) probes the **new attack surface** the fixes introduced (rate-limit store, audit table, `_client_ip`, CSP residual).

Threat model + attack-surface map are unchanged from [round 1](../260524-1342-serve-api-security-audit/threat-model.md); only the deltas below are new.

## Re-verification of round-1 fixes — all hold ✅

| Finding | Re-verified by |
|---------|----------------|
| F1 event IDOR | owner gate present; 5 tests (unit + two-owner route); smoke 8/8 |
| F2 fail-open bind | guard present; 4 tests; smoke container starts only with auth |
| F3 CSP/vendoring | headers on every response; no unpkg refs; 4 tests |
| F4 rate limiting | XFF-aware limiter; 2 tests (429 + disabled) — *see R1* |
| F5 introspection cache | sha256 key + cap; 2 tests |
| F6 audit log | rows written; 1 test — *see R2* |
| F7 peek/follow gate | unregistered on public bind; 2 tests |
| F8a/F8b errors + IP | generic errors, trusted-proxy XFF; 4 tests |
| F9 startup DB | schema DB created; 1 test; smoke first-push merges |

No regression of any prior finding. XSS, SQLi (incl. the new `audit_log` insert — parameterized), JWT, and SSRF re-checked clean.

---

## New findings (introduced by the round-1 fixes)

### [LOW] R1 — Rate-limit store grows unbounded (no expiry sweep) {#r1}

- **OWASP:** A04 Insecure Design · **STRIDE:** Denial of Service · **Confidence:** Confirmed
- **Location:** `src/siftd/serve/app.py` (RateLimitConfig) → Litestar `MemoryStore`
- **Introduced by:** F4

**Description.** The F4 rate limiter uses Litestar's in-memory store keyed by client IP (`RateLimitMiddleware` → `MemoryStore`). `MemoryStore` only evicts an expired key when **that same key is accessed again** (`get()` pops if expired); the middleware never calls `delete_expired()`, and there is no background sweep. An IP that makes one request and never returns leaves a permanent entry. Behind a reverse proxy that forwards real client IPs (the F8b path), the keyspace is the set of distinct client IPs ever seen — which grows without bound on a widely-exposed instance.

**Impact.** Slow memory growth. Negligible at homelab scale (a handful of users → a handful of keys). Material only if the instance is exposed to a large/hostile client population.

**Mitigation.** Periodically call the store's `delete_expired()` (e.g. a Litestar lifespan background task on an interval), or cap the keyspace, or accept-and-document at homelab scale. See [recommendations.md#r1](./recommendations.md).

### [LOW] R2 — `audit_log` has no retention / rotation {#r2}

- **OWASP:** A09 Logging Failures · **STRIDE:** — · **Confidence:** Confirmed
- **Location:** `src/siftd/storage/sqlite.py` (`audit_log` table)
- **Introduced by:** F6

**Description.** The new `audit_log` table grows one row per state-changing operation forever, with no rotation, retention window, or prune command. This is the classic flip side of adding audit logging: unbounded growth. Low impact (rows are tiny; SQLite handles millions), but worth an explicit retention decision so it doesn't silently bloat the DB over years.

**Mitigation.** Add a retention policy (prune rows older than N days) — either a `siftd serve` startup prune or a documented operator task. See [recommendations.md#r2](./recommendations.md).

### [INFO] R3 — `_client_ip` returns empty string for malformed leading-comma XFF {#r3}

- **OWASP:** A09 · **Confidence:** Confirmed
- **Location:** `src/siftd/serve/routes.py:_client_ip`

**Description.** An `X-Forwarded-For` of `", 1.2.3.4"` (leading empty element) makes `_client_ip` return `""` (the empty left-most element), recording an empty `source_ip`. Only reachable *through a configured trusted proxy* (untrusted peers' XFF is ignored — verified), and a correct proxy like Caddy never emits this, so impact is cosmetic. Verified edge cases: IPv6 ✓, surrounding spaces ✓, single value ✓, empty/absent → peer ✓, untrusted peer ignores XFF ✓.

**Mitigation.** Skip empty XFF elements (take the first non-empty). See [recommendations.md#r3](./recommendations.md).

### [INFO] R4 — CSP retains `script-src 'unsafe-inline'` {#r4}

- **OWASP:** A05 · **STRIDE:** Tampering · **Confidence:** Accepted residual
- **Location:** `src/siftd/serve/app.py:_add_security_headers`

**Description.** The F3 CSP keeps `'unsafe-inline'` on `script-src` because the page shell has inline `<script>` blocks and inline `hx-on::`/`onclick` handlers. This means an injected inline script would still execute. **Mitigated in depth:** there is no XSS sink today (round-1 verified), all scripts are vendored (no external origin to poison), and `connect-src 'self'` blocks off-origin token exfiltration even if inline injection occurred. Tightening to a nonce-based policy (moving inline scripts to `/static`, removing inline handlers) is the clean end state.

**Mitigation.** Nonce-based CSP + externalized scripts. Deferred. See [recommendations.md#r4](./recommendations.md).

### [INFO] R5 — Browser behavior under the new CSP is unverified {#r5}

- **Confidence:** Gap (not a vulnerability — a test-coverage gap)
- **Location:** `src/siftd/serve/html_routes.py`

**Description.** Automated coverage verifies the CSP/headers exist and that vendored assets return 200, and smoke-homelab exercises the JSON API + CLI delegation — but **nothing loads `/` in a real browser**. Unverified by tests: that htmx XHRs succeed under `connect-src 'self'` (expected — same origin), that Prism's autoloader honors `data-autoloader-path="/static/vendor/prism/components/"` at runtime, and that inline `hx-on`/divider/login scripts still execute under `'unsafe-inline'`. If the UI broke, the suite would not catch it.

**Mitigation.** A manual browser smoke (load `/`, open a conversation, search, sign in) before relying on the UI; longer term, a Playwright/headless check. See [recommendations.md#r5](./recommendations.md).
