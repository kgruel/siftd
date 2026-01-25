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
  - Hybrid retrieval: FTS5 recall → embeddings rerank (default mode)
  - Real token counts from fastembed tokenizer stored per chunk
  - Strategy metadata recorded in embeddings DB
  - Explicit `--index`/`--rebuild` required (no auto-build)
  - `--embed-db PATH` for alternate embeddings databases
  - Progressive disclosure: default snippets, `-v` full chunk, `--full` complete exchange, `--context N` surrounding exchanges, `--chrono` temporal sort
  - `--thread` two-tier narrative output: top conversations expanded with role-labeled exchanges, rest as compact shortlist
  - `--role user|assistant` filters by source role
  - `--first` returns chronologically earliest match above relevance threshold
  - `--conversations` aggregates per conversation (max/mean scores, ranked)
- **Query command**: composable conversation browser with filters, drill-down, and multiple output formats
  - Filters: `-w` workspace, `-m` model, `-t` tool, `-l` label, `-s` FTS5 search, `--since`/`--before`
  - Output: default (short, one-line with truncated ID), `-v` (full table), `--json`
  - Drill-down: `tbd query <id>` shows conversation timeline (prompts, responses, tool calls)
  - IDs: 12-char prefix, copy-pasteable for drill-down
  - SQL subcommand: `tbd query sql` lists `.sql` files, `tbd query sql <name>` runs them
- **CLI**: `ingest`, `status`, `query`, `label`, `labels`, `backfill`, `path`, `ask`
- **XDG paths**: data `~/.local/share/tbd`, config `~/.config/tbd`, queries `~/.config/tbd/queries`, adapters `~/.config/tbd/adapters`

### Benchmarking framework (`bench/`)
- **Corpus analysis**: `bench/corpus_analysis.py` — profiles token distribution using fastembed's tokenizer
- **Chunker**: `src/embeddings/chunker.py` — shared module with `chunk_text()` and `extract_exchange_window_chunks()`, used by both production `tbd ask` and bench
- **Strategies**: `bench/strategies/*.json` — `"strategy": "exchange-window"` (token-aware windowing) or legacy per-block
- **Build**: `bench/build.py --strategy <file>` — builds embeddings DB per strategy. Supports `--sample N` (conversation subset) and `--dry-run` (stats without embedding).
- **Runner**: `bench/run.py --strategy <file> <embed_db>...` — runs 25 queries, stores full chunk text + token counts in results
  - Presentation metrics: conversation diversity, temporal span, chrono degradation, cluster density
  - Retrieval dimensions: `--hybrid`, `--role user|assistant`, first-mention timestamps, conversation-level aggregation
  - All metrics emitted in structured JSON alongside score-based measures
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
    ├── fixtures/               # Minimal adapter test fixtures
    ├── test_adapters.py        # Adapter parsing tests (19 tests)
    ├── test_embeddings_storage.py  # Embeddings DB edge cases
    ├── test_models.py          # Model name parsing tests
    └── test_chunker.py         # Token-aware chunking smoke tests
