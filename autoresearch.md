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
(Updated every ~5 experiments)

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
