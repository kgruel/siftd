# Security Audit — siftd serve (round 2)

**Date:** 2026-05-24 14:28
**Scope:** `src/siftd/serve/**` + invoked `api/` paths (same as round 1)
**Focus:** re-verify the round-1 fixes hold + probe the new attack surface they introduced
**Depth:** deep · **Baseline:** [`260524-1342-serve-api-security-audit/`](../260524-1342-serve-api-security-audit/overview.md)

## Summary

- **Round-1 findings: all 9 fixes verified holding** (F1–F9); F10 confirmed by-design. No regressions.
- **New findings this pass: 5** — all **Low (2) / Info (3)**, all residuals of the round-1 fixes.
- **No new Critical, High, or Medium.** The two Criticals and four Mediums from round 1 are closed.

## Historical Comparison

**Previous audit:** `260524-1342-serve-api-security-audit/` (earlier today)

| Metric | Round 1 | Round 2 | Change |
|--------|---------|---------|--------|
| Critical | 2 (open) | 0 | ↓ -2 ✅ (F1, F2 fixed) |
| Medium | 4 (open) | 0 | ↓ -4 ✅ (F3–F6 fixed) |
| Low | 3 (open) | 2 (new) | F7/F8a/F8b fixed; R1/R2 new |
| Info | 1 | 3 (new) | F10 by-design; R3/R4/R5 new |
| OWASP coverage | 10/10 | 10/10 | → |
| STRIDE coverage | 6/6 | 6/6 | → |

### Finding status
| Status | Count | Details |
|--------|-------|---------|
| ✅ Fixed since round 1 | 9 | F1, F2, F3, F4, F5, F6, F7, F8a, F8b |
| 📝 By design | 1 | F10 (body cap — enforced + configurable) |
| 🆕 New (Low) | 2 | R1 rate-limit store growth, R2 audit_log retention |
| 🆕 New (Info) | 3 | R3 XFF empty element, R4 CSP unsafe-inline residual, R5 browser-CSP untested |

### Note on the new findings
R1–R5 are **second-order**: each is a residual of a round-1 fix (R1←F4, R2←F6, R3←F8b, R4←F3, R5←F3). This is expected — adding a rate limiter, an audit table, and a CSP each open a small new surface. None reintroduces the original vulnerability, and all are Low/Info. This is a healthy convergence: the high-severity issues are closed and what remains is hardening polish.

## Top items

1. **[LOW] R1** — the rate-limit in-memory store is never swept (`delete_expired` uncalled), so it grows with distinct client IPs. Negligible at homelab scale; matters only if internet-exposed to many clients.
2. **[LOW] R2** — `audit_log` (and `push_log`) grow forever; no retention policy.
3. **[INFO] R5** — the browser UI was never loaded under the new CSP by any test; verify manually before relying on it.

## Files

- [Findings](./findings.md) — re-verification table + R1–R5
- [Recommendations](./recommendations.md) — mitigations (all Low/Info; reported for triage, not auto-fixed)
- [Iteration Log](./security-audit-results.tsv)
- Threat model / attack surface: unchanged — see [round 1](../260524-1342-serve-api-security-audit/threat-model.md)

## Verification basis

`./dev check` → lint clean, **2839 passed** (sole failure: pre-existing `test_peek_follow.py` timing flake, unrelated). `./dev smoke-homelab` → **8/8 PASS**. ~20 new regression tests across `test_serve.py` + `test_serve_auth_edges.py` pin the fixes.

## Disposition

The serve layer's exploitable issues (corpus IDOR, fail-open auth, supply-chain/CSP, brute force, repudiation) are **closed and tested**. The residuals are Low/Info hardening. Recommended next: R3 (2-min XFF fix) + R1/R2 (small) if desired; R4/R5 (CSP nonces + browser test) as a follow-up. Changes are **uncommitted** on `autoresearch/security-serve-audit` (vendored assets are `git add`-staged — commit before any `git stash` or sdist build or they'll be dropped).
