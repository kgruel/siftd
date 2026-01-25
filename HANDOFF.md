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
- **Tags**: manual tagging via CLI (`tbd tag`), conversation/workspace/tool_call scopes
- **Shell command categorization**: 13 auto-tags (`shell:vcs`, `shell:test`, `shell:file`, etc.), 91% coverage of 25k+ commands
  - Auto-tagged at ingest time (no separate backfill needed for new data)
  - `tbd backfill --shell-tags` still works for existing data
  - `tbd query --tool-tag shell:test` filters conversations by tool_call tags
  - `tbd tools` summary command with `--by-workspace` rollups
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
  - `--threshold SCORE` filters results below relevance score (0.7+ = on-topic, <0.6 = noise)
- **Query command**: composable conversation browser with filters, drill-down, and multiple output formats
  - Filters: `-w` workspace, `-m` model, `-t` tool, `-l` tag, `-s` FTS5 search, `--since`/`--before`
  - Output: default (short, one-line with truncated ID), `-v` (full table), `--json`, `--stats` (summary totals)
  - Drill-down: `tbd query <id>` shows conversation timeline with collapsed tool calls (e.g., `→ shell.execute ×47`)
  - IDs: 12-char prefix, copy-pasteable for drill-down
  - SQL subcommand: `tbd query sql` lists `.sql` files, `tbd query sql <name>` runs them
  - Root workspaces display as `(root)` instead of blank
- **CLI**: `ingest`, `status`, `query`, `tag`, `tags`, `backfill`, `path`, `ask`, `adapters`, `copy`, `tools`
  - `tbd adapters` — list discovered adapters (built-in, drop-in, entry point)
  - `tbd copy adapter <name>` — copy built-in adapter to config for customization
  - `tbd copy query <name>` — copy built-in query to config
- **Library API** (`tbd.api`): Programmatic access to all CLI functionality
  - `list_conversations()`, `get_conversation()` — conversation queries (supports `tool_tag` filter)
  - `hybrid_search()`, `aggregate_by_conversation()`, `first_mention()` — semantic search
  - `get_stats()` → `DatabaseStats` — database statistics
  - `get_tool_tag_summary()`, `get_tool_tags_by_workspace()` — tool tag analytics
  - `list_query_files()`, `run_query_file()` — SQL query execution
  - `list_adapters()`, `copy_adapter()`, `copy_query()` — adapter/resource management
- **OutputFormatter pattern** (`tbd.output`): Pluggable presentation for `tbd ask`
  - `ChunkListFormatter`, `VerboseFormatter`, `FullExchangeFormatter`, `ContextFormatter`, `ThreadFormatter`, `ConversationFormatter`, `JsonFormatter`
  - `--json` flag for structured output (bench/agent consumption)
  - `--format NAME` for explicit formatter selection
  - Drop-in plugins: `~/.config/tbd/formatters/*.py` + `tbd.formatters` entry points
  - `select_formatter(args)` dispatch based on CLI flags
- **XDG paths**: data `~/.local/share/tbd`, config `~/.config/tbd`, queries `~/.config/tbd/queries`, adapters `~/.config/tbd/adapters`, formatters `~/.config/tbd/formatters`

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
- 5,697 conversations, 154k responses, 83k tool calls across 287 workspaces
- ~900MB database at `~/.local/share/tbd/tbd.db`
- Harnesses: Claude Code (Anthropic), Codex CLI (OpenAI), Gemini CLI (Google)
- Models: Opus 4.5, Haiku 4.5, Sonnet 4.5, Gemini 3 pro/flash, GPT-5.2
- Top workspace: `~/.config` (1,167 conversations)

