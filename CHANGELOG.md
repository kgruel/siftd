# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/anthropics/siftd/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/anthropics/siftd/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/anthropics/siftd/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/anthropics/siftd/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/anthropics/siftd/releases/tag/v0.1.0
