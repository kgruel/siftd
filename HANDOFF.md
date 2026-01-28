# strata — Handoff

Personal LLM usage analytics. Ingests conversation logs from CLI coding tools, stores in SQLite, queries via FTS5 and user-defined SQL files.

## Current State

### What exists
- **Domain model**: `Conversation → Prompt → Response → ToolCall` dataclass tree (`src/domain/`)
- **Four adapters**: `claude_code` (file dedup), `gemini_cli` (session dedup), `codex_cli` (file dedup), `aider` (file dedup, markdown chat history)
- **Adapter plugin system**: built-in + drop-in (`~/.config/strata/adapters/*.py`) + entry points (`strata.adapters`)
- **Ingestion**: orchestration layer with adapter-controlled dedup, `--path` for custom dirs, `-a/--adapter` filter, `discover(locations=None)` delegation (adapters own their glob patterns), error recording (failed files tracked to prevent retry loops)
- **Storage**: SQLite with schema, ULIDs, schemaless attributes. Modular: `sqlite.py` (817 lines, core primitives), `tags.py` (tag CRUD), `fts.py` (FTS5 subsystem), `embeddings.py` (separate DB). Backfills extracted to top-level `backfill.py`.
- **Shared utilities**: `ids.py` (ULID generation), `math.py` (cosine similarity), `adapters/_jsonl.py` (shared JSONL parsing), `domain/shell_categories.py` (command categorization), `domain/tags.py` (tag SQL builder)
- **Tool canonicalization**: 16 canonical tools (`file.read`, `shell.execute`, `shell.stdin`, etc.), cross-harness aliases
- **Model parsing**: raw names decomposed into family/version/variant/creator/released
- **Provider tracking**: derived from adapter's `HARNESS_SOURCE`, populated on responses during ingestion
- **Cache tokens**: `cache_creation_input_tokens`, `cache_read_input_tokens` extracted into `response_attributes`
- **Cost tracking**: flat `pricing` table (model+provider → rates), approximate cost via query-time JOIN
- **Tags**: full classification system with CRUD, boolean filtering, and composition with search
  - Apply: `strata tag <id> <tag> [tag2 ...]` (bulk application, defaults to conversation), `--last N` for recent conversations
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
- **CLI**: `ingest`, `status`, `query`, `tag` (apply/remove/bulk), `tags` (list/drill-down/rename/delete), `backfill`, `path`, `ask`, `adapters`, `copy`, `tools`, `doctor`, `peek`
  - `cli.py` (1361 lines): argparse + 13 command handlers. `cli_ask.py` (342 lines): `cmd_ask` extracted — the only command complex enough to warrant its own file.
  - `strata adapters` — list discovered adapters (built-in, drop-in, entry point)
  - `strata copy adapter <name>` — copy built-in adapter to config for customization
  - `strata copy query <name>` — copy built-in query to config
  - `strata doctor` — health checks and maintenance
- **Health checks** (`strata doctor`): Pluggable diagnostics with fix suggestions
  - `strata doctor` — run all checks
  - `strata doctor checks` — list available checks
  - `strata doctor fixes` — show fix commands for issues
  - Built-in checks: `ingest-pending`, `ingest-errors`, `embeddings-stale`, `pricing-gaps`, `drop-ins-valid`, `orphaned-chunks`
  - Checks declare `has_fix = True/False` as class attribute (no runtime probing)
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
  - Marketplace install: `claude plugin marketplace add kaygee/strata` + `claude plugin install strata@strata`
  - Dev mode: `claude --plugin-dir plugin/`
  - `.claude-plugin/marketplace.json` at repo root for marketplace distribution
- **XDG paths**: data `~/.local/share/strata`, config `~/.config/strata`, queries `~/.config/strata/queries`, adapters `~/.config/strata/adapters`, formatters `~/.config/strata/formatters`

### Bench (`bench/`) — workbench for retrieval tinkering
- **Corpus analysis**: `bench/corpus_analysis.py` — profiles token distribution using fastembed's tokenizer
- **Chunker**: `src/embeddings/chunker.py` — shared module with `chunk_text()` and `extract_exchange_window_chunks()`, used by both production `strata ask` and bench
- **Strategy**: `bench/strategies/exchange-window.json` — token-aware windowing (the only strategy)
- **Build**: `bench/build.py --strategy <file>` — builds embeddings DB from strategy
- **Runner**: `bench/run.py --strategy <file> <embed_db>...` — runs 50 queries against embeddings DBs
  - Diversity metrics: conversation redundancy, unique workspace count
  - `--hybrid` for FTS5 recall before reranking, `--rerank mmr|relevance`, `--lambda` for MMR tuning
- **Viewer**: `bench/view.py <run.json> [--html]` — stdout summary or self-contained HTML report
- **Queries**: `bench/queries.json` — 50 queries across 10 groups

