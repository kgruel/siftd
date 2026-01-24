# Roadmap

## Phases

### Foundation (done)
Core loop is solid: ingest → store → query.

- Domain model, two adapters (claude_code, gemini_cli)
- SQLite schema, FTS5 search, query runner
- Tool canonicalization, model parsing
- CLI: `ingest`, `status`, `search`, `queries`, `path`

### Enrichment (done)
Make the stored data more queryable without changing the core loop.

- [x] Approximate cost tracking (flat pricing table, query-time computation)
- [x] Provider population from adapter source
- [x] Cache token extraction into response_attributes
- [x] Manual labels via `tbd label`
- [x] Queries UX (missing var detection, var listing)

### Expansion (done)
More data sources, better search, extensibility.

- [x] Codex CLI adapter
- [x] Adapter plugin system (drop-in `~/.config/tbd/adapters/*.py` + entry points)
- [x] `tbd ask` — semantic search via embeddings (fastembed backend)
- [x] `tbd ask` tuning — exchange-window chunking, model evaluation (bge-small wins)
- [x] Unified chunking: production `tbd ask` and bench share `src/embeddings/chunker.py`
- [x] Bench pipeline: corpus analysis → strategy → build → run → view

### Retrieval Quality (done)
Hybrid retrieval improves relevance; package is installable for programmatic access.

- [x] Hybrid retrieval: FTS5 recall → embeddings reranking (default mode, `--embeddings-only` preserves old behavior)
- [x] Bench pipeline hybrid mode (`--hybrid`, `--recall N`, per-query recall metadata in output)
- [x] Installable package (`pyproject.toml`, `uv.lock`, `tbd.search.hybrid_search` public API)

### Next: Reliability
- [ ] Test infrastructure (pytest, CI basics)
- [ ] Bench runs comparing hybrid vs pure-embeddings retrieval quality

### Precision (future, only when justified)
Add complexity only when real usage demands it.

- [ ] Billing context (API vs subscription per workspace) for real cost tracking
- [ ] Cache-aware pricing (cache_read_per_mtok, cache_creation_per_mtok)
- [ ] Temporal pricing (effective_date) if provider rates change frequently
- [ ] `tbd enrich` — auto-labeling via LLM, only for ops that justify the cost

---

## Explicitly Deferred

| Item | Rationale |
|------|-----------|
| Pluggable storage | Storage is the gravity well — SQL files, FTS5, attributes are all SQLite-specific. No real use case for alternatives. |
| Additional adapters | Cline/Goose/Cursor/Aider removed (no data). Plugin system supports re-adding as drop-in files. Recovery: commit `f5e3409`. |
| `workspaces.git_remote` | Not blocking any current queries. Add when workspace identity matters across machines. |
| `tbd enrich` | Only justified for expensive operations (LLM classification). Manual labels cover current needs. |
| Temporal pricing | Anthropic hasn't changed prices frequently enough to warrant the complexity. |
| Billing context | Requires solving "which workspace uses which billing" — deferred until API spend tracking is a real need. |
| cli.py decomposition | 1166 lines, maintenance hotspot. Not blocking functionality. Refactor when it becomes painful. |

---

*The general principle: don't build automation until patterns emerge from real usage of the manual/explicit version.*
