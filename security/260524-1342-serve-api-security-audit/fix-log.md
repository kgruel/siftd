# Fix Log — serve-api-security-audit

Auto-fix loop on Confirmed Critical findings, after your sign-off ("Fix both F1 + F2 now"). Read-only findings left for separate triage.

| Finding | Severity | Status | Verification |
|---------|----------|--------|--------------|
| F1 — event IDOR | Critical | ✅ **Fixed** | 5 owner-scoping tests (unit + route); smoke 8/8 |
| F2 — fail-open public bind | Critical | ✅ **Fixed** | 4 bind-guard tests; smoke 8/8 |
| F3 — CSP/SRI + token exfil | Medium | ✅ **Fixed** | vendored htmx/prism + CSP headers; 4 tests |
| F4 — no rate limiting | Medium | ✅ **Fixed** | XFF-aware RateLimitConfig (600/min default); 2 tests |
| F5 — unbounded introspection cache | Medium | ✅ **Fixed** | sha256 key + size cap; 2 tests |
| F6 — no tag-mutation audit | Medium | ✅ **Fixed** | audit_log table + 3 call sites; 1 test |
| F7 — peek/follow unscoped | Low | ✅ **Fixed** | gated off on public bind; 2 tests |
| F8a — error path leak | Low | ✅ **Fixed** | generic errors + server-side log; 2 tests |
| F8b — proxy-only source IP | Low | ✅ **Fixed** | trusted-proxy XFF; 2 tests |
| F9 — first-push bootstrap | Low | ✅ **Fixed** | server-created DB at startup; 1 test; smoke 8/8 |
| F10 — 500MB body default | Info | 📝 **By design** | cap is enforced + configurable; see below |

## Second batch (F3–F10) — 2026-05-24, after sign-off "fix the remaining issues"

**Files touched:** `serve/auth.py` (F5), `serve/app.py` (F3/F4/F7 — `_add_security_headers`, RateLimitConfig, live-endpoint gate), `serve/routes.py` (F6/F8a/F8b — `_client_ip`, `_actor_identity`, audit calls, generic errors), `serve/html_routes.py` (F3 vendored asset refs, F6 ui_tag audit), `cli/serve.py` (F9 startup DB, F4/F7 config wiring), `storage/sqlite.py` (`ensure_audit_log_table`), `api/serve_status.py` + `api/__init__.py` (`record_audit_event`), `src/siftd/serve/static/vendor/**` (vendored htmx + prism, 28 files).

**Tests added:** ~20 across `test_serve.py` (TestServeHardening, TestAuditAndProvenance, TestErrorHygieneAndStartup) + `test_serve_auth_edges.py` (F5 hash/bound). Existing cache-seeding tests migrated to the hashed key.

**F10 decision (push-back):** *not* lowered. The 500MB default matches the Caddy `request_body max_size` (lockstep-documented) and accommodates legitimately large DB slices (the live DB is ~2.4 GB; a workspace slice can exceed 100 MB). The cap **is** enforced on the streaming path (verified, Litestar 2.21) and is configurable via `serve.request_max_body_size`. Lowering it would reject real pushes — a correctness regression for an Info-severity disk-pressure concern that is better bounded at the container/proxy layer. Documented rather than changed.

