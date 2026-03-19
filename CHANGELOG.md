# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-03-19

### Added

- **`siftd tool-search`** — Search tool usage across conversations
- **`siftd tag --current`** — Auto-detect the active session and queue tags, falling back to `--last` when no session is registered
- **`siftd serve`** — HTTP team sync server (`siftd[serve]` optional extra):
  - 5 endpoints: `POST /v1/push`, `GET /v1/pull`, `GET /v1/query`, `GET /v1/search`, `GET /v1/health`
  - Auth middleware: OIDC JWT validation and RFC 7662 token introspection
  - Client-side token acquisition: `token_command` > `env:VAR` > `file:path` resolution
  - Push attribution: `push_log` table records identity/IP/timestamp, conversations tagged `pushed_by:<identity>`
  - FTS rebuild strategies: `on_push` (default), `scheduled`, `off`
  - Built on Litestar; HTTP transport auto-detected from remote URL prefix
- **`db pull`** — Pull conversations from a remote database (inverse of `db push`):
  - SSH and HTTP transport (auto-detected from remote URL)
  - Local-path transport slices remote DB directly
  - `--since`, `--all`, `--dry-run`, `-w` filters mirror push
  - `last_pull` delta tracking — repeated pulls transfer only new conversations
- **`db send`** — Slice database to stdout as binary SQLite (inverse of `db receive`)
- **Porter stemmer for FTS5** — Improves keyword recall for morphological variants
- **Scoped FTS5 passthrough** — Field-scoped queries (e.g. `tool:read`) pass through to FTS5 directly
- **Tool summary embeddings** — Tool call patterns embedded alongside text for semantic search over tool usage
- **In-memory embedding cache** — Cached embeddings, backend resolution, and active-session exclusion sets with TTL-based invalidation
- **CLI→serve delegation** — Search commands delegate to serve endpoint when configured
- **`acquire_token()`** — Public API function for token acquisition from auth config

### Changed

- **Output rendering migrated to painted** — Peek and query detail views render through painted's block/line primitives with the three-axis Fidelity model (visibility × depth × density), replacing the single-axis zoom system
- **`--brief` / `-b` and `--full` / `-F` flags** — Aliases for compact and full-depth rendering on peek and query detail
- **Tool-specific presenters** — file.read, file.edit, shell.execute, search.grep, and task.spawn render structured hints instead of raw input dumps
- **Unified `tag` command** — `siftd tags` merged into `siftd tag` with subcommands (`list`, `rename`, `delete`). `siftd tags` still works as deprecated bridge
- **FTS5 tokenizer upgrade** — Content keyword search now uses the Porter stemmer; opening an existing DB in write mode will rebuild the FTS index once to apply stemming
- **Embeddings index tool summaries** — `siftd search --index` now adds a per-conversation `tool_summary` chunk and will backfill missing summaries for already-indexed conversations that have tool calls
- **Search defaults to embeddings-only** — Skips FTS5 recall pass for lower latency; hybrid mode still available
- **Connection tracking** — Read-only connections reopened on cache reload to escape stale snapshots

### Fixed

- **Search caching correctness** — Fixed -inf score leak, stale cache detection, active-session exclusion underfill, and cache TTL regression
- **Tool-only conversations skipped** — Conversations with only tool calls (no text) were silently dropped during ingestion
- **`event_to_json` missing narrative** — `--follow --json --thinking` no longer silently drops thinking content
- **Trailing whitespace in block rendering** — painted 0.1.4 strips trailing space cells, fixing terminal line-wrap on wide blocks

### Removed

- **Deprecated top-level commands** — `siftd status`, `siftd workspaces`, `siftd path` removed (deprecated since v0.4.4)
- **Zoom module** — Replaced by painted's Fidelity model

## [0.4.7] - 2026-02-18

### Added

- **4 new adapters** — Expanding tool coverage beyond the original 4:
  - **VSCode Chat** — `~/.config/Code/User/History/chat/` (JSON and JSONL formats)
  - **Pi Coding Agent** — `~/.pi/agent/sessions/` (JSONL with thinking blocks, tool calls, usage/cost)
  - **OpenCode** — `~/.local/share/opencode/opencode.db` (SQLite adapter using `open_external_db()`)
  - **Copilot CLI** — `~/.local/state/.copilot/session-state/` (JSONL with subagent tracking)
