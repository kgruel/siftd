# strata — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Current State

### What exists
- **Domain model**: `Conversation → Prompt → Response → ToolCall` dataclass tree (`src/domain/`)
- **Four adapters**: `claude_code` (file dedup), `gemini_cli` (session dedup), `codex_cli` (file dedup), `aider` (file dedup, markdown chat history)
- **Adapter plugin system**: built-in + drop-in (`~/.config/strata/adapters/*.py`) + entry points (`strata.adapters`)
- **Ingestion**: orchestration layer with adapter-controlled dedup, `--path` for custom dirs, `-a/--adapter` filter, `discover(locations=None)` delegation (adapters own their glob patterns), error recording (failed files tracked to prevent retry loops)
- **Storage**: SQLite with schema, ULIDs, schemaless attributes
- **Tool canonicalization**: 16 canonical tools (`file.read`, `shell.execute`, `shell.stdin`, etc.), cross-harness aliases
- **Model parsing**: raw names decomposed into family/version/variant/creator/released
- **Provider tracking**: derived from adapter's `HARNESS_SOURCE`, populated on responses during ingestion
- **Cache tokens**: `cache_creation_input_tokens`, `cache_read_input_tokens` extracted into `response_attributes`
- **Cost tracking**: flat `pricing` table (model+provider → rates), approximate cost via query-time JOIN
- **Tags**: full classification system with CRUD, boolean filtering, and composition with search
  - Apply: `strata tag <id> <tag>` (defaults to conversation), `--last N` for recent conversations
  - Remove: `strata tag --remove <id> <tag>`, composes with `--last`
  - Manage: `strata tags --rename OLD NEW`, `strata tags --delete NAME` (with `--force` guard)
  - Browse: `strata tags` (list with counts), `strata tags <name>` (drill-down to conversations), `--prefix` filtering
  - Visible in output: `strata query` list/detail/verbose/JSON all show tags
  - Prefix matching: trailing colon convention (`research:` matches `research:auth`, `research:perf`) on both conversation tags and tool tags
  - Boolean filtering on `query` and `ask`: multiple `-l` (OR), `--all-tags` (AND), `--no-tag` (NOT)
  - Tip shown after `strata ask` results to encourage tagging workflow
- **Shell command categorization**: 13 auto-tags (`shell:vcs`, `shell:test`, `shell:file`, etc.), 91% coverage of 25k+ commands
  - Auto-tagged at ingest time (no separate backfill needed for new data)
  - `strata backfill --shell-tags` still works for existing data
  - `strata query --tool-tag shell:test` filters conversations by tool_call tags
  - `strata tools` summary command with `--by-workspace` rollups
- **FTS5**: full-text search on prompt+response text content
- **Semantic search**: `strata ask` — embeddings in separate SQLite DB, fastembed backend, incremental indexing
  - Uses exchange-window chunking (token-aware, prompt+response pairs as atomic units)
  - Hybrid retrieval: FTS5 recall → embeddings rerank → **MMR diversity reranking** (default pipeline)
  - **MMR (Maximal Marginal Relevance)**: conversation-level penalty suppresses same-conversation duplicates, standard cosine penalty for cross-conversation diversity. λ=0.7 default, tunable via `--lambda`. Disable with `--no-diversity`.
  - Real token counts from fastembed tokenizer stored per chunk
  - Strategy metadata recorded in embeddings DB
  - Explicit `--index`/`--rebuild` required (no auto-build)
  - `--embed-db PATH` for alternate embeddings databases
  - Progressive disclosure: default snippets, `-v` full chunk, `--full` complete exchange, `--context N` surrounding exchanges, `--chrono` temporal sort
  - `--thread` two-tier narrative output: top conversations expanded with role-labeled exchanges, rest as compact shortlist
  - `-l`/`--tag` filters by conversation tag (repeatable OR, prefix matching), `--all-tags` (AND), `--no-tag` (NOT)
  - `--role user|assistant` filters by source role
  - `--first` returns chronologically earliest match above relevance threshold
  - `--conversations` aggregates per conversation (max/mean scores, ranked)
  - `--threshold SCORE` filters results below relevance score (0.7+ = on-topic, <0.6 = noise)
