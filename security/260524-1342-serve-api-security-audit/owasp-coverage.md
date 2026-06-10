# OWASP Top 10 (2021) Coverage — siftd serve

| ID | Category | Tested | Findings | Status |
|----|----------|--------|----------|--------|
| A01 | Broken Access Control | ✓ | F1, F7 | ⚠️ **Critical IDOR** (F1); peek/follow scope (F7) |
| A02 | Cryptographic Failures | ✓ | 0 | ✅ Clean — `hmac.compare_digest`, RS256/ES256, sha256 db_id; secrets at-rest are deployment-owned |
| A03 | Injection | ✓ | 0 | ✅ Clean — parameterized SQL/FTS; markdown XSS refuted empirically; all HTML interpolation escaped |
| A04 | Insecure Design | ✓ | F4, F5, F10 | ⚠️ No rate limit (F4); unbounded token cache (F5); large body default (F10) |
| A05 | Security Misconfiguration | ✓ | F2, F3, F8a | ⚠️ **Fail-open auth** (F2 Critical); no CSP/headers (F3); path leaks (F8a) |
| A06 | Vulnerable Components | ◑ | 0 | ◑ pip-audit unavailable; versions current, no known CVEs at cutoff — see dependency-audit.md |
| A07 | Auth & Identification Failures | ✓ | (F4) | ✅ JWT hardened; ⚠️ no lockout/throttle (shared with F4) |
| A08 | Software & Data Integrity | ✓ | F3, F9 | ⚠️ CDN no-SRI (F3); first-push bootstrap trust (F9) |
| A09 | Security Logging & Monitoring | ✓ | F6, F8b | ⚠️ No tag-mutation audit (F6); proxy-IP-only provenance (F8b) |
| A10 | Server-Side Request Forgery | ✓ | 0 | ✅ Clean — issuer/JWKS/introspection URLs admin-configured; jwks_uri origin pinned to issuer |

**Coverage: 10/10 categories tested** (A06 partial — no scanner). 9 findings (2 Critical, 4 Medium, 3 Low) + 1 Info.

## Per-category detail

### A01 Broken Access Control — ⚠️
- IDOR on parameterized routes: **F1** — `/api/v1/events/{id}` has no owner scoping; prefix resolver enumerable.
- Horizontal privesc: F1 (cross-owner event reads).
- Conversation/stats/tags/search/export: ✅ owner-scoped via `_effective_owner` → `wb.owner()`/`owner_predicate`/`resolve_entity_id(owner=)` (verified `conversations.py:285-300, 590`).
- `_effective_owner` pins owner to authed `sub` — an authed user **cannot** override `?owner=` to read another user (verified).
- Live endpoints bypass DB+owner: **F7**.
- Directory traversal on file ops: ✅ none — `find_session_file` matches discovered files, builds no path from input.

### A02 Cryptographic Failures — ✅
- Static token: `hmac.compare_digest` (constant-time). JWT: RS256/ES256 (asymmetric, no HS/none confusion), `require exp/iss/aud`. db_id: sha256. No MD5/SHA1, no weak RNG in security paths.
- Secrets (`static_token`, `client_secret`) support `env:` indirection; at-rest protection is the deployment's responsibility (config mounted read-only per Dockerfile).

### A03 Injection — ✅ (actively refuted)
- SQL/FTS: all queries parameterized (`?`); FTS `MATCH` takes user `q` as a bound param. No string-built SQL on user input.
- **XSS (markdown):** `mistune.create_markdown(escape=True)` — tested empirically (mistune 3.2.0): `javascript:`/`data:` links rewritten to `#harmful-link`, raw HTML and code escaped. Not exploitable.
- **XSS (renderers):** every dynamic value in `html_fmt.py`/`html_routes.py` flows through `html.escape`, `short_id`, or numeric formatting. Swept for unescaped `{var}` sinks — none found.
- Command injection: no shell/exec in the serve path. SSTI: f-strings are not a user-controlled template engine.

### A04 Insecure Design — ⚠️
- No rate limiting/lockout (**F4**). Unbounded introspection cache (**F5**). 500MB body default (**F10**, cap verified enforced).
- CSRF: not exploitable — auth is via `Authorization` header sourced from `sessionStorage`, which cross-site JS cannot read or set. No cookie auth.

### A05 Security Misconfiguration — ⚠️
- **Fail-open auth on `0.0.0.0` (F2, Critical).** Missing CSP/X-Frame-Options/X-Content-Type-Options (**F3**). Verbose path leaks (**F8a**).
- Debug mode: `create_app` does not set `debug=True` → Litestar default `False`; 500s do not emit tracebacks (verified). ✅
- `/api/v1/sync/status` redacts inbox `error` strings. ✅

### A06 Vulnerable Components — ◑
See [dependency-audit.md](./dependency-audit.md). pip-audit not installed in venv; versions are current; no known CVEs at the Jan 2026 knowledge cutoff. Recommend wiring `pip-audit` into CI.

### A07 Auth Failures — ✅ / ⚠️
- JWT validation is hardened (kid-based key selection, algorithm allowlist, exp/iss/aud required — this is the previously-fixed OIDC path). Introspection rejects missing identity claim. ✅
- No MFA/lockout/throttle (shared with F4). Static-token brute-force feasible if token is short.

### A08 Data Integrity — ⚠️
- CDN scripts without SRI (**F3**). First-push bootstrap adopts client SQLite (**F9**).
- CI/CD: `publish.yml` gates on CI; no unsigned-artifact issue in scope.

### A09 Logging & Monitoring — ⚠️
- No audit trail for tag mutations (**F6**). `push_log` source_ip is the proxy, not the client (**F8b**). No failed-auth logging/alerting (auth failures are `logging.debug` only).

### A10 SSRF — ✅
- Outbound requests target only the admin-configured issuer/JWKS/introspection URLs (not user input). `_jwks_origin_matches_issuer` pins the discovered `jwks_uri` to the issuer's exact origin and fails closed on parse errors — blocks a hostile discovery document from redirecting to attacker-controlled JWKS.