- **Configuration surface** — Expanded `siftd config` with schema validation:
  - `db.path` — Override default database location
  - `query.limit`, `query.format`, `query.workspace` — Query defaults
  - `tools.limit` — Default tool listing limit
  - `adapter.locations.<name>` — Override adapter search paths
  - `config set --append` / `--remove` for list-valued keys
  - Known-keys registry with schema validation on `config set`
- **Friendlier ingest output** — Progress reporting with per-adapter counts, timing, and `--json` flag for structured output
- **`session-tools` bundled query** — Per-tool-call character counts for a session (`:session` named parameter)
- **`open_external_db()`** — SDK helper for adapters that read external SQLite databases (read-only URI mode)

### Changed

- Unified display formatting with shared output helpers across CLI modules
- Adapter exclusion markers prevent cross-adapter file mismatches (Pi Agent, Copilot CLI paths excluded from Claude Code adapter)

### Fixed

- SQL validation in architecture tests now handles `:var` named parameters (was only normalizing `$var`)
- Prysk acceptance tests use `python3` instead of bare `python` (macOS compatibility)
- Homebrew tap name and upgrade instructions corrected

## [0.4.6] - 2026-02-12

### Added

- **`siftd install plugin`** — Install the bundled Claude Code plugin to user or project scope:
  - Bundles plugin into wheel via hatch force-include
  - `--scope user` (default) installs to `~/.claude/plugins/siftd/`
  - `--scope project` installs to `.claude/plugins/siftd/`
  - Symlink-safe cleanup replaces dev-mode symlinks with real directories
  - `--dry-run` shows source/target without writing
- **`db merge`** — Import an external database (slice) into the main database:
  - Vocabulary ID remapping — same harness/model/workspace with different ULIDs across machines are matched by natural key
  - Workspace matching by `git_remote` (priority) with `path` fallback
  - Replace-by-default — re-ingested conversations (newer ULID, same external_id) replace stale target versions with full cascade
  - `--no-replace` flag to keep existing versions (first-version-wins)
  - `--dry-run` previews merge counts without modifying the target
  - Schema version guard rejects cross-version merges
  - Content blob dedup via SHA256, ref_count recomputation, FK integrity validation
- **`db push`** — Push conversations to a shared remote database
- **Tags temporal filtering** — `siftd tags` accepts `--since`/`--until` for time-scoped tag views
- **Installation guide** — New `docs/guides/install.md` covering siftd and plugin setup

### Changed

- Plugin consolidated to single skill with slash commands
- Concept docs rewritten; example outputs updated to match actual CLI formats
- CLI help text reorganized by functional group

## [0.4.5] - 2026-02-10

### Added

- **`peek --follow` mode** — Real-time session tailing for monitoring live agents:
  - Streams turns as they arrive with text and tool call summaries
  - Tool hints: file paths, commands, search patterns extracted from tool inputs
  - `--json` output produces NDJSON for piping to jq
  - `--exchanges N` controls initial context window (default 3)
  - Auto-selects most recent active session when no ID given
  - Respects `--workspace` and `--branch` filters for session auto-selection
- **Tool hint extraction** — `extract_tool_hint()` summarizes tool_use inputs (file paths truncated to last 2 components, commands, patterns, queries)
- **`TOOL_HINT_KEYS`** — Adapter-specific mapping for hint extraction from Claude Code tool schemas
- **`db slice` filter args for tags drill-down** — `siftd tags` now accepts filter pipeline args

### Fixed

- **Tool accumulation in peek exchanges** — Multi-turn assistant exchanges now show all tools used across turns, not just the last turn's tools
- **Placeholder-only response text** — Assistant turns containing only `[tool: X]` placeholders no longer latch as the exchange response text
- **Assistant-first exchanges** — Sessions starting with an assistant turn (no preceding user record) now create a proper exchange instead of being silently dropped
- **Follow loop robustness** — Inode-aware file reopening for log rotation, truncation recovery seeks to start of file, proper file handle cleanup in finally block
- **`db slice` column order** — ALTER TABLE column ordering bug in slice export

### Changed

- Homebrew formula generation uses PyPI JSON API directly (replaces `homebrew-pypi-poet` dependency)

## [0.4.4] - 2026-02-10

### Added

- **`siftd db` namespace** — Container-level database operations:
  - `db info` — file metadata, page size, journal mode, schema version, FTS5 status
  - `db stats` — database statistics (absorbs `siftd status`)
  - `db workspaces` — list workspaces (absorbs `siftd workspaces`)
  - `db path` — show XDG paths (absorbs `siftd path`)
  - `db vacuum` — compact database and optimize indexes, reports size savings
  - `db backup <file>` — consistent online backup via `sqlite3.Connection.backup()`
  - `db restore <file>` — restore from backup with SQLite magic-byte validation
  - `db slice <file>` — export filtered conversation subset into standalone SQLite database
