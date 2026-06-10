# Dependency Audit — siftd serve

## Tool availability

`pip-audit` is **not installed** in the project venv (`.venv`), and the offline environment cannot fetch the PyPI advisory DB. A live CVE scan was therefore **not run**. This file records the installed versions and a knowledge-cutoff assessment instead of silently skipping the check.

> **Action:** install and wire `pip-audit` (or `uv pip audit`) into CI so this becomes an automated gate. The CI non-embed lane already runs `uv sync --extra dev`; add a `pip-audit` step there.

## Installed `[serve]` dependency versions

| Package | Installed | Notes |
|---------|-----------|-------|
| litestar | 2.21.0 | Current 2.x. `request_max_body_size` streaming enforcement verified in this version. |
| uvicorn | 0.41.0 | Current. |
| httpx | 0.28.1 | Current. Used for JWKS/introspection (async) — outbound to admin-configured URLs only. |
| PyJWT (`jwt`) | 2.11.0 | Current. Crypto path (`PyJWKSet`, RS256/ES256) exercised. |
| cryptography | 46.0.5 | Current; backs PyJWT[crypto]. |
| mistune | 3.2.0 | Current 3.x. `escape=True` XSS-safe behavior verified empirically. |

**Assessment (knowledge cutoff Jan 2026):** no known CVEs apply to these versions. All are recent releases. This is a point-in-time judgment, **not** a substitute for an automated scan.

## Pinned front-end libraries (loaded from unpkg.com CDN)

| Library | Pin | Risk |
|---------|-----|------|
| htmx.org | 2.0.4 | Loaded without SRI — see [Finding 3](./findings.md#finding-3). |
| prismjs | 1.30.0 (core + autoloader + components) | Loaded without SRI — see [Finding 3](./findings.md#finding-3). |

These are pinned to exact versions (good — no floating `@latest`), but without Subresource Integrity a compromise of the published artifact or the CDN injects arbitrary JS into the authenticated origin. Vendor them under `/static` or add `integrity=` hashes.

## Supply-chain posture

- `Dockerfile`: multi-stage, build tooling (uv, compilers) excluded from the runtime image; non-root uid 10001; read-only config mount. ✅
- `publish.yml` gates PyPI publish on CI (`needs: ci`). ✅
- No unsigned/unverified third-party build artifacts in the serve path beyond the unvendored CDN scripts above.