### Files
```
tbd-v2/
├── README.md                   # Comprehensive documentation (600+ lines)
├── docs/
│   ├── cli.md                  # Auto-generated CLI reference
│   └── config.md               # Configuration file documentation
├── scripts/
│   └── gen-cli-docs.sh         # Regenerates docs/cli.md from --help
├── queries/
│   ├── cost.sql                # Approximate cost by workspace
│   └── shell-analysis.sql      # Shell command granularity analysis (tag breakdown, git actions, read/write)
├── bench/
│   ├── queries.json            # 25 benchmark queries (5 groups)
│   ├── corpus_analysis.py      # Token distribution profiling
│   ├── run.py                  # Benchmark runner
│   ├── build.py                # Strategy-based embeddings DB builder
│   ├── view.py                 # Run viewer: stdout summary or HTML report
│   ├── strategies/             # Strategy definitions (exchange-window, per-block)
│   └── runs/                   # Benchmark output (gitignored)
├── src/
│   ├── cli.py                  # Argparse + thin command handlers (dispatcher only)
│   ├── paths.py                # XDG directory handling
│   ├── models.py               # Model name parser
│   ├── search.py               # Hybrid search orchestration
│   ├── domain/
│   │   ├── models.py           # Dataclasses (Conversation, Prompt, Response, etc.)
│   │   ├── protocols.py        # Adapter/Storage protocols
│   │   └── source.py           # Source(kind, location, metadata)
│   ├── api/                    # Public library API
│   │   ├── __init__.py         # Re-exports all public functions
│   │   ├── conversations.py    # list_conversations(), get_conversation(), query files
│   │   ├── stats.py            # get_stats() → DatabaseStats
│   │   ├── search.py           # hybrid_search(), aggregate_by_conversation(), first_mention()
│   │   ├── tools.py            # get_tool_tag_summary(), get_tool_tags_by_workspace()
│   │   ├── adapters.py         # list_adapters() → AdapterInfo
│   │   ├── resources.py        # copy_adapter(), copy_query()
│   │   └── file_refs.py        # fetch_file_refs() for tool results
│   ├── output/                 # Presentation layer (OutputFormatter pattern)
│   │   ├── __init__.py         # Re-exports formatters
│   │   ├── formatters.py       # ChunkList, Verbose, Full, Context, Thread, Conversation, Json formatters
│   │   └── registry.py         # Formatter plugin discovery (drop-in + entry points)
│   ├── adapters/
│   │   ├── __init__.py         # Adapter exports
│   │   ├── registry.py         # Plugin discovery + wrap_adapter_paths()
│   │   ├── claude_code.py      # JSONL parser, TOOL_ALIASES, cache token extraction
│   │   ├── codex_cli.py        # JSONL parser, OpenAI Codex sessions
│   │   └── gemini_cli.py       # JSON parser, session dedup, discover()
│   ├── embeddings/
│   │   ├── __init__.py         # Re-exports get_backend, build_embeddings_index
│   │   ├── base.py             # EmbeddingBackend protocol + fallback chain resolver
│   │   ├── chunker.py          # Exchange-window chunking + token-aware splitting
│   │   ├── indexer.py          # build_embeddings_index() → IndexStats
│   │   ├── ollama_backend.py   # Local Ollama embedding models
│   │   └── fastembed_backend.py # Local ONNX inference via fastembed
│   ├── ingestion/
│   │   ├── discovery.py        # discover_all()
│   │   └── orchestration.py    # ingest_all(), IngestStats, dedup strategies
│   └── storage/
│       ├── schema.sql          # Full schema + FTS5 + pricing table
│       ├── sqlite.py           # All DB operations, backfills, tag functions
│       └── embeddings.py       # Embeddings DB schema + cosine similarity search
└── tests/
    ├── fixtures/               # Minimal adapter test fixtures
    ├── test_adapters.py        # Adapter parsing tests
    ├── test_api.py             # API layer tests (conversations, stats, search)
    ├── test_embeddings_storage.py  # Embeddings DB edge cases
    ├── test_models.py          # Model name parsing tests
    └── test_chunker.py         # Token-aware chunking smoke tests
```

### Release Status (0.1.0)
- **Version**: 0.1.0 — first stable release for personal use
- **Tests**: 101 passing (adapters, API, formatters, embeddings, models, chunker)
- **Install**: `uv pip install .` or `pip install .` from repo root
- **CLI**: `tbd` available after install, or `./tbd` from repo root
- **Pre-commit hook**: Auto-regenerates `docs/cli.md` (local-only, not versioned)

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
| Manual tags first, auto when patterns emerge | Shell categorization justified by 25k+ calls. LLM-based classification still deferred. |
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
| "Tag" over "label" | Shorter, fits tool vibe. Renamed before extending to tool_calls. |
| Shell tags via tool_call_tags | Same join-table pattern as conversation/workspace tags. Namespaced `shell:*` to separate auto from manual. |
| CLI as thin dispatcher | Business logic in `tbd.api`, presentation in `tbd.output`. CLI is ~500 lines of argparse + routing. |
| OutputFormatter protocol | Pluggable presentation for `tbd ask`. Each output mode (`--thread`, `--context`, etc.) is a formatter class. |
| Library API re-exports | Top-level `from tbd import ...` exposes public functions. CLI is just one consumer. |
| `tbd copy` for customization | Copy built-in adapters/queries to config dir for modification. Same-name overrides built-in. |
| Flat kwargs over filter objects | `list_conversations(workspace=..., model=...)` not `list_conversations(filter=Filter(...))`. Dataclasses for return types only. |
| Formatter plugins like adapters | Same three-tier discovery: built-in < entry point < drop-in. Drop-ins can override built-ins. |
| Ingest-time tagging (inline) | Shell tags applied during `store_conversation()`, not hooks. General hook pattern deferred until second auto-tag use case emerges. |
| Pre-commit hook local-only | `.git/hooks/pre-commit` regenerates CLI docs. Not versioned — too little benefit for portability overhead. |
| Tag hierarchy rejected | Empirical analysis of 25k+ commands showed single-tool dominance (git 85% of vcs, pytest 98% of test). Flat tags sufficient; query-time parsing handles finer granularity. |
| TOML for config | Human-editable, dotfile-friendly, tomlkit preserves comments on write. SQLite rejected — mixes concerns, not inspectable. |
| Config get/set over edit | `tbd config get/set` more ergonomic than `$EDITOR`. Programmatic access enables scripting. |

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