**Verification:** `./dev check` → lint clean, **2839 passed** (sole failure: the pre-existing `test_peek_follow.py` timing flake, unrelated). `./dev smoke-homelab` → **8/8 PASS** (validates F9 first-push-merge, F2 container-start-with-auth, F3 vendored assets present in image, F4/F7 don't regress probes).

## F1 — Owner-scope the event detail endpoint

**Change.**
- `src/siftd/api/events.py`: `get_event()` gains an `owner: str | None = None` parameter. After `resolve_event_row`, when `owner` is set and the `conversation_owners` table exists, the event is returned only if its owning conversation belongs to `owner`; otherwise `None` (→404). On pre-migration DBs (no owners table) the filter is a no-op, matching the list/detail paths. Added `has_conversation_owners_table` import.
- `src/siftd/serve/routes.py`: `event_detail_route` now binds `owner = _effective_owner(request, None)` (replacing the incorrect `del request  # auth middleware enforces read access`) and threads it into `get_event`.
- Local CLI callers (`cli/query.py`, `cli/id_cmd.py`) pass `owner=None` → unchanged single-user behavior. `resolve_event_row` left as-is; the owner gate neutralizes the prefix-enumeration vector regardless (a non-owner gets `None`).

**Tests.** `tests/test_api_events.py::TestGetEventOwnerScoping` — owner match returns event; other owner → None; `owner=None` unscoped (CLI); prefix-enumeration blocked for a non-owner.

## F2 — Fail closed on public bind without auth

**Change.**
- `src/siftd/cli/serve.py`: after resolving `host` + `auth_config`, refuse to start (`return 2`, message to stderr) when the bind is non-loopback (`host not in {127.0.0.1, ::1, localhost}`) and auth is off (`--no-auth` or no `[serve.auth]`), unless `--unsafe-public-no-auth` is passed.
- Added the `--unsafe-public-no-auth` argument (documents the danger).

**Tests.** `tests/test_serve.py::TestPublicBindAuthGuard` — public+no-auth refused (uvicorn never bound); public+no-auth+override proceeds; public+auth proceeds; loopback+no-auth allowed.

> **Operational note for the Docker image:** the `Dockerfile` `CMD` uses `--host 0.0.0.0`. With this guard, the container now **refuses to start** unless `[serve.auth]` is mounted (fail-closed) or `--unsafe-public-no-auth` is added. This is the intended behavior — the entrypoint should require the auth config mount. Update `docs/ops/homelab.md` / the compose file accordingly before redeploy.

## Verification

- New tests: `pytest tests/test_api_events.py::TestGetEventOwnerScoping tests/test_serve.py::TestPublicBindAuthGuard` → **8 passed**.
- Targeted suites (`test_api_events`, `test_serve`, `test_serve_routes_edges`, `test_events_roundtrip`) → **80 passed**.
- Full `./dev check` (lint + 2.8k tests) → lint clean; **2839 passed, 5 skipped**. The sole failure is `tests/test_peek_follow.py::test_follow_session_json_output`, a **pre-existing timing flake** (thread-based live-session file polling) that passes in isolation and varied 2→1 across runs — unrelated to this change (the diff does not touch `peek/`/`follow`).

## Not auto-fixed (per scope: Critical/High only)

F3 (CSP/SRI), F4 (rate limiting), F5 (introspection cache), F6 (tag audit log), F7 (peek/follow scope), F8a/8b (error/IP), F9 (bootstrap), F10 (body cap) remain Open — see [recommendations.md](./recommendations.md).

## Reconciliation onto main (2026-06-09, branch feat/security-audit-reconcile)

This audit branch was cut from a much older main; before reconciling, current
main had already absorbed some findings through other branches and the report's
fix-log above predates that. State at reconcile time:

- **F1 (event-detail IDOR)** — already on main via a different implementation
  (`get_event(owner=...)` using `owner_predicate`; `event_detail_route` binds
  the effective owner). Not re-applied.
- **F5 (introspection cache)** — already on main (sha256 key + size cap). Not
  re-applied.
- **F3 (CSP + vendored htmx/prism)** — re-implemented and **extended**: the
  original CSP hard-coded `connect-src 'self'`, but main's browser PKCE SPA
  (`auth.js`, which postdates this branch) does the OIDC discovery and
  code→token exchange via `fetch()` to the issuer, governed by `connect-src`.
  The reconciled CSP widens `connect-src` to the configured issuer origin so
  client-side SSO login isn't silently broken.
- **F2, F4, F6, F7, F8b, F9** — re-implemented against current main's structure.
- **F8a (generic error messages)** — applied to `_dispatch`,
  `event_detail_route`, and the session-queue route. Deliberately NOT applied to
  `tag_write_route`: `apply_tags`/`rename`/`delete` overload `FileNotFoundError`
  for both a missing db file AND safe, contract-pinned domain messages
  ("no matching entities found", "Tag not found: <name>"). Genericizing would
  mask those; the db-file branch is unreachable once F9 pre-creates the DB.
  *Latent smell flagged for a future targeted fix: those domain misses deserve
  their own exception type rather than reusing FileNotFoundError.*