### Data (current ingestion)
- 5,656+ conversations, 165k responses, 89k tool calls across 303 workspaces
- ~960MB database at `~/.local/share/strata/strata.db`
- Harnesses: Claude Code (Anthropic), Codex CLI (OpenAI), Gemini CLI (Google), Cline (Anthropic)
- Models: Opus 4.5, Haiku 4.5, Sonnet 4.5, Gemini 3 pro/flash, GPT-5.2
- Top workspace: `~/.config` (1,114 conversations)

### Files
```
strata/
├── README.md                   # Narrative introduction (160 lines: problem → search → agents → memory)
├── .claude-plugin/
│   └── marketplace.json        # Marketplace distribution manifest
├── plugin/
│   ├── .claude-plugin/
│   │   └── plugin.json         # Plugin metadata (author, hooks, skills paths)
│   ├── README.md               # Plugin install guide (marketplace + dev mode)
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
│               ├── ask.md      # Full strata ask reference (incl. --lambda, --no-diversity)
│               ├── query.md    # Full strata query reference
│               └── tags.md     # Full tag management reference
├── docs/
│   ├── cli.md                  # Auto-generated CLI reference (regenerated, uses "strata" not "tbd")
│   ├── config.md               # Configuration file documentation
│   ├── search.md               # Search pipeline, MMR, backends, bench
│   ├── tags.md                 # Tag system, boolean filtering, conventions, workflow
│   ├── api.md                  # Library API reference
│   ├── data-model.md           # Schema, tables, cost model, IDs
│   ├── adapters.md             # Built-in + drop-in + entry point adapter authoring
│   ├── queries.md              # SQL queries with examples
│   └── plugin.md               # Claude Code plugin (marketplace install, hooks, skill)
├── scripts/
│   └── gen-cli-docs.sh         # Regenerates docs/cli.md from --help
├── queries/
│   ├── cost.sql                # Approximate cost by workspace
│   └── shell-analysis.sql      # Shell command granularity analysis (tag breakdown, git actions, read/write)
├── bench/
│   ├── queries.json            # 50 queries (10 groups)
│   ├── corpus_analysis.py      # Token distribution profiling
│   ├── run.py                  # Query runner
│   ├── build.py                # Embeddings DB builder
│   ├── view.py                 # Results viewer (stdout or HTML)
│   ├── strategies/             # exchange-window.json
│   └── runs/                   # Run output (gitignored)
├── src/
│   ├── cli.py                  # Argparse + 13 command handlers (1361 lines)
│   ├── cli_ask.py              # cmd_ask + helpers, extracted (342 lines)
│   ├── backfill.py             # Backfill operations (moved out of storage/)
│   ├── ids.py                  # ULID generation (shared utility)
│   ├── math.py                 # Cosine similarity (shared utility)
│   ├── paths.py                # XDG directory handling
│   ├── models.py               # Model name parser
│   ├── search.py               # Hybrid search orchestration + MMR diversity reranking
│   ├── domain/
│   │   ├── models.py           # Dataclasses (Conversation, Prompt, Response, etc.)
│   │   ├── shell_categories.py # Shell command categorization (extracted from storage)
│   │   ├── tags.py             # Tag condition SQL builder (shared utility)
│   │   └── source.py           # Source(kind, location, metadata) with .as_path property
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
│   │   ├── _jsonl.py           # Shared JSONL utilities (load, now_iso, parse_block)
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
│   │   ├── checks.py           # Check protocol + 6 built-in checks (has_fix declarative)
│   │   └── runner.py           # list_checks(), run_checks(), apply_fix()
│   ├── peek/
│   │   ├── __init__.py         # Re-exports
│   │   ├── scanner.py          # SessionInfo, list_active_sessions()
│   │   └── reader.py           # PeekExchange, SessionDetail, read_session_detail(), tail_session()
│   └── storage/
│       ├── schema.sql          # Full schema + FTS5 + pricing table
│       ├── sqlite.py           # Core DB operations (817 lines: connection, entities, write, read, dedup)
│       ├── tags.py             # Tag CRUD (get_or_create, apply, remove, rename, delete, list)
│       ├── fts.py              # FTS5 (ensure, rebuild, insert, search, recall)
│       └── embeddings.py       # Embeddings DB schema + cosine similarity search + orphan pruning
├── docs/
│   ├── principles.md           # Project principles catalog with strata conversation references
│   ├── cli.md                  # Auto-generated CLI reference
│   ├── config.md               # Configuration file documentation
│   ├── search.md               # Search pipeline, MMR, backends, bench
│   ├── tags.md                 # Tag system, boolean filtering, conventions, workflow
│   ├── api.md                  # Library API reference
│   ├── data-model.md           # Schema, tables, cost model, IDs
│   ├── adapters.md             # Built-in + drop-in + entry point adapter authoring
│   ├── queries.md              # SQL queries with examples
│   └── plugin.md               # Claude Code plugin (marketplace install, hooks, skill)
└── tests/
    ├── conftest.py             # Shared fixtures (test_db, make_conversation, FIXTURES_DIR)
    ├── fixtures/               # Minimal adapter test fixtures
    ├── test_adapters.py        # Adapter parsing tests (parametrized)
    ├── test_api.py             # API layer tests (conversations, stats, search)
    ├── test_cli.py             # CLI smoke tests (--help, status, query, bulk tag)
    ├── test_config.py          # Config load/get/set tests
    ├── test_doctor.py          # Health check tests
    ├── test_embeddings_storage.py  # Embeddings DB tests
    ├── test_exclude_active.py  # Active session exclusion tests
    ├── test_formatters.py      # Output formatter tests
    ├── test_ingestion.py       # Ingest pipeline tests
    ├── test_integration.py     # End-to-end: ingest→query→stats, FTS5, store round-trip
    ├── test_models.py          # Model name parsing (parametrized)
    ├── test_peek.py            # Live session inspection tests
    ├── test_shell_categorization.py  # Shell command categorization (15 categories, parametrized)
    └── test_chunker.py         # Token-aware chunking tests
```

