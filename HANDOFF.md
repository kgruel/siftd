# tbd-v2 — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Current State

### What exists
- **Domain model**: `Conversation → Prompt → Response → ToolCall` dataclass tree (`src/domain/`)
- **Three adapters**: `claude_code` (file dedup), `gemini_cli` (session dedup), `codex_cli` (file dedup)
- **Adapter plugin system**: built-in + drop-in (`~/.config/tbd/adapters/*.py`) + entry points (`tbd.adapters`)
- **Ingestion**: orchestration layer with adapter-controlled dedup, `--path` for custom dirs
- **Storage**: SQLite with schema, ULIDs, schemaless attributes
- **Tool canonicalization**: 16 canonical tools (`file.read`, `shell.execute`, `shell.stdin`, etc.), cross-harness aliases
- **Model parsing**: raw names decomposed into family/version/variant/creator/released
- **Provider tracking**: derived from adapter's `HARNESS_SOURCE`, populated on responses during ingestion
- **Cache tokens**: `cache_creation_input_tokens`, `cache_read_input_tokens` extracted into `response_attributes`
- **Cost tracking**: flat `pricing` table (model+provider → rates), approximate cost via query-time JOIN
- **Labels**: manual labeling via CLI (`tbd label`), conversation and workspace scopes
- **FTS5**: full-text search on prompt+response text content
- **Semantic search**: `tbd ask` — embeddings in separate SQLite DB, fastembed backend, incremental indexing
  - Uses exchange-window chunking (token-aware, prompt+response pairs as atomic units)
  - Real token counts from fastembed tokenizer stored per chunk
  - Strategy metadata recorded in embeddings DB
  - Explicit `--index`/`--rebuild` required (no auto-build)
  - `--embed-db PATH` for alternate embeddings databases
- **Logs command**: composable conversation browser with filters, drill-down, and multiple output formats
  - Filters: `-w` workspace, `-m` model, `-t` tool, `-l` label, `-q` FTS5 search, `--since`/`--before`
  - Output: default (short, one-line with truncated ID), `-v` (full table), `--json`
  - Drill-down: `tbd logs <id>` shows conversation timeline (prompts, responses, tool calls)
  - IDs: 12-char prefix, copy-pasteable for drill-down
- **Query runner**: `.sql` files in `~/.config/tbd/queries/`, `$var` substitution, missing var detection
- **CLI**: `ingest`, `status`, `search`, `logs`, `queries`, `label`, `labels`, `backfill`, `path`, `ask`
- **XDG paths**: data `~/.local/share/tbd`, config `~/.config/tbd`, queries `~/.config/tbd/queries`, adapters `~/.config/tbd/adapters`

### Benchmarking framework (`bench/`)
- **Corpus analysis**: `bench/corpus_analysis.py` — profiles token distribution using fastembed's tokenizer
- **Chunker**: `src/embeddings/chunker.py` — shared module with `chunk_text()` and `extract_exchange_window_chunks()`, used by both production `tbd ask` and bench
- **Strategies**: `bench/strategies/*.json` — `"strategy": "exchange-window"` (token-aware windowing) or legacy per-block
- **Build**: `bench/build.py --strategy <file>` — builds embeddings DB per strategy. Supports `--sample N` (conversation subset) and `--dry-run` (stats without embedding).
- **Runner**: `bench/run.py --strategy <file> <embed_db>...` — runs 25 queries, stores full chunk text + token counts in results
- **Viewer**: `bench/view.py <run.json> [--html]` — stdout summary or self-contained HTML report with score-coded cards, opens in browser
- **Queries**: `bench/queries.json` — 25 queries across 5 groups (conceptual, philosophical, technical, specific, exploratory)

### Data (current ingestion)
- ~5,289 conversations, 135k+ responses, 68k+ tool calls across 270+ workspaces
- ~710MB database at `~/.local/share/tbd/tbd.db`
- Harnesses: Claude Code (Anthropic), Codex CLI (OpenAI), Gemini CLI (Google)
- Models: Opus 4.5, Haiku 4.5, Sonnet 4.5, Gemini 3 pro/flash, GPT-5.2
- Top workspace: `gruel.network` (~700 conversations, 68M tokens)

