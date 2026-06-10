# Security Audit — siftd serve + API/serialization

**Date:** 2026-05-24 13:42
**Scope:** `src/siftd/serve/**` (auth, routes, html_routes, delegation, client, app) + the `api/` and `serialization/` paths the server invokes
**Focus:** external-facing HTTP attack surface (the `security-serve-audit` worktree intent)
**Depth:** deep — 23 vectors tested across all 10 OWASP categories + STRIDE
**Mode:** report (auto-fix of Criticals **paused for sign-off** — see below)

## Summary

- **Total findings:** 9 + 1 Info
  - **Critical: 2** · Medium: 4 · Low: 3 · Info: 1
- **STRIDE coverage:** 6/6 · **OWASP coverage:** 10/10 (A06 partial — no scanner available)
- **Confirmed: 8** · Possible: 1 · Info: 1
- **Refuted (tested, not findings):** markdown XSS, renderer XSS, SQL injection, JWT validation, SSRF, CSRF, owner-spoofing on push, debug-traceback leakage

The serve layer is **well-built on the basics** — output escaping is disciplined and consistent (no XSS sink found; mistune verified XSS-safe empirically), SQL is uniformly parameterized, the JWT path is hardened (RS256/ES256, kid selection, exp/iss/aud required, jwks_uri origin-pinned), static tokens use constant-time comparison, and the Dockerfile is non-root and minimal. The problems are **authorization gaps and fail-open defaults**, not injection.

> **Why this matters more than the count suggests:** this is a *multi-tenant* corpus where conversation text routinely contains pasted secrets, keys, and source. Per the project's agent-consumer pattern, automated agents hold serve tokens and read the DB constantly — so any single token (legitimate or compromised) reaching the event IDOR is **corpus-level disclosure**, not a one-user leak.

## Top findings

1. **[CRITICAL] Event detail endpoint has no owner authorization (IDOR)** — [`GET /api/v1/events/{id}`](./findings.md#finding-1) discards the request and calls `get_event()`, which has no owner parameter. The prefix resolver (`LIKE 'X%' LIMIT 1`) makes it trivially enumerable: an attacker needs *no* IDs — iterate prefixes to walk every owner's events (full prompt/response/tool I/O). The route's `# auth middleware enforces read access` comment is false — middleware authenticates, it does not authorize per-row.

2. **[CRITICAL] Auth fails open on non-loopback bind** — [`siftd serve --host 0.0.0.0`](./findings.md#finding-2) with no `[serve.auth]` config starts **unauthenticated** (middleware installed only `if auth_config:`), and `require_write()` is a no-op without auth → unauthenticated read *and* write. The only warning is a stderr line. The production Dockerfile's `CMD` uses `--host 0.0.0.0`, so a forgotten config mount exposes the entire corpus.

3. **[MEDIUM] No CSP + CDN scripts without SRI + token in sessionStorage** — [one exploit chain](./findings.md#finding-3): a unpkg.com/version compromise injects JS into the authenticated origin (no CSP to stop it) that reads the bearer token from `sessionStorage` → full API access.

## Files in this report

- [Threat Model](./threat-model.md) — assets, trust boundaries, STRIDE matrix
- [Attack Surface Map](./attack-surface-map.md) — full route inventory + abuse paths
- [Findings](./findings.md) — all findings with evidence + mitigations
- [OWASP Coverage](./owasp-coverage.md) — per-category results incl. refuted checks
- [Dependency Audit](./dependency-audit.md) — versions + pip-audit gap
- [Recommendations](./recommendations.md) — prioritized fixes with code
- [Iteration Log](./security-audit-results.tsv) — every vector tested (incl. refutations)

## Fixes applied

Both Criticals were fixed on this branch after sign-off (see [fix-log.md](./fix-log.md)):

- **F1 ✅** — `get_event()` now takes an `owner` gate; the route binds it to the authenticated identity. Cross-owner event reads (and the prefix-enumeration vector) return 404. 4 regression tests.
- **F2 ✅** — `siftd serve` refuses to bind a non-loopback address with auth disabled unless `--unsafe-public-no-auth` is passed. 4 regression tests.

Verification: lint clean; **2839 tests pass** (`./dev check`). The lone failure is a pre-existing `test_peek_follow.py` timing flake (passes in isolation, untouched by this diff).

**Open (not auto-fixed, scope was Critical/High):** F3–F10 — see [recommendations.md](./recommendations.md). The Mediums (CSP/SRI, rate limiting, introspection-cache bound, tag-mutation audit log) are the recommended next batch.

> **Before redeploying the Docker image:** the F2 guard makes the container fail-closed — `--host 0.0.0.0` (the image `CMD`) now requires `[serve.auth]` mounted, or an explicit `--unsafe-public-no-auth`. Update `docs/ops/homelab.md` / compose accordingly.

Changes are uncommitted on `autoresearch/security-serve-audit` — commit when you're ready.