### Release Status (0.1.0)
- **Version**: 0.1.0 — first stable release for personal use
- **Tests**: 262 passing, 1 skipped (fastembed optional). Integration-first, shared fixtures, parametrized, xdist-safe.
- **Install**: `uv pip install .` or `pip install .` from repo root
- **CLI**: `strata` available after install
- **Plugin**: `claude plugin marketplace add kaygee/strata` for Claude Code integration
- **Pre-commit hook**: Auto-regenerates `docs/cli.md` (local-only, not versioned)
- **Docs**: README (narrative) + 10 reference docs in `docs/` (including `principles.md`) + plugin README

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
| Bench as workbench, not benchmark | Directional signal tool for tinkering with retrieval strategies. Not rigorous evaluation — stripped to core metrics (scores + conversation redundancy + workspace count). |
| MMR as default reranking | Conversation-level penalty (1.0 for same-conv, cosine sim for cross-conv). λ=0.7 balances relevance and diversity. FTS5 pre-filtering regresses diversity; MMR fixes that and exceeds pure similarity on every diversity metric. |
| Plugin over skill for agent DX | Hooks (session-start, prompt-submit, post-tool-use) provide active nudges. Skill bundled inside plugin for single-source distribution. |
| "Tag" over "label" | Shorter, fits tool vibe. Renamed before extending to tool_calls. |
| Shell tags via tool_call_tags | Same join-table pattern as conversation/workspace tags. Namespaced `shell:*` to separate auto from manual. |
| CLI as thin dispatcher | Business logic in `strata.api`, presentation in `strata.output`. CLI is argparse + routing. |
| Extract only what strains the format | `cmd_ask` is the only command complex enough (210 lines, multi-stage pipeline) for its own file. Don't pre-decompose 13 stable thin wrappers. |
| Extract by concern, not by entity | Storage split into tags/FTS/backfill subsystems, not per-table modules. Captures actual seams. |
| Backfills are operations, not storage | Backfill functions use storage but aren't storage primitives. Moved out to fix storage→adapter dependency. |
| No repository pattern | "Functions that take `conn`" is already the thin interface. Only one implementation, no polymorphism needed. |
| Shared utilities over inline duplication | ULID, cosine sim, JSONL parsing, tag SQL — extracted when found in 2+ locations. Zero-dependency utility modules. |
| UTC timestamps everywhere | Standardized from naive local time. `datetime.now(timezone.utc).isoformat()` across all adapters. |
| Principles as tagged semantic index | Project principles documented in `docs/principles.md` with strata conversation references. Tagged `principles:*` for retrieval via `strata query -l principles:architecture`. |
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
| Narrative README over reference README | README tells a story: problem → data → personal search → agent search → self-teaching → institutional memory. Reference content extracted to `docs/`. README is introductory; docs are comprehensive. |
| Synthesis = tagging, not LLM | Tags encode human/agent judgment without LLM cost. The data exposure (MMR, `--thread`, plugin skill) is the synthesis enabler. LLM-generated narratives deferred — let agents test the workflow first. |
| Marketplace for plugin distribution | `.claude-plugin/marketplace.json` at repo root. `claude plugin marketplace add` + `claude plugin install` for global install. Dev mode still available via `--plugin-dir`. |

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

**Recently completed (this session — release prep cleanup)**:

