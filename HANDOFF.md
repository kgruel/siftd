# strata — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and semantic search.

## Current Focus

**Version 0.1.0 shipped.** Core features stable.

Recent additions:
- `strata export` command for PR review workflows
- Optional embeddings via `strata[embed]`
- Strata-first documentation pattern (this HANDOFF)
- API-first skill + `/strata` quick search modes

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
| Strata-first docs pattern | `strata ask -w strata "strata-first documentation"` |
| Optional embeddings | `strata ask -w strata "optional embeddings strata[embed]"` |
| Export for PR workflows | `strata ask -w strata "export prompts PR review"` |
| Team via push/pull (not shared DB) | `strata ask -w strata "team scale push pull"` |

Full decision log: `strata query -l decision:` or search with `strata ask -w strata "decision rationale"`.

## Open Threads

| Thread | Status | Reference |
|--------|--------|-----------|
| FTS5 error handling | Open | `query -s` lacks error handling |
| `--json` everywhere | Open | status, tools, doctor lack JSON output |
| Embedding search perf | Open | O(n) cosine sim bottleneck |
| Derived ingest-time tags | Open | Generalize shell-tag pattern |
| Export git linking | Deferred | Link sessions to branches/commits/PRs |
| Export import format | Deferred | Portable `.strata` for reviewer import |
| Plugin `--tools` mode | Deferred | Tool usage pattern analysis |

## History

| Date | Session | Summary |
|------|---------|---------|
| 2026-01-28 | `01KG19R9S1P2` | Export command, optional embeddings, strata-first docs pattern |
| 2026-01-27 | `01KG14WC7XHC` | SQL architecture, derivative dedup, WhereBuilder, 293 tests |
| 2026-01-26 | `01KG0E1E7RXC` | Renamed tbd → strata, plugin/skill deployment |

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
