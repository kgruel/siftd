# siftd — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and semantic search.

## Current Focus

**Release v0.1.0** — rename to `siftd` complete.

Next steps:
- Tag v0.1.0
- Configure PyPI trusted publishing for siftd
- Push and publish

Recent additions:
- CI workflows (test + lint on PR, publish on tag)
- Pre-commit hook with ty + ruff
- Type errors resolved
- Release review completed (no blockers)

## Active Blockers

None.

## Key Decisions

| Topic | Reference |
|-------|-----------|
| Architecture (adapters vs storage) | `01KFMBEQRGX7` • `principles:architecture` |
| Exchange-window chunking | `siftd ask -w siftd "exchange window chunking"` |
| MMR diversity reranking | `01KG0EWYWQZR` • `siftd ask -w siftd "MMR diversity"` |
| Derivative conversation dedup | `siftd ask -w siftd "derivative auto-tag exclude"` |
| Tag boolean filtering (flags over expressions) | `siftd ask -w siftd "boolean tag filtering flags"` |
| CLI as thin dispatcher | `principles:cli` |
| Retrieval vs synthesis boundary | `siftd ask -w siftd "retrieval synthesis boundary"` |
| Team-scale SQLite analysis | `siftd ask -w siftd "team scale SQLite"` |
| Siftd-first docs pattern | `siftd ask -w siftd "siftd-first documentation"` |
| Optional embeddings | `siftd ask -w siftd "optional embeddings siftd[embed]"` |
| Export for PR workflows | `siftd ask -w siftd "export prompts PR review"` |
| Team via push/pull (not shared DB) | `siftd ask -w siftd "team scale push pull"` |

Full decision log: `siftd query -l decision:` or search with `siftd ask -w siftd "decision rationale"`.

## Open Threads

| Thread | Status | Reference |
|--------|--------|-----------|
| `--json` everywhere | Open | status, tools, doctor lack JSON output |
| Built-in query examples | Open | `builtin_queries/` is empty |
| CLI test coverage | Open | `path`, `config` commands untested at CLI level |
| Embedding search perf | Deferred | O(n) cosine sim — acceptable for v0.1.0 |
| FTS5 error handling | Deferred | Graceful degradation approach is fine |
| Derived ingest-time tags | Deferred | Generalize shell-tag pattern • `context:derived-tags` |
| Export git linking | Deferred | Link sessions to branches/commits/PRs |
| Export import format | Deferred | Portable `.siftd` for reviewer import |

## History

| Date | Session | Summary |
|------|---------|---------|
| 2026-01-28 | (pending tag) | Rename strata → siftd complete; ready for v0.1.0 release |
| 2026-01-28 | `01KG19R9S1P2` | Export command, optional embeddings, siftd-first docs pattern |
| 2026-01-27 | `01KG14WC7XHC` | SQL architecture, derivative dedup, WhereBuilder, 293 tests |
| 2026-01-26 | `01KG0E1E7RXC` | Renamed tbd → siftd, plugin/skill deployment |

Walk back further: `siftd query -l handoff:update --since 2026-01`

## Bootstrap

New agent? Start here:
```bash
# Architecture overview
siftd ask -w siftd "architecture overview" --thread

# Key decisions
siftd query -l decision:
siftd query -l principles:architecture

# Recent work
siftd ask -w siftd "recent changes" --since 2026-01-20 --thread
```

## Run

```bash
# Install
uv pip install .

# Ingest
siftd ingest

# Query
siftd query -w .        # current workspace
siftd ask "topic"       # semantic search

# Tests
uv run pytest tests/ -v
```

## See Also

- `docs/cli.md` — CLI reference (auto-generated)
- `README.md` — project introduction
- `.subtask/tasks/review--release-prep/REPORT.md` — v0.1.0 release review

---

*Pattern: docs as index, siftd as source of truth. See `.subtask/tasks/research--siftd-first-docs/DESIGN.md`.*
