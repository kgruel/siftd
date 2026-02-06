# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/anthropics/siftd/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/anthropics/siftd/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/anthropics/siftd/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/anthropics/siftd/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/anthropics/siftd/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/anthropics/siftd/releases/tag/v0.1.0