- **Query command**: composable conversation browser with filters, drill-down, and multiple output formats
  - Filters: `-w` workspace, `-m` model, `-t` tool, `-l` tag (repeatable, OR), `--all-tags` (AND), `--no-tag` (NOT), `-s` FTS5 search, `--since`/`--before`
  - Output: default (short, one-line with truncated ID), `-v` (full table), `--json`, `--stats` (summary totals)
  - Drill-down: `strata query <id>` shows conversation timeline with collapsed tool calls (e.g., `→ shell.execute ×47`)
  - IDs: 12-char prefix, copy-pasteable for drill-down
  - SQL subcommand: `strata query sql` lists `.sql` files, `strata query sql <name>` runs them
  - Root workspaces display as `(root)` instead of blank
- **Live session inspection** (`strata peek`): Reads raw JSONL session files directly from disk, bypassing SQLite
  - `strata peek` — list active sessions (last 2 hours by default, `--all` for everything)
  - `strata peek <id>` — detail view with exchange timeline (prompt text, response text, tool calls, tokens)
  - `-w SUBSTR` workspace filter, `--last N` exchange count, `--tail` raw JSONL tail, `--json` structured output
  - Prefix matching on session ID (like `strata query <id>`)
  - Uses adapter `DEFAULT_LOCATIONS` to find files; mtime for "active" detection
  - Self-contained `peek/` package: `scanner.py` (discovery + metadata), `reader.py` (detail parsing)
- **CLI**: `ingest`, `status`, `query`, `tag` (apply/remove), `tags` (list/drill-down/rename/delete), `backfill`, `path`, `ask`, `adapters`, `copy`, `tools`, `doctor`, `peek`
  - `strata adapters` — list discovered adapters (built-in, drop-in, entry point)
  - `strata copy adapter <name>` — copy built-in adapter to config for customization
  - `strata copy query <name>` — copy built-in query to config
  - `strata doctor` — health checks and maintenance
- **Health checks** (`strata doctor`): Pluggable diagnostics with fix suggestions
  - `strata doctor` — run all checks
  - `strata doctor checks` — list available checks
  - `strata doctor fixes` — show fix commands for issues
  - Built-in checks: `ingest-pending`, `ingest-errors`, `embeddings-stale`, `pricing-gaps`, `drop-ins-valid`
  - API: `list_checks()`, `run_checks()`, `apply_fix()` in `strata.api`
- **Library API** (`strata.api`): Programmatic access to all CLI functionality
  - `list_conversations()`, `get_conversation()` — conversation queries (boolean tag filters: `tags`, `all_tags`, `exclude_tags`, plus `tool_tag`)
  - `hybrid_search()`, `aggregate_by_conversation()`, `first_mention()` — semantic search
  - `get_stats()` → `DatabaseStats` — database statistics
  - `get_tool_tag_summary()`, `get_tool_tags_by_workspace()` — tool tag analytics
  - `list_query_files()`, `run_query_file()` — SQL query execution
  - `list_adapters()`, `copy_adapter()`, `copy_query()` — adapter/resource management
  - `list_checks()`, `run_checks()`, `apply_fix()` — health checks (doctor)
  - `list_active_sessions()`, `read_session_detail()`, `tail_session()`, `find_session_file()` — live session inspection (peek)
- **OutputFormatter pattern** (`strata.output`): Pluggable presentation for `strata ask`
  - `ChunkListFormatter`, `VerboseFormatter`, `FullExchangeFormatter`, `ContextFormatter`, `ThreadFormatter`, `ConversationFormatter`, `JsonFormatter`
  - `--json` flag for structured output (bench/agent consumption)
  - `--format NAME` for explicit formatter selection
  - Drop-in plugins: `~/.config/strata/formatters/*.py` + `strata.formatters` entry points
  - `select_formatter(args)` dispatch based on CLI flags
- **Claude Code plugin** (`plugin/`): Agent DX layer for strata
  - Hooks: session-start (remind after compaction), skill-reminder (detect "strata" mentions), skill-required (detect raw `strata` commands)
  - Bundled skill: `plugin/skills/strata/SKILL.md` — progressive disclosure (core → output → filtering → preserving)
  - Reference docs: `plugin/skills/strata/reference/` — full feature set for `ask`, `query`, and `tags`
  - Install: `claude --plugin-dir plugin/` (dev) or via marketplace (global)
- **XDG paths**: data `~/.local/share/strata`, config `~/.config/strata`, queries `~/.config/strata/queries`, adapters `~/.config/strata/adapters`, formatters `~/.config/strata/formatters`