- **`db slice` filter pipeline** — Full filter vocabulary available: `-w`, `-m`, `--since`, `--before`, `-l`, `--exclude-tag`, `--tool`, `--tool-tag`, `-s`
- **Shared filter args** — `cli_filters.py` with `FilterArgs` dataclass, `add_filter_args()`, `extract_filter_args()` replacing 3 copy-pasted filter blocks
- **Codex CLI token extraction** — Token usage parsing from Codex CLI sessions
- **Token coverage metrics** — Track token extraction completeness across adapters
- **Cache-aware cost calculation** — Cost queries account for cache read tokens
- **CLI display ergonomics** — Status enrichment, query cost display, peek adapter info

### Changed

- `siftd status`, `siftd workspaces`, `siftd path` are deprecated with stderr warnings; use `siftd db stats`, `siftd db workspaces`, `siftd db path`
- Shared filter pipeline reduces CLI argument duplication across query, search, export, slice
- Architecture tests hardened: CLI SQL hygiene checks, `TYPE_CHECKING` import handling, peek types moved to domain

### Fixed

- Cache JOIN duplication in cost queries producing inflated token counts
- Turn narrative: ID-based tool matching, `tool_result` rendering, Gemini thinking block handling
- Token filter relaxed to avoid dropping valid zero-token responses
- `workflow_dispatch` added to publish workflow for manual re-trigger

## [0.4.3] - 2026-02-09

### Added

- **Narrative detail view** — `siftd query <id>` renders response content as interleaved narrative blocks (text, tool calls, thinking) instead of flat prompt/response pairs:
  - `--thinking` flag to include model reasoning blocks
  - `--tools [FILTER]` to show tool inputs/results (optional filter: tool name prefix or `errors`)
  - `--tool-chars N` to control tool content truncation
- API wrappers: `list_workspaces`, `resolve_entity_id`, `get_recent_conversation_ids`
- `resolve_db` helper — centralizes database path resolution across CLI modules
- Declarative dependency manifest for architecture enforcement with violation ratchet

### Changed

- **`Turn` is now the primary conversation detail structure** — `ConversationDetail.turns` is the source of truth; `.exchanges` is a backward-compatible derived property (one per prompt, not per response). Consumers using `.exchanges` continue to work unchanged.
- Detail view summary line says `Turns:` instead of `Exchanges:`
- CLI fully decomposed — `cli.py` is now a 59-line dispatcher; logic extracted to `cli_common`, `cli_meta`, `cli_sessions`, `cli_tags`, `cli_query`, `cli_data`, `cli_peek`, `cli_export`
- `tag --last` defaults to 1 when count omitted
- Lazy imports in `cli_data.py` for adapters, backfill, and ingestion modules

### Fixed

- `search --json` no longer errors on empty result sets
- Connection leak in `_search_fts_only` (try/finally)
- `open_database` import consistency in `api/search.py`

## [0.4.0] - 2026-02-05

### Added

- **Unified `search` command** — Replaces `siftd ask` with auto-selection:
  - Semantic search when embeddings available, FTS5 fallback when not
  - `--semantic` flag to force semantic mode (errors if embeddings missing)
  - `--by-time` flag for chronological ordering
- **Live session tagging** — Tag active sessions before they're ingested:
  - `/siftd:tag` Claude Code skill for tagging from within sessions
  - `active_sessions` and `pending_tags` tables for deferred tag application
  - Tags applied automatically at next ingest
  - `siftd doctor fix --pending-tags` to clean up orphaned/stale pending tags
- **Binary content filtering** — Binary blobs filtered during ingest; metadata placeholder preserves type/size info
- **Workspace identity** — Git remote URL as primary identifier, resolved path fallback for non-git dirs
- **Git worktree resolution** — Worktrees resolve to main repo workspace; branch tracked separately
- **Peek improvements**:
  - Subagent detection and grouping
  - Worktree branch identity: `[branch]` suffix in display, `--branch` filter
  - `--last-response` / `--last-prompt` flags for quick extraction
