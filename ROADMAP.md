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

### Expansion (current)
More data sources, more search, extensibility.

- [x] Codex CLI adapter
- [x] Adapter plugin system (drop-in `~/.config/tbd/adapters/*.py` + entry points)
- [x] `tbd ask` — semantic search via embeddings (Ollama/fastembed backends)
- [ ] `tbd ask` tuning — manual testing, chunking/ranking iteration
- [ ] More adapters: Copilot, Cursor, Aider (now just drop-in `.py` files)
- [ ] `workspaces.git_remote` — resolve via `git remote -v` at ingest time
- [ ] More query files — common patterns as shipped defaults

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
| `workspaces.git_remote` | Not blocking any current queries. Add when workspace identity matters across machines. |
| `tbd enrich` | Only justified for expensive operations (LLM classification). Manual labels cover current needs. |
| Temporal pricing | Anthropic hasn't changed prices frequently enough to warrant the complexity. |
| Billing context | Requires solving "which workspace uses which billing" — deferred until API spend tracking is a real need. |

---

*The general principle: don't build automation until patterns emerge from real usage of the manual/explicit version.*