### Benchmarking framework (`bench/`)
- **Corpus analysis**: `bench/corpus_analysis.py` — profiles token distribution using fastembed's tokenizer
- **Chunker**: `src/embeddings/chunker.py` — shared module with `chunk_text()` and `extract_exchange_window_chunks()`, used by both production `strata ask` and bench
- **Strategies**: `bench/strategies/*.json` — `"strategy": "exchange-window"` (token-aware windowing) or legacy per-block
- **Build**: `bench/build.py --strategy <file>` — builds embeddings DB per strategy. Supports `--sample N` (conversation subset) and `--dry-run` (stats without embedding).
- **Runner**: `bench/run.py --strategy <file> <embed_db>...` — runs 50 queries, stores full chunk text + token counts in results
  - Presentation metrics: conversation diversity, temporal span, chrono degradation, cluster density
  - **Diversity metrics**: conversation redundancy, unique workspace count, pairwise similarity, cross-query overlap (Jaccard for broad-then-narrow pairs)
  - Retrieval dimensions: `--hybrid`, `--role user|assistant`, first-mention timestamps, conversation-level aggregation
  - **Reranking modes**: `--rerank mmr|relevance` for A/B comparison, `--lambda` for MMR tuning
  - All metrics emitted in structured JSON alongside score-based measures
- **Viewer**: `bench/view.py <run.json> [--html]` — stdout summary or self-contained HTML report with score-coded cards, opens in browser. Displays diversity metrics alongside retrieval scores.
- **Queries**: `bench/queries.json` — 50 queries across 10 groups (conceptual, philosophical, technical, specific, exploratory, cross-workspace, broad-then-narrow, temporal-trace, tagged-subset, research-workflow)

### Data (current ingestion)
- 5,656+ conversations, 165k responses, 89k tool calls across 303 workspaces
- ~960MB database at `~/.local/share/strata/strata.db`
- Harnesses: Claude Code (Anthropic), Codex CLI (OpenAI), Gemini CLI (Google), Cline (Anthropic)
- Models: Opus 4.5, Haiku 4.5, Sonnet 4.5, Gemini 3 pro/flash, GPT-5.2
- Top workspace: `~/.config` (1,114 conversations)

### Files
```
strata/
├── README.md                   # Comprehensive documentation (600+ lines)
├── plugin/
│   ├── .claude-plugin/
│   │   └── plugin.json         # Plugin metadata
│   ├── hooks/
│   │   └── hooks.json          # Event hook definitions
│   ├── scripts/
│   │   ├── session-start.sh    # Remind about strata after compaction/resume
│   │   ├── skill-reminder.sh   # Detect "strata" mentions in prompts
│   │   └── skill-required.sh   # Detect raw strata commands in Bash
│   └── skills/
│       └── strata/
│           ├── SKILL.md        # Progressive disclosure skill (core → output → filter → preserve)
│           └── reference/
│               ├── ask.md      # Full strata ask reference
│               ├── query.md    # Full strata query reference
│               └── tags.md     # Full tag management reference
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
│   ├── search.py               # Hybrid search orchestration + MMR diversity reranking
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
│   │   ├── file_refs.py        # fetch_file_refs() for tool results
│   │   └── peek.py             # list_active_sessions(), read_session_detail(), etc.
│   ├── output/                 # Presentation layer (OutputFormatter pattern)
│   │   ├── __init__.py         # Re-exports formatters
│   │   ├── formatters.py       # ChunkList, Verbose, Full, Context, Thread, Conversation, Json formatters
│   │   └── registry.py         # Formatter plugin discovery (drop-in + entry points)
│   ├── adapters/
│   │   ├── __init__.py         # Adapter exports
│   │   ├── registry.py         # Plugin discovery + wrap_adapter_paths() + discover(locations) delegation
│   │   ├── aider.py            # Markdown parser, chat history sessions, cost extraction
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
│   ├── doctor/
│   │   ├── __init__.py         # Re-exports
│   │   ├── checks.py           # Check protocol + 5 built-in checks
│   │   └── runner.py           # list_checks(), run_checks(), apply_fix()
│   ├── peek/
│   │   ├── __init__.py         # Re-exports
│   │   ├── scanner.py          # SessionInfo, list_active_sessions()
│   │   └── reader.py           # PeekExchange, SessionDetail, read_session_detail(), tail_session()
│   └── storage/
│       ├── schema.sql          # Full schema + FTS5 + pricing table
│       ├── sqlite.py           # All DB operations, backfills, tag CRUD (apply/remove/rename/delete)
│       └── embeddings.py       # Embeddings DB schema + cosine similarity search
└── tests/
    ├── fixtures/               # Minimal adapter test fixtures
    ├── test_adapters.py        # Adapter parsing tests
    ├── test_api.py             # API layer tests (conversations, stats, search)
    ├── test_doctor.py          # Health check tests (23 tests)
    ├── test_embeddings_storage.py  # Embeddings DB edge cases
    ├── test_models.py          # Model name parsing tests
    ├── test_peek.py            # Live session inspection tests (27 tests)
    └── test_chunker.py         # Token-aware chunking smoke tests
```

