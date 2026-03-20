# Autoresearch Ideas

## nolimit_ms (~3000ms, cold-cache bound)
- 1644 conversations → reads most of responses table (363K rows) regardless of query shape
- **Denormalize**: add `prompt_count`, `response_count`, `total_tokens` columns to `conversations` table. Backfill on ingest, update on insert. Eliminates response table scan for listing.
- **SQLite page_size tuning**: larger pages (8KB/16KB) reduce page count and cache misses
- **PRAGMA cache_size**: increase from default 2000 pages to keep more in memory between queries

## Import time (~30-60ms)
- `siftd.api` imports all adapters eagerly (26ms) — not needed for query path
- Lazy-import adapters only when `ingest` or `peek` commands run
- Could shave 20-30ms off startup

## Render time (~35ms)
- `painted` library import + table rendering is stable at ~35ms
- Not much to optimize unless we skip painted for simple table output
