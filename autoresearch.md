# Autoresearch: hybrid search latency

## Objective
Minimize total wall-clock time to run 50 benchmark queries through `hybrid_search()` with MMR reranking, using the real local siftd database. The 50 queries come from `bench/queries.json` (10 groups x 5 queries).

## Metrics
- **Primary**: total_ms (ms, lower is better) — wall-clock for all 50 queries
- **Secondary**:
  - avg_top1 — mean top-1 cosine similarity across 50 queries (must not regress)
  - avg_redundancy — mean conversation redundancy in top-10 (must not regress = must not increase)

## How to Run
`bash ./autoresearch.sh` — outputs `METRIC name=number` lines.

## Files in Scope
- `src/siftd/search.py` — pipeline orchestrator (hybrid_search, mmr_rerank, filter_conversations, get_active_conversation_ids)
- `src/siftd/storage/embeddings.py` — embedding fetch, decode, cosine search (search_similar, _decode_embedding_numpy)
- `src/siftd/math.py` — cosine_similarity_batch (query vs all embeddings)
- `src/siftd/storage/fts.py` — FTS5 recall (fts5_recall_conversations)
- `src/siftd/paths.py` — XDG paths, db_path() calls get_config() every time
- `src/siftd/config.py` — TOML config parser, load_config() reads file from disk

## Off Limits
- `bench/queries.json` — benchmark queries must not be modified
- `tests/` — test fixtures and test files
- `autoresearch.sh`, `autoresearch.checks.sh` — benchmark infrastructure
- Database files on disk

## Constraints
- All 976 tests must pass (`./dev check`)
- avg_top1 must not decrease (search quality)
- avg_redundancy must not increase (conversation dedup quality)
- No new dependencies

## What's Been Tried

### Kept
1. **Cache active session exclusion (30s TTL)** — 89.6ms/call filesystem scan was called per query. Now cached. Saved ~4.5s.
2. **Share single main DB connection in hybrid_search()** — Saved 2 connection open/close cycles per query.
3. **Cache embedding backend** — get_backend() re-probed ollama HTTP on every call (70ms × 50). Now cached by preferred key. Saved ~3.5s.
4. **In-memory embedding cache** — search_similar() fetched + decoded all embeddings from SQLite per call. Now caches the full numpy matrix + metadata + conv_id→indices lookup. Saved ~900ms.

### Discarded
- Cache db_path() — negligible, benchmark already passes explicit paths
- Batch decode (b''.join + single frombuffer) — within noise, decode isn't the bottleneck
- Subquery+DISTINCT FTS5 recall — slower than GROUP BY + ORDER BY MIN(rank)
- Reduce FTS5 recall from 80→40 — quality regressed (avg_top1 and avg_redundancy both worse)
- Pre-normalize cached embeddings — within noise, ~1K candidates normalize fast enough

### Profile after 4 keeps (baseline 14520ms → 2719ms)
- FTS5 recall: 2327ms (69%) — the AND→OR fallback path is 48ms/query, 43/50 queries fall through to OR
- Embedding search: 876ms (26%) — numpy on cached data, hard to optimize further
- embed_one: 136ms (4%) — model inference, fixed cost
- MMR: 12ms (0.4%) — fast enough

## Loop Protocol
1. Edit code with an optimization idea
2. Run benchmark: `bash ./autoresearch.sh 2>&1` — capture output and exit code
3. If benchmark passed (exit 0) AND `autoresearch.checks.sh` exists, run checks
4. Parse `METRIC name=number` lines from benchmark output
5. Decide: primary metric improved → **keep**, worse/equal → **discard**, failed → **crash**, checks failed → **checks_failed**
6. **keep**: `git add -A && git commit -m "<description>\n\nResult: <json>"`
7. **discard/crash/checks_failed**: `git checkout -- .` to revert all changes
8. Append one JSON line to `autoresearch.jsonl`
9. Print summary. Update this doc every ~5 runs. **Never stop. Never ask to continue.**

**Rules**: Primary metric is king. Simpler is better — removing code for equal perf counts as a win. Don't thrash on the same idea. Think deeper when stuck.
