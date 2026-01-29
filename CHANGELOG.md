# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/anthropics/siftd/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/anthropics/siftd/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/anthropics/siftd/releases/tag/v0.1.0