### Agent usage analysis (2026-01-25)

Analyzed real tbd usage by agents in non-tbd workspaces:

**Usage found**:
- `/Code/experiments`: 16+ `tbd ask` queries researching architecture concepts
- `/Code/rill`: 17 queries researching testing philosophy
- `/Code/cells`: 14 queries

**Observed pattern**:
1. `tbd --help` or `tbd ask --help` — discover capabilities
2. `tbd ask "semantic query" --since --workspace --full -n` — iterative search
3. `tbd query <id>` — drill into specific conversations
4. **No tagging** — agents never use `tbd tag` to mark useful results

**Sample queries from experiments**:
- "framework projection stream events fold spec"
- "JSONL file broker offset byte tailer persistence"
- "prefer integration tests fakes over mocks"

**Gap identified**: Agents find useful content but have no workflow to mark it for later retrieval. `tbd tag` exists but agents don't know about it.

---

## Next Session

**Ready for implementation (subtasks with approved plans)**:

- **tagging-ergonomics**: Code changes to improve discoverability (~60 LOC total)
  - Footer hint in `tbd ask` output: "Tip: Tag useful results → tbd tag <id> <tag>"
  - Default entity_type to `conversation` (simplify syntax)
  - `--last` flag to tag recent conversations

- **tag-after-search-docs**: Documentation for agent tag workflow
  - Update `tbd tag --help` with examples, workflow, conventions
  - CLAUDE.md template section for other projects
  - Tag namespace conventions: `research:*`, `useful:*`, `review:*`
  - Workflow documentation: search → drill-down → tag → retrieve

**Recently completed**:

- **User-configurable formatters**: TOML config at `~/.config/tbd/config.toml`, `tbd config get/set` syntax. Using tomlkit for comment-preserving read/write.
- **Ingest fix**: Added `~/.config/claude/projects` to claude_code adapter DEFAULT_LOCATIONS. Was only discovering `~/.claude/projects` (123 files), missing 5,268 files in the config location.
- **Tagging leakage fix**: Moved sed/awk from `shell:search` to `shell:file`, added `uv run ruff` pattern to `shell:lint`.

**Potential directions**:

- **`tbd copy formatter`**: Copy built-in formatter to config for customization. Lower priority — config solves the common case of "different defaults."

**Lower priority**:

- **Synthesis layer**: LLM-generated narratives over structured retrieval output. Consumer of tbd, not part of it.
- **Doc cross-reference**: Embedding docs alongside conversations. Unclear if concepts-only-in-docs is a real use case.

---

## Remaining Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| Doc cross-reference | Deferred | Embedding docs alongside conversations. Deferred — unclear if concepts-only-in-docs is a real use case. `--refs` covers files referenced in conversations. |
| Synthesis layer | Design phase | LLM-generated narratives over structured retrieval output. Consumer of tbd, not part of it. |
| `workspaces.git_remote` | Deferred | Could resolve via `git remote -v`. Not blocking queries yet. |
| `tbd enrich` | Deferred | Only justified for expensive ops (LLM-based labeling). |
| Billing context | Deferred | API vs subscription per workspace. Needed for precise cost, not approximate. |
| Re-add adapters | When needed | Cline/Goose/Cursor/Aider at commit `f5e3409`. Plugin system supports drop-in. |

---

## Key Dependencies
- `fastembed` 0.7.4 — embedding + tokenizer (bundled, model-agnostic)
- `tokenizers` 0.22.2 — HuggingFace Rust tokenizer (fastembed dependency)
- `tomlkit` 0.14.0 — TOML parsing with comment preservation (for config)

---

*Updated: 2026-01-25 (config system merged, ingest fix for ~/.config/claude/projects, tagging leakage fix, usage analysis completed, tag-after-search docs in progress)*
*Origin: Redesign from tbd-v1, see `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md`*