- **Unified output formatting** — `--brief` / `--summary` modes for `query`; `--exchanges N` for `peek`
- **Skill interface versioning** — `skill-interface-version: 1` in skill frontmatter for stability promises
- **Index compatibility validation** — Embedding index now tracks schema version, backend, model, and dimension:
  - Actionable error messages when backend/model mismatch detected
  - `EmbeddingsCompatCheck` doctor check for configuration drift
  - Incremental indexing blocked when it would mix incompatible embeddings
- **Score explainability** — `--json` output includes `breakdown` with component scores:
  - `embedding_sim`, `recency_boost`, `pre_mmr_score`, `mmr_penalty`, `mmr_rank`, `final_score`
  - `fts5_matched` and `fts5_mode` for hybrid search transparency
- **Deterministic search results** — Chunk ID (ULID) used as tie-breaker throughout scoring pipeline
- **3 new doctor checks**:
  - `fts-stale` — Detects FTS5 index out of sync with content tables
  - `fts-integrity` — Checks FTS5 table integrity for corruption
  - `config-valid` — Validates config file syntax and formatter names
- CLI help argument groups for organized `--help` output
- Helpful hints when `query` returns empty results
- MMR safety cap to prevent unbounded memory on large result sets
- `siftd ingest --rebuild-fts` — Rebuild FTS index from existing data without re-ingesting

### Changed

- **Breaking:** `siftd ask` renamed to `siftd search`
- **Breaking:** Removed deprecated `query -s/--search` flag — use `siftd search --fts` instead
- **Breaking:** Removed deprecated `query --count` flag — use `-n/--limit` instead
- **Breaking:** Removed deprecated `peek --last` flag — use `-n/--limit` instead
- `siftd peek` defaults to 10 sessions (was unbounded 2-hour window); use `-n/--limit` to control
- `siftd status` query performance optimized
- `--exclude-tag` renamed to `--no-tag` in export command (consistency with other filters)
- Narrowed `siftd.api` public exports — internal search primitives moved to `siftd.api.search`
- Removed phantom dependencies: `httpx`, `tqdm`, `pyyaml`, `loguru`
- Architectural tests moved to `tests/architecture/` for clearer separation

### Fixed

- Schema version tracking via `PRAGMA user_version` — prevents older siftd from opening newer databases
- `siftd query --since invalid` now shows clear error instead of silently returning empty results
- `siftd` with no args now shows help instead of terse argparse error
- Empty-filter query tip now suggests broadening filters instead of re-running ingest
- Connection leak safety: all `search.py` database connections wrapped in try/finally

- **P0**: Session ID mismatch in live tagging — hooks now use namespaced `claude_code::sessionId`
- **P1**: Active session staleness detection — added `last_seen_at` timestamp
- Peek session lookup: O(n) scan → O(1) path-based filtering
- Workspace resolution for git worktrees (worktrees assigned to correct workspace)
- Peek session resolution prefers parent session over subagents
- `siftd peek` Ctrl+C now exits cleanly (exit code 130) instead of stacktrace
- `--by-time` warns when it has no effect (no temporal data)
- Test isolation issues with XDG_CONFIG_HOME in ask tests

## [0.3.0] - 2026-01-30

### Added

- `--since`/`--before` accept relative dates: `7d`, `1w`, `yesterday`, `today`
- `--recency` flag for temporal weighting in semantic search (with `--recency-half-life`, `--recency-max-boost`)
- Automatic batching for large IN() lists (avoids SQLite 999-variable limit)
- Help examples in `siftd ingest` and `siftd backfill` epilogs

### Changed

- Vector search uses numpy batch operations (14-21x faster); numpy now a core dependency
- `exclude_conversation_ids` filter pushed to SQL for incremental indexing
- Unknown `--format` values error with available options (was silent fallback)

### Removed

- `--role` flag from `siftd ask` (exchange chunks always matched; not worth fixing)

## [0.2.0] - 2026-01-30

### Added

- **Hard rules enforcement tests** — Automated CI checks for architectural invariants:
  - `sqlite3.connect()` outside storage/ (AST-based)
  - stderr hygiene (tips/warnings must use stderr)
  - Built-in query SQL validation
  - Built-in adapter compliance
  - Formatter registration validity
  - JSON output purity
- **Privacy warnings** — `--full` and `--refs` flags now print warning to stderr about sensitive content

### Changed