### Files
```
tbd-v2/
├── tbd                         # CLI entry point
├── queries/
│   └── cost.sql                # Approximate cost by workspace
├── bench/
│   ├── queries.json            # 25 benchmark queries (5 groups)
│   ├── corpus_analysis.py      # Token distribution profiling
│   ├── run.py                  # Benchmark runner
│   ├── build.py                # Strategy-based embeddings DB builder
│   ├── view.py                 # Run viewer: stdout summary or HTML report
│   ├── strategies/             # Strategy definitions (exchange-window, per-block)
│   └── runs/                   # Benchmark output (gitignored)
├── src/
│   ├── cli.py                  # argparse commands
│   ├── paths.py                # XDG directory handling
│   ├── models.py               # Model name parser
│   ├── domain/
│   │   ├── models.py           # Dataclasses (Conversation, Prompt, Response, etc.)
│   │   ├── protocols.py        # Adapter/Storage protocols
│   │   └── source.py           # Source(kind, location, metadata)
│   ├── adapters/
│   │   ├── __init__.py         # Adapter exports
│   │   ├── registry.py         # Plugin discovery (built-in + drop-in + entry points)
│   │   ├── claude_code.py      # JSONL parser, TOOL_ALIASES, cache token extraction
│   │   ├── codex_cli.py        # JSONL parser, OpenAI Codex sessions
│   │   └── gemini_cli.py       # JSON parser, session dedup, discover()
│   ├── embeddings/
│   │   ├── __init__.py         # Re-exports get_backend
│   │   ├── base.py             # EmbeddingBackend protocol + fallback chain resolver
│   │   ├── chunker.py          # Exchange-window chunking + token-aware splitting (shared by cli + bench)
│   │   ├── ollama_backend.py   # Local Ollama embedding models
│   │   └── fastembed_backend.py # Local ONNX inference via fastembed
│   ├── ingestion/
│   │   ├── discovery.py        # discover_all()
│   │   └── orchestration.py    # ingest_all(), IngestStats, dedup strategies
│   └── storage/
│       ├── schema.sql          # Full schema + FTS5 + pricing table
│       ├── sqlite.py           # All DB operations, backfills, label functions
│       └── embeddings.py       # Embeddings DB schema + cosine similarity search
└── tests/
    ├── test_models.py          # Model name parsing tests
    └── test_chunker.py         # Token-aware chunking smoke tests (skipped without fastembed)
```

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| `commit=False` default | Caller controls transaction boundaries |
| Adapter-controlled dedup | Claude/Codex=file (one convo per file), Gemini=session (latest wins) |
| Adapters pluggable, storage not | Adapters are plural and stateless; storage is the gravity well (SQL files, FTS5, attributes) |
| Module-level adapter conventions | Adapters are stateless parsers — constants + functions, no class instantiation needed |
| Hybrid plugin discovery | Drop-in dir for iteration, entry points for packaging — same pattern as queries |
| Domain objects as dataclasses | Simple, no ORM, protocol-based interfaces |
| FTS5 for text search | Native SQLite, no deps, prompt+response text only (skip thinking/tool_use) |
| Embeddings in separate DB | Expensive to compute, treat as persistent derived data, keep main DB clean |
| Brute-force cosine similarity | Correct first; ANN (faiss/hnswlib) only if corpus grows past ~100k chunks |
| Fastembed (bge-small-en-v1.5) | Best-performing model tested. bge-base (768d) was worse on this corpus. |
| Exchange-window chunking | Prompt+response pairs as atomic units, accumulated to 256-token windows. Solved token distribution (0% truncation, 86% in model sweet spot). |
| Built-in sentence/word splitting | Dropped `semantic-text-splitter` dependency. Oversized exchanges are rare with exchange-window; naive splitting suffices. |
| Queries as .sql files | User-extensible, `string.Template` for var substitution |
| Tool canonicalization | Aliases enable cross-harness queries, unknown tools still tracked |
| Model parsing at ingest | Regex decomposition, structured fields enable family/variant queries |
| Cost at query time | No stored redundancy, immediate price updates, pricing table JOIN |
| Attributes for variable metadata | Avoids schema sprawl for provider-specific fields |
| Approximate cost, labeled explicitly | Flat pricing is useful now; precision deferred until billing context matters |
| Manual labels first | Auto-classification deferred until usage patterns justify LLM cost |
| `logs` as primary interface | Composable flags for 80% case, `queries` stays for power users (raw SQL) |
| Short mode as default | Dense one-liners with IDs; verbose table via `-v` |
| `search` folded into `logs -q` | FTS5 composes with other filters instead of being a separate command |
| No auto-build on `ask` | Explicit `--index` required. Indexing is expensive, shouldn't surprise the user. |
| Remove untested adapters | Cline/Goose/Cursor/Aider had zero ingested data. Plugin system allows re-adding later. Recovery: commit `f5e3409`. |
| WIP branches for sessions | Session work (handoff updates, tests, scratch) goes in `wip/*`, subtasks merge to main. |