- **Bench release prep** — stripped 7 unused features (-741 lines), reframed from "benchmark" to "workbench", bench now imports production code instead of reimplementing
- **Test suite overhaul** — integration-first restructure: conftest.py with shared fixtures, parametrized tests, end-to-end ingest→query test, FTS5 search test, CLI smoke tests, shell categorization coverage. 179 → 262 tests.
- **Code cleanup** — extracted 5 shared modules (ids.py, math.py, _jsonl.py, shell_categories.py, domain/tags.py), removed dead code, fixed architectural violations (FTS cleanup on delete, storage→adapter decoupling, timezone standardization to UTC)
- **Storage refactoring** — sqlite.py split: tags.py (tag CRUD), fts.py (FTS5 subsystem), backfill.py (moved out of storage entirely, fixes architecture). sqlite.py: 1327 → 817 lines.
- **CLI refactoring** — cmd_ask extracted to cli_ask.py (342 lines). cli.py: 1690 → 1361 lines. Only the one complex command warranted extraction.
- **Doctor improvements** — declarative `has_fix` on check classes (no runtime probing), orphaned chunks check + `prune_orphaned_chunks()` function
- **Bulk tag application** — `strata tag <id> tag1 tag2 tag3` applies multiple tags in one call
- **Principles catalog** — `docs/principles.md` with categorized principles extracted from dev history via strata ask, tagged with `principles:*` for semantic retrieval
- **ROADMAP.md** — updated stale references (label→tag, adapter list, line counts, test infrastructure done)
- **Prior session**: documentation overhaul, plugin marketplace install, cleanup fixes, bench pairwise fix, synthesis decision, Claude Code plugin, MMR diversity reranking, tag ergonomics, aider adapter

**Open threads for next session**:

- **SQL query duplication in formatters** — prompt/response extraction SQL repeated 4 times across formatters.py and chunker.py. Extract to shared query helper.
- **Storage Option B** — if sqlite.py grows past ~1000 lines, split further (connection/migrations/entities/write/read). Seams identified in research/storage PLAN.md.
- **Bench hybrid comparison** — run comparing hybrid vs pure-embeddings on current corpus
- **MMR test coverage** — default search strategy has zero tests. `mmr_rerank()`, `--lambda`, `--no-diversity` all untested.
- **README polish** — narrative expanded this session, may want a final pass
- **Principles adherence audit** — use `docs/principles.md` to review codebase for violations (the original purpose of the principles extraction)
- **Session-aware dedup implementation** — plan exists. Cross-query dedup for sequential research queries.

**Lower priority**:

- **Drop-in checks**: `~/.config/strata/checks/*.py` for user-defined health checks. Pattern exists, add when needed.
- **Observe agent behavior**: Plugin/skill deployed. Watch whether agents discover tagging, `--thread`, workspace filtering.
- **Synthesis layer**: LLM-generated narratives over structured retrieval output. Consumer of strata, not part of it.
- **Doc cross-reference**: Embedding docs alongside conversations. Unclear if real use case.

---

## Remaining Open Threads

| Thread | Status | Notes |
|--------|--------|-------|
| Doc cross-reference | Deferred | Embedding docs alongside conversations. Deferred — unclear if concepts-only-in-docs is a real use case. `--refs` covers files referenced in conversations. |
| Synthesis layer | Deferred | Current synthesis = tagging. LLM-generated narratives are a consumer of strata, not part of it. Deferred — let agents test improved plugin/skill workflow first. |
| `workspaces.git_remote` | Deferred | Could resolve via `git remote -v`. Not blocking queries yet. |
| `strata enrich` | Deferred | Only justified for expensive ops (LLM-based labeling). |
| Billing context | Deferred | API vs subscription per workspace. Needed for precise cost, not approximate. |
| Provenance marking | Deferred | Tag ingested conversations containing `strata ask` tool calls as `strata:derivative`, down-weight in search. Detectable from shell.execute tool call data. Deferred — active session exclusion may be sufficient. |
| Re-add adapters | Partially done | Aider built-in. Goose/Cursor available via plugin system. |
| SQL duplication in formatters | Open | Prompt/response extraction SQL repeated 4 times across formatters.py and chunker.py. |

---

## Key Dependencies
- `fastembed` 0.7.4 — embedding + tokenizer (bundled, model-agnostic)
- `tokenizers` 0.22.2 — HuggingFace Rust tokenizer (fastembed dependency)
- `tomlkit` 0.14.0 — TOML parsing with comment preservation (for config)

---

*Updated: 2026-01-27 (Release prep cleanup: bench strip, test overhaul 262 tests, code cleanup 5 shared modules, storage/CLI refactoring, doctor improvements, bulk tag, principles catalog)*
*Origin: Redesign from tbd-v1, see `/Users/kaygee/Code/tbd/docs/reference/a-simple-datastore.md`*
