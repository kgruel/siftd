# Roadmap

## Phases

### Foundation (done)
Core loop is solid: ingest → store → query.

- Domain model, four adapters (claude_code, codex_cli, gemini_cli, aider)
- SQLite schema, FTS5 search, query runner
- Tool canonicalization, model parsing
- CLI: `ingest`, `status`, `ask`, `query`, `tag`, `tags`, `tools`, `peek`, `doctor`, `config`, `adapters`, `copy`, `backfill`, `path`

### Enrichment (done)
Make the stored data more queryable without changing the core loop.

- [x] Approximate cost tracking (flat pricing table, query-time computation)
- [x] Provider population from adapter source
- [x] Cache token extraction into response_attributes
- [x] Manual labels via `strata tag`
- [x] Queries UX (missing var detection, var listing)

### Expansion (done)
More data sources, better search, extensibility.

- [x] Codex CLI adapter
- [x] Adapter plugin system (drop-in `~/.config/strata/adapters/*.py` + entry points)
- [x] `strata ask` — semantic search via embeddings (fastembed backend)
- [x] `strata ask` tuning — exchange-window chunking, model evaluation (bge-small wins)
- [x] Unified chunking: production `strata ask` and bench share `src/embeddings/chunker.py`
- [x] Bench pipeline: corpus analysis → strategy → build → run → view

### Retrieval Quality (done)
Hybrid retrieval improves relevance; package is installable for programmatic access.

- [x] Hybrid retrieval: FTS5 recall → embeddings reranking (default mode, `--embeddings-only` preserves old behavior)
- [x] Bench pipeline hybrid mode (`--hybrid`, `--recall N`, per-query recall metadata in output)
- [x] Installable package (`pyproject.toml`, `uv.lock`, `strata.search.hybrid_search` public API)

### Reliability (done)
- [x] Test infrastructure (pytest, 253 tests, integration-first, shared fixtures, parametrized)
- [x] Bench pipeline stripped to workbench: build → run → view → corpus_analysis
- [ ] Bench runs comparing hybrid vs pure-embeddings retrieval quality

### Precision (future, only when justified)
Add complexity only when real usage demands it.

- [ ] Billing context (API vs subscription per workspace) for real cost tracking
- [ ] Cache-aware pricing (cache_read_per_mtok, cache_creation_per_mtok)
- [ ] Temporal pricing (effective_date) if provider rates change frequently
- [ ] `strata enrich` — auto-labeling via LLM, only for ops that justify the cost

---

## Explicitly Deferred

| Item | Rationale |
|------|-----------|
| Pluggable storage | Storage is the gravity well — SQL files, FTS5, attributes are all SQLite-specific. No real use case for alternatives. |
| Additional adapters | Cline/Goose/Cursor removed (no data). Aider is built-in. Plugin system supports adding others as drop-in files. |
| `workspaces.git_remote` | Not blocking any current queries. Add when workspace identity matters across machines. |
| `strata enrich` | Only justified for expensive operations (LLM classification). Manual labels cover current needs. |
| Temporal pricing | Anthropic hasn't changed prices frequently enough to warrant the complexity. |
| Billing context | Requires solving "which workspace uses which billing" — deferred until API spend tracking is a real need. |
| cli.py decomposition | ~1700 lines, maintenance hotspot. Not blocking functionality. Refactor when it becomes painful. |

---

*The general principle: don't build automation until patterns emerge from real usage of the manual/explicit version.*
