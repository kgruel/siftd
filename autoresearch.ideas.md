# Autoresearch Ideas

## nolimit_ms (the remaining slow path)
- `nolimit_ms` is ~2500ms for 1644 conversations because Phase 2 correlated subqueries hit cold SQLite page cache
- Consider: precomputed response_count / total_tokens columns on `conversations` table (denormalization)
- Consider: a single aggregate query using window functions instead of per-conv subqueries
- Consider: batch the Phase 2 in chunks of 100 conversation IDs to amortize page cache warming

## Import time (~30-50ms)
- `siftd.api` imports all adapters eagerly (26ms) — not needed for query path
- Lazy-import adapters only when `ingest` or `peek` commands run
- Could shave 20-30ms off startup

## Render time (~35ms)
- `painted` library import + table rendering is stable at ~35ms
- Not much to optimize unless we skip painted for simple table output