### Release Status (0.1.0)
- **Version**: 0.1.0 — first stable release for personal use
- **Tests**: 183 passing (adapters, API, config, doctor, formatters, embeddings, ingestion, models, peek, chunker)
- **Install**: `uv pip install .` or `pip install .` from repo root
- **CLI**: `strata` available after install
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
| Flag conventions over expression syntax for boolean tags | Multiple `-l` (OR), `--all-tags` (AND), `--no-tag` (NOT). CLI-idiomatic, no parser, shell-safe. Expression language deferred unless flag approach proves insufficient. |
| Trailing-colon prefix convention | `research:` matches `research:auth`, `research:perf`. Follows existing namespace convention naturally. Applied to both conversation tags and tool tags. |
| `query` as primary interface | Composable flags for 80% case, `query sql` for power users (raw SQL) |
| Short mode as default | Dense one-liners with IDs; verbose table via `-v` |
| FTS5 via `query -s` | FTS5 composes with other filters instead of being a separate command |
| No auto-build on `ask` | Explicit `--index` required. Indexing is expensive, shouldn't surprise the user. |
| Remove untested adapters | Goose/Cursor had zero ingested data at time of removal. Cline and Aider now re-added as built-in. Plugin system supports drop-in for others. |
| WIP branches for sessions | Session work (handoff updates, tests, scratch) goes in `wip/*`, subtasks merge to main. |
| Hybrid as default, not quality win | At ~5k conversations, FTS5 OR-mode hits recall limit on every query. Hybrid is a speed optimization for future scale, quality-neutral today. |
| Two-tier output (`--thread`) | Top 3-4 conversations (above-mean clusters) as narrative, rest as shortlist. Partition matches bench finding of 3.5 strong clusters per query. |
| Retrieval vs synthesis boundary | strata owns deterministic structured retrieval (no LLM cost). Narrative synthesis is a consumer, not a feature. Manual-first principle applies. |
| Presentation metrics in bench | Diversity, temporal span, chrono degradation, cluster density alongside retrieval scores. Measures output shape, not just retrieval quality. |
| MMR as default reranking | Conversation-level penalty (1.0 for same-conv, cosine sim for cross-conv). λ=0.7 balances relevance and diversity. FTS5 pre-filtering regresses diversity; MMR fixes that and exceeds pure similarity on every diversity metric. |
| Plugin over skill for agent DX | Hooks (session-start, prompt-submit, post-tool-use) provide active nudges. Skill bundled inside plugin for single-source distribution. |
| "Tag" over "label" | Shorter, fits tool vibe. Renamed before extending to tool_calls. |
| Shell tags via tool_call_tags | Same join-table pattern as conversation/workspace tags. Namespaced `shell:*` to separate auto from manual. |
| CLI as thin dispatcher | Business logic in `strata.api`, presentation in `strata.output`. CLI is ~500 lines of argparse + routing. |
| OutputFormatter protocol | Pluggable presentation for `strata ask`. Each output mode (`--thread`, `--context`, etc.) is a formatter class. |
| Library API re-exports | Top-level `from strata import ...` exposes public functions. CLI is just one consumer. |
| `strata copy` for customization | Copy built-in adapters/queries to config dir for modification. Same-name overrides built-in. |
| Flat kwargs over filter objects | `list_conversations(workspace=..., model=...)` not `list_conversations(filter=Filter(...))`. Dataclasses for return types only. |
| Formatter plugins like adapters | Same three-tier discovery: built-in < entry point < drop-in. Drop-ins can override built-ins. |
| Ingest-time tagging (inline) | Shell tags applied during `store_conversation()`, not hooks. General hook pattern deferred until second auto-tag use case emerges. |
| Pre-commit hook local-only | `.git/hooks/pre-commit` regenerates CLI docs. Not versioned — too little benefit for portability overhead. |
| Tag hierarchy rejected | Empirical analysis of 25k+ commands showed single-tool dominance (git 85% of vcs, pytest 98% of test). Flat tags sufficient; query-time parsing handles finer granularity. |
| TOML for config | Human-editable, dotfile-friendly, tomlkit preserves comments on write. SQLite rejected — mixes concerns, not inspectable. |
| Config get/set over edit | `strata config get/set` more ergonomic than `$EDITOR`. Programmatic access enables scripting. |
| `peek` bypasses SQLite | Live reads from raw JSONL, no ingestion latency. mtime for "active" detection (simple, cross-platform). |
| Error column on `ingested_files` | NULL = success, non-NULL = error message. Same-hash files skip (same content → same error). Hash change clears record and retries. Simpler than a separate failures table. |
| Skip empty conversations at adapter | JSONL files with only `file-history-snapshot` records (no user/assistant messages) yield no conversation. Filter at parse boundary, not downstream. |