- `--thread --json` now warns and ignores `--thread` (JSON formatter doesn't support thread grouping)
- FTS5 error handling improved — "no such table" gives "run ingest first" hint, other errors suggest `siftd doctor`
- Date examples in docs/help now use ISO format (`2024-01-01`) instead of unsupported relative dates

### Removed

- `--latest` flag from `siftd query` — was a no-op (newest-first is the default)

### Fixed

- `--thread` mode no longer trims widened candidate pool to `--limit`
- `--first` now respects `--threshold` (was hardcoded to 0.65)
- `--first` now sorts by prompt timestamp, not conversation start time
- `--json --refs` combination now errors instead of producing invalid JSON
- All search paths use `open_database(read_only=True)` — no WAL/SHM files on read-only media
- `first_mention()` docstring: `source_ids` is required, not optional
- `fts5_recall_conversations()` docstring: mode is "and/or/none", not "prefix/exact/none"
- Multiple stderr hygiene fixes in CLI (tips/warnings now correctly go to stderr)

## [0.1.1] - 2026-01-29

### Added

- `siftd install embed` — Convenience command to auto-detect installation method and install embedding dependencies
- `:var` parameterized syntax for query files — safe quoting via sqlite3, alongside existing `$var` text substitution
- `ADAPTER_INTERFACE_VERSION = 1` — Required attribute for all adapters, enables future interface migrations
- `ON DELETE CASCADE` on schema foreign keys — Child records now cascade on parent delete

### Changed

- Adapter `discover()` function now requires `locations` keyword argument (fallback removed)
- Error messages for missing `[embed]` extra now reference `siftd install embed` and suggest FTS5 alternative

### Removed

- `Conversation.default_model` field — Was defined but never populated or used

### Fixed

- Type checker (`ty`) configuration for optional dependencies — No longer blocks commits
- `bench/corpus_analysis.py` type annotation bug

## [0.1.0] - 2026-01-28

Initial public release.

### Added

#### Core Features
- **Ingestion** — Aggregate conversation logs from multiple CLI coding tools
- **FTS5 Search** — Full-text search across all conversations via `siftd query -s`
- **Semantic Search** — Vector similarity search via `siftd ask` (requires `[embed]` extra)
- **Tagging** — Apply tags to conversations, workspaces, and tool calls for organization

#### Adapters
- Claude Code (Anthropic) — `~/.claude/projects`
- Aider — `~/.aider`
- Gemini CLI (Google) — `~/.gemini/tmp`
- Codex CLI (OpenAI) — `~/.codex/sessions`
- Drop-in adapter support via `~/.config/siftd/adapters/`
- Entry-point adapter registration for pip-installable adapters

#### CLI Commands
- `siftd ingest` — Ingest logs from all discovered sources
- `siftd status` — Show database statistics
- `siftd query` — List/filter conversations with flexible filters
- `siftd ask` — Semantic search over conversations (optional `[embed]` extra)
- `siftd tag` — Apply or remove tags on entities
- `siftd tags` — List, rename, or delete tags
- `siftd tools` — Summarize tool usage by category
- `siftd export` — Export conversations for PR review workflows
- `siftd doctor` — Run health checks and maintenance
- `siftd peek` — Inspect live sessions from disk (bypasses SQLite)
- `siftd path` — Show XDG paths
- `siftd config` — View or modify configuration
- `siftd adapters` — List discovered adapters
- `siftd copy` — Copy built-in resources for customization
- `siftd backfill` — Backfill derived data from existing records

#### Query System
- User-defined SQL queries via `~/.config/siftd/queries/*.sql`
- `$var` syntax for text substitution
- Built-in queries: `cost.sql`, `shell-analysis.sql`

#### Python API
- `siftd.api.list_conversations()` — Query conversations with filters
- `siftd.api.get_conversation()` — Get full conversation detail
- `siftd.api.export_conversations()` — Export for external tools
- `siftd.api.hybrid_search()` — Combined FTS5 + semantic search

#### Storage
- SQLite with FTS5 for full-text search
- ULID primary keys throughout
- Normalized schema with proper foreign key constraints
- Extensible `*_attributes` tables for variable metadata

#### Developer Experience
- XDG Base Directory compliance for paths
- `--db PATH` override for all commands
- JSON output mode for scripting (`--json`)

---

[Unreleased]: https://github.com/kgruel/siftd/compare/v0.4.7...HEAD
[0.4.7]: https://github.com/kgruel/siftd/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/kgruel/siftd/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/kgruel/siftd/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/kgruel/siftd/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/kgruel/siftd/compare/v0.4.2...v0.4.3
[0.4.0]: https://github.com/kgruel/siftd/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/kgruel/siftd/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kgruel/siftd/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/kgruel/siftd/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kgruel/siftd/releases/tag/v0.1.0