```

### Release Status (0.1.0)
- **Version**: 0.1.0 — first stable release for personal use
- **Tests**: 30 passing (adapter fixtures, embeddings, models, chunker)
- **Install**: `uv pip install .` or `pip install .` from repo root
- **CLI**: `tbd` available after install, or `./tbd` from repo root

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
| `query` as primary interface | Composable flags for 80% case, `query sql` for power users (raw SQL) |
| Short mode as default | Dense one-liners with IDs; verbose table via `-v` |
| FTS5 via `query -s` | FTS5 composes with other filters instead of being a separate command |
| No auto-build on `ask` | Explicit `--index` required. Indexing is expensive, shouldn't surprise the user. |
| Remove untested adapters | Cline/Goose/Cursor/Aider had zero ingested data. Plugin system allows re-adding later. Recovery: commit `f5e3409`. |
| WIP branches for sessions | Session work (handoff updates, tests, scratch) goes in `wip/*`, subtasks merge to main. |
| Hybrid as default, not quality win | At ~5k conversations, FTS5 OR-mode hits recall limit on every query. Hybrid is a speed optimization for future scale, quality-neutral today. |
| Two-tier output (`--thread`) | Top 3-4 conversations (above-mean clusters) as narrative, rest as shortlist. Partition matches bench finding of 3.5 strong clusters per query. |
| Retrieval vs synthesis boundary | tbd owns deterministic structured retrieval (no LLM cost). Narrative synthesis is a consumer, not a feature. Manual-first principle applies. |
| Presentation metrics in bench | Diversity, temporal span, chrono degradation, cluster density alongside retrieval scores. Measures output shape, not just retrieval quality. |

---

## `tbd ask` — Current State

### Retrieval pipeline (resolved)
- **Model**: bge-small-en-v1.5 (384d), fastembed backend. bge-base (768d) was worse on this corpus.
- **Chunking**: exchange-window (prompt+response pairs, 256-token windows). 0% truncation, 86% in model sweet spot.
- **Hybrid retrieval**: FTS5 recall → embeddings rerank (default). FTS5 narrows candidates by vocabulary, embeddings score within candidates.
- **Bench finding**: Hybrid is quality-neutral at current corpus size (~5k conversations). FTS5 always hits the 80-conversation recall limit in OR-mode. Hybrid becomes a speed optimization as corpus grows past brute-force threshold.

### Presentation metrics (bench)
Measured on full corpus with exchange-window-256:

| Metric | Value | Interpretation |
|--------|-------|----------------|
| Unique Conversations | 7.7/10 | Results scatter across many conversations (not clustering) |
| Temporal Span | 18.0 days | Meaningful chronological spread — real arc across results |
| Chrono Degradation | 0.031 | Small cost to prefer timeline over best-score-first |
| Clusters Above Mean | 3.5 | ~half the conversations are "strong" hits (narrative backbone) |

### User feedback (archaeological research task)
From using `tbd ask` to reconstruct intellectual history across ~12 workspaces, ~2 months, hundreds of conversations (`experiments/docs/tbd-feedback.md`):

**What worked**: `-w` workspace filter (essential), `-v` verbose mode (workhorse), semantic queries finding conceptual matches (0.7+ = on-topic), chronological mode showing evolution.

**What didn't**: `--full` too noisy for research, query reformulation trial-and-error (~5-10 variations), result fragmentation across conversations, no "first mention" capability, no thread reconstruction.

**Key insight**: The gap between "search tool" and "cognitive context capture" is about *synthesis*. FTS5 + embeddings answer *content* questions ("find where we discussed X"). Missing: *shape* questions ("how did thinking about X evolve?"). The data supports shape queries, but the interface doesn't expose them yet.

### Design boundary: retrieval vs synthesis
- **tbd owns structured retrieval**: thread reconstruction, two-tier output, conversation-level ranking, role filtering. Deterministic, reproducible, no LLM cost.
- **Synthesis is a consumer of tbd's output**: LLM-generated narratives, topic evolution summaries, provenance trails. Opt-in, expensive, external.
- Keeps tbd as a data platform that exposes the right projections.

### Next direction: doc cross-reference

Project documentation (README, HANDOFF, RETROSPECTIVE) captures crystallized knowledge that originated in conversations. tbd has the raw conversations, but the crystallized form isn't cross-referenced back. Indexing workspace markdown alongside conversation chunks closes this gap:
- Concept found in docs but not conversations → doc chunk surfaces it
- Concept found in conversations → existing behavior
- Cross-workspace doc search becomes possible
- Implementation: discover markdown files during indexing, chunk and embed alongside conversation exchanges

---

## Next Session

**Cleaning**: cli.py structure, lint/type fixes, general tidying before next feature work.

---

## Remaining Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| Doc cross-reference | Next feature | Index workspace markdown (README, HANDOFF, RETROSPECTIVE) alongside conversation chunks. Closes "concept in docs" gap. |
| Synthesis layer | Design phase | LLM-generated narratives over structured retrieval output. Consumer of tbd, not part of it. |
| Relevance threshold | Trivial | `--threshold 0.65` to cut noise below a score. Score bands are meaningful (0.7+ on-topic, <0.6 noise). |
| cli.py structure | Acknowledged | Growing with new features. Not blocking. |
| Pricing table migration | Resolved | `ensure_pricing_table()` creates table idempotently on connection. Table exists in live DB. |
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
