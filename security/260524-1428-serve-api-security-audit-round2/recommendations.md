# Recommendations — round 2

All round-2 findings are **Low/Info** and are residuals of the round-1 fixes. None are blocking. Prioritized:

## R1 — Bound the rate-limit store {#r1}
**Effort:** ~20 min. Add a Litestar lifespan background task that calls the rate-limit store's `delete_expired()` on an interval (e.g. every 5 min):
```python
async def _sweep_rate_limit_store(app):
    store = app.stores.get("rate_limit")
    while True:
        await asyncio.sleep(300)
        await store.delete_expired()
```
Or accept at homelab scale and document that the in-memory limiter is sized for a small, trusted client population (use the proxy for internet-facing rate limiting). Lowest-risk: document; implement the sweep only if the instance faces a large client population.

## R2 — `audit_log` retention {#r2}
**Effort:** ~30 min. Add a prune on serve startup (or a `siftd serve --prune-audit-days N`):
```sql
DELETE FROM audit_log WHERE occurred_at < :cutoff;
```
Default e.g. 365 days, configurable via `serve.audit_retention_days` (0 = keep forever). Pairs with the push_log, which has the same property and could share the prune.

## R3 — Skip empty XFF elements {#r3}
**Effort:** ~2 min:
```python
parts = [p.strip() for p in xff.split(",") if p.strip()]
if parts:
    return parts[0]
```

## R4 — Nonce-based CSP {#r4}
**Effort:** ~2–3 h (frontend surgery). Move the three inline `<script>` blocks in `_page_shell` to `/static/siftd.js`, replace inline `onclick`/`hx-on::` with delegated listeners, emit a per-response nonce, and drop `'unsafe-inline'` from `script-src`. Gated on R5 (browser testing) so regressions are catchable.

## R5 — Browser smoke for the UI {#r5}
**Effort:** ~30 min manual / ~2 h automated. Manually: serve locally, open `/`, confirm list/detail/search/sign-in/syntax-highlighting work under the CSP. Automated: a small Playwright check (load `/`, assert no CSP violations in console, assert a Prism-highlighted block renders). Do this before tightening the CSP (R4) or relying on the htmx UI in production.

---

**Disposition:** these are reported for triage, not auto-fixed — they are Low/Info and the auto-fix scope was Critical/High. R3 is trivial if you want it folded into this branch now; R1/R2 are small; R4/R5 are larger follow-ups.