---

## `tbd ask` — Resolved

### Embedding model evaluation (complete)
Tested bge-small-en-v1.5 (384d) and bge-base-en-v1.5 (768d) with exchange-window chunking on 500-conversation sample:

| Metric | Baseline (per-block) | EW bge-small | EW bge-base |
|--------|---------------------|-------------|-------------|
| Avg Score | 0.6702 | 0.6754 | 0.6319 |
| Variance | 0.001050 | 0.001142 | 0.001163 |
| Spread | 0.0326 | 0.0364 | 0.0401 |

**Conclusion**: Variance ~0.001 regardless of model or chunking. Brute-force cosine similarity on general-purpose embeddings cannot meaningfully discriminate relevance for conversation fragments. bge-small outperforms bge-base on this corpus. Exchange-window is the correct chunking strategy.

### Current production state
- `tbd ask --index`: uses exchange-window chunking via shared `src/embeddings/chunker.py`
- Model: bge-small-en-v1.5 (384d), fastembed backend
- Token counts: real (from fastembed tokenizer, not word splits)
- Strategy metadata stored in embeddings DB

### Next direction: hybrid retrieval + narrative output

The ceiling is architectural, not model/chunking. Next approach: FTS5 recall + embeddings reranking.
- Use FTS5 keyword search to pull candidate conversations (good at vocabulary recall)
- Rerank chunks within candidates using cosine similarity
- Bench pipeline (`build → run → view`) can prototype this with a new strategy mode

**User feedback from real usage** (searching project history):
- FTS5 literal matching required ~10 query variations to find 3-4 relevant conversations
- Once found, results were immediately useful (architecture visions, problem diagnoses)
- Missing: workspace scoping on search, chronological ordering, full exchange context
- Meta-gap: "grep over conversations" vs "knowledge retrieval" — the tool finds fragments when the user wants narrative
- Key insight: presentation matters as much as retrieval. A good result is 3-4 exchanges across 2-3 conversations, chronologically ordered, with enough context to see the arc.
- Note: `logs -q` already composes FTS5 with `-w` workspace filter — discoverability issue?

**Quick wins before hybrid**:
1. Chronological sort flag on search/ask results
2. Exchange-level context in output (not just matching snippet)
3. Verify `logs -q -w` is discoverable for workspace-scoped search

---

## Remaining Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| Hybrid retrieval (`tbd ask` v2) | Next feature | FTS5 recall + embeddings reranking. Different architecture. Bench pipeline ready for prototyping. |
| cli.py structure | Acknowledged | 1166 lines, 10 commands, inconsistent DB access. Not blocking. |
| Provider semantics | Resolved by adapter removal | 3 remaining adapters all map correctly (anthropic, openai, google). |
| Pricing table migration | Open | Schema defines `pricing` table but it doesn't exist in live DB. Needs migration or re-create. |
| `workspaces.git_remote` | Deferred | Could resolve via `git remote -v`. Not blocking queries yet. |
| `tbd enrich` | Deferred | Only justified for expensive ops (LLM-based labeling). |
| Billing context | Deferred | API vs subscription per workspace. Needed for precise cost, not approximate. |
| Re-add adapters | When needed | Cline/Goose/Cursor/Aider at commit `f5e3409`. Plugin system supports drop-in. |

---

## Key Dependencies
- `fastembed` 0.7.4 — embedding + tokenizer (bundled, model-agnostic)
- `tokenizers` 0.22.2 — HuggingFace Rust tokenizer (fastembed dependency)

---

*Updated: 2026-01-24*
*Origin: Redesign from tbd-v1, see `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md`*