---

## `strata ask` — Current State

### Retrieval pipeline (resolved)
- **Model**: bge-small-en-v1.5 (384d), fastembed backend. bge-base (768d) was worse on this corpus.
- **Chunking**: exchange-window (prompt+response pairs, 256-token windows). 0% truncation, 86% in model sweet spot.
- **Hybrid retrieval**: FTS5 recall → embeddings rerank → **MMR diversity reranking** (default pipeline). FTS5 narrows candidates by vocabulary, embeddings score within candidates, MMR selects for diversity. Tag filters compose with all other filters.
- **MMR**: conversation-level penalty (λ=0.7). Fetches 3x candidates, greedily selects for relevance + diversity. Trades ~0.024 avg score for +20% unique conversations, +35% unique workspaces, -29% redundancy.
- **Bench finding**: FTS5 pre-filtering without MMR regresses diversity (candidate pool narrows). MMR fixes that regression and exceeds pure similarity on every diversity metric. Hybrid+MMR is the best configuration.

### Bench comparison: search strategy evolution
Three-way comparison on full corpus (50 queries, 10 groups):

| Metric | Pure Similarity | Hybrid FTS5+Rerank | Hybrid+MMR (default) |
|--------|----------------|-------------------|----------------------|
| Avg Score | 0.7319 | 0.7218 | 0.7082 |
| Conv Redundancy | 0.1460 | 0.2040 | **0.1040** |
| Unique Conversations | 8.3 | 6.9 | **9.9** |
| Unique Workspaces | 4.9 | 4.3 | **6.6** |
| Temporal Span (days) | 24.4 | 22.7 | **34.1** |
| Topic Clusters | 3.8 | 3.4 | **4.7** |

**Key findings**: MMR trades ~0.024 avg score for substantially better diversity across every metric. FTS5 pre-filtering without MMR actually regresses diversity (narrows candidate pool). MMR fixes that regression and exceeds pure similarity. All scores remain above 0.70 threshold.

### User feedback (archaeological research task)
From using `strata ask` to reconstruct intellectual history across ~12 workspaces, ~2 months, hundreds of conversations (`experiments/docs/tbd-feedback.md`):

**What worked**: `-w` workspace filter (essential), `-v` verbose mode (workhorse), semantic queries finding conceptual matches (0.7+ = on-topic), chronological mode showing evolution.

**What didn't**: `--full` too noisy for research, query reformulation trial-and-error (~5-10 variations), result fragmentation across conversations, no "first mention" capability, no thread reconstruction.

**Key insight**: The gap between "search tool" and "cognitive context capture" is about *synthesis*. FTS5 + embeddings answer *content* questions ("find where we discussed X"). Missing: *shape* questions ("how did thinking about X evolve?"). The data supports shape queries, but the interface doesn't expose them yet.

### Design boundary: retrieval vs synthesis
- **strata owns structured retrieval**: thread reconstruction, two-tier output, conversation-level ranking, role filtering. Deterministic, reproducible, no LLM cost.
- **Synthesis is a consumer of strata's output**: LLM-generated narratives, topic evolution summaries, provenance trails. Opt-in, expensive, external.
- Keeps strata as a data platform that exposes the right projections.

### Agent usage analysis (2026-01-25)

Analyzed real strata usage by agents in non-strata workspaces:

**Usage found**:
- `/Code/experiments`: 16+ `strata ask` queries researching architecture concepts
- `/Code/rill`: 17 queries researching testing philosophy
- `/Code/cells`: 14 queries

**Observed pattern**:
1. `strata --help` or `strata ask --help` — discover capabilities
2. `strata ask "semantic query" --since --workspace --full -n` — iterative search
3. `strata query <id>` — drill into specific conversations
4. **No tagging** — agents never use `strata tag` to mark useful results

**Sample queries from experiments**:
- "framework projection stream events fold spec"
- "JSONL file broker offset byte tailer persistence"
- "prefer integration tests fakes over mocks"

