# strata — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and semantic search.

## Current Focus

**Version 0.1.0 shipped.** 293 tests passing.

Landing unstaged Codex changes:
- Read-only database connections
- Tags drill-down + prefix filtering
- `builtin_queries/` package
- Doctor fix command update
- `docs/dev/` feedback docs

## Active Blockers

None.

## Key Decisions

| Topic | Reference |
|-------|-----------|
| Architecture (adapters vs storage) | `01KFMBEQRGX7` • `principles:architecture` |
| Exchange-window chunking | `strata ask -w strata "exchange window chunking"` |
| MMR diversity reranking | `01KG0EWYWQZR` • `strata ask -w strata "MMR diversity"` |
| Derivative conversation dedup | `strata ask -w strata "derivative auto-tag exclude"` |
| Tag boolean filtering (flags over expressions) | `strata ask -w strata "boolean tag filtering flags"` |
| CLI as thin dispatcher | `principles:cli` |
| Retrieval vs synthesis boundary | `strata ask -w strata "retrieval synthesis boundary"` |
| Team-scale SQLite analysis | `strata ask -w strata "team scale SQLite"` |

Full decision log: `strata query -l decision:` or search with `strata ask -w strata "decision rationale"`.

## Open Threads

| Thread | Status | Reference |
|--------|--------|-----------|
| SQL consolidation | Implemented, needs rebase | `strata ask -w strata "sql consolidation constants"` |
| FTS5 error handling | Open | `query -s` lacks error handling |
| `--json` everywhere | Open | status, tools, doctor lack JSON output |
| Embedding search perf | Open | O(n) cosine sim bottleneck |
| Derived ingest-time tags | Open | Generalize shell-tag pattern |

## History

| Date | Session | Summary |
|------|---------|---------|
| 2026-01-27 | `01KG14WC7XHC` | SQL architecture, derivative dedup, WhereBuilder, 293 tests |
| 2026-01-26 | `01KG0E1E7RXC` | Renamed tbd → strata, plugin/skill deployment |
| 2026-01-25 | `01KFV90WV2YC` | Agent usage analysis, plugin work |

Walk back further: `strata query -l handoff:update --since 2026-01`

## Bootstrap

New agent? Start here:
```bash
# Architecture overview
strata ask -w strata "architecture overview" --thread

# Key decisions
strata query -l decision:
strata query -l principles:architecture

# Recent work
strata ask -w strata "recent changes" --since 2026-01-20 --thread
```

## Run

```bash
# Install
uv pip install .

# Ingest
strata ingest

# Query
strata query -w .        # current workspace
strata ask "topic"       # semantic search

# Tests
uv run pytest tests/ -v
```

## See Also

- `docs/principles.md` — design principles with strata references
- `docs/search.md` — search pipeline, MMR, backends
- `docs/cli.md` — CLI reference (auto-generated)
- `README.md` — narrative introduction

---

*Pattern: docs as index, strata as source of truth. See `.subtask/tasks/research--strata-first-docs/DESIGN.md`.*