**Gap identified**: Agents find useful content but have no workflow to mark it for later retrieval. `strata tag` exists but agents don't know about it.

**Addressed (2026-01-25)**: Progressive help epilog now teaches the search → refine → save workflow. Tip after results explains WHY to tag. Active session exclusion prevents circular results (derivative content outranking originals). Remaining gap: provenance marking for ingested derivative content (conversations containing `strata ask` tool calls) — deferred until active exclusion proves insufficient.

---

## Next Session

**Recently completed (this session)**:

- **Claude Code plugin** (`plugin/`): hooks + bundled skill + reference docs
  - Three hooks: session-start (compaction/resume), skill-reminder (detect "strata" mentions), skill-required (detect raw commands)
  - SKILL.md redesigned for progressive disclosure: core → output → filtering → preserving. Behavioral combinations inline, reference files for full feature set.
  - Reference docs: `ask.md`, `query.md`, `tags.md` — every flag with examples
  - Motivated by STRATA_FEEDBACK finding: agents never discovered tagging, `--thread`, or workspace filtering from the old skill
- **MMR diversity reranking** — new default search strategy
  - Two-tier conversation-level penalty: 1.0 for same-conversation (hard suppress), cosine sim for cross-conversation
  - λ=0.7 default, `--lambda` tunable, `--no-diversity` opt-out for pure relevance
  - Bench validates: +20% unique conversations, +35% unique workspaces, -29% redundancy, +40% temporal span
  - FTS5 pre-filtering regresses diversity; MMR fixes that and exceeds pure similarity on every diversity metric
- **Bench update**: 50 queries across 10 groups (5 new: cross-workspace, broad-then-narrow, temporal-trace, tagged-subset, research-workflow)
  - Diversity metrics: conversation redundancy, unique workspace count, pairwise similarity, cross-query overlap
  - `--rerank mmr|relevance` for A/B comparison
  - Three-way bench run: pure similarity vs hybrid FTS5 vs hybrid+MMR
- **Prior session**: Tag ergonomics overhaul (boolean filtering, rename/delete, prefix matching), doctor cleanup, aider adapter

**Potential directions**:

- **Plugin installation**: Set up global install via marketplace (currently dev-mode `--plugin-dir` only)
- **Pairwise similarity in bench**: Currently N/A — embeddings stripped before diversity metrics compute. Minor fix to retain embeddings through the metrics pass.
- **Session-aware dedup**: MMR handles intra-query diversity. Cross-query dedup (across sequential queries in a research session) is partially addressed by removing skill fork mode, but no automated mechanism yet.
- **Drop-in checks**: `~/.config/strata/checks/*.py` for user-defined health checks. Pattern exists, add when needed.

**Lower priority**:

- **Synthesis layer**: LLM-generated narratives over structured retrieval output. Consumer of strata, not part of it.
- **Doc cross-reference**: Embedding docs alongside conversations. Unclear if concepts-only-in-docs is a real use case.

---

## Remaining Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| Doc cross-reference | Deferred | Embedding docs alongside conversations. Deferred — unclear if concepts-only-in-docs is a real use case. `--refs` covers files referenced in conversations. |
| Synthesis layer | Design phase | LLM-generated narratives over structured retrieval output. Consumer of strata, not part of it. |
| `workspaces.git_remote` | Deferred | Could resolve via `git remote -v`. Not blocking queries yet. |
| `strata enrich` | Deferred | Only justified for expensive ops (LLM-based labeling). |
| Billing context | Deferred | API vs subscription per workspace. Needed for precise cost, not approximate. |
| Provenance marking | Deferred | Tag ingested conversations containing `strata ask` tool calls as `strata:derivative`, down-weight in search. Detectable from shell.execute tool call data. Deferred — active session exclusion may be sufficient. |
| Re-add adapters | Partially done | Cline and Aider re-added as built-in. Goose/Cursor still available at commit `f5e3409` if needed. |

---

## Key Dependencies
- `fastembed` 0.7.4 — embedding + tokenizer (bundled, model-agnostic)
- `tokenizers` 0.22.2 — HuggingFace Rust tokenizer (fastembed dependency)
- `tomlkit` 0.14.0 — TOML parsing with comment preservation (for config)

---

*Updated: 2026-01-27 (MMR diversity reranking, Claude Code plugin with bundled skill + reference docs, bench update with 50 queries and diversity metrics)*
*Origin: Redesign from tbd-v1, see `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md`*
