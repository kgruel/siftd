# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`--latest` alias for `--last`** — Both `siftd tag` and `siftd export` now accept `--latest` as an alias for the existing `--last` flag, providing an alternative ergonomic name for the same functionality.
- **`siftd id <ULID>` classification command** — Resolves a ULID to either a conversation or event, emits a one-line summary with context (workspace, started date for conversations; conversation ID for events) and a view hint. Supports `--json` for structured classification. Exit codes: 0 (hit), 1 (miss), 2 (ambiguous).

### Changed

- **`siftd db --help` epilog reorganized** — Help text now groups subcommands into logical sections: Inspection (info, schema-version, stats, workspaces, path, sync-status), Maintenance (vacuum, backup, restore), Sync (slice, merge, send, push, pull), and Sync remotes (remote, receive, process). Improves discoverability for users navigating the database operations namespace.
- **ULID truncation standardized to 8 characters** — All list and search renderers (terminal, markdown, HTML, JSON) now display conversation IDs as 8-character short forms (`:8`). Detail views remain unchanged. Affects `siftd query`, `siftd search`, `siftd tag list`, serve HTML routes, and all export formats. **Note for agents:** grep patterns matching 12-character IDs should be updated to 8 characters for consistency with displayed output.

## [0.8.1] - 2026-05-07

### Added

- **Event IDs in JSON (default-on)** — `Turn` gains `prompt_id` / `response_ids[]` / `tool_call_ids[]`; `NarrativeBlock` gains `event_id`; `ToolCallDetail` gains `tool_call_id`. Search chunks emit `chunk_id` / `source_ids` by default. Enables agents to round-trip event IDs through query → tag / detail surfaces without secondary lookups.
- **Late-bound `--last-*` pending tags** — `siftd tag --session <id> --last-{prompt,response,exchange,tool-call}` queues tag intent against a live session; resolution to the most-recent matching event happens on next `siftd ingest`. Schema-additive `last_marker` column on `pending_tags` (in-place rebuild on first open). New `POST /api/v1/sessions/{id}/tags` HTTP route.
- **Event detail surface** — `EventDetail` dataclass + `get_event(id, *, include_neighbors=False)` API. `siftd query <event_id>` smart-routes via prefix-match across event kinds. `GET /api/v1/events/{id}` HTTP route.
- **Tag-prefix conventions table** — `[tag_prefixes]` config section with built-in defaults (`decision:`, `research:`, `useful:`, `rationale:`, `genesis:`). `siftd config tag-prefixes [--json]` dumps the resolved table. Groundwork for future skill/hook consumers; no runtime consumer in this release.

### Changed

- **CI matrix expanded to Python 3.12, 3.13, and 3.14.** Previously only 3.12 was tested; argparse formatting differences in later versions had silently slipped past CI. Help-snapshot tests now run on every matrix version via per-version snapshot directories at `tests/snapshots/__snapshots__/py{ver}/`. Snapshot policy: `docs/guides/snapshot-policy.md`.

### Deprecated

- **`--debug-ids` flag and `debug_ids` kwarg** — Now a hidden no-op (chunk_id and source_ids ship by default in JSON). Accepted on `siftd search`, `to_render_dict()`, `render_search()`, and the serve render context through v0.9.x; removed in v0.10.0.

### Removed

- **`siftd tool-search` command and its denormalized projection table.** The `tool_search` table and `tool_search_fts` virtual table are dropped in schema migration v8 (validated against a 2.7 GB production DB: 200k+ tool_search rows reclaimed in <10 s, ~11% DB size recoverable via `siftd db vacuum`). Capability lost: bare-text FTS over a 280-char tool-call result snippet. Tool-call queries now go through the events substrate via `siftd query --tool` and structured tag filters. The `/api/v1/tool-search` HTTP route, the serve HTML `/tools` page, and the `tools.limit` config key are also removed.
- **`siftd tags` command.** Deprecated command removed. Use `siftd tag list`, `siftd tag rename`, or `siftd tag delete` instead.

### Fixed

- **Polymorphic tag filter** — `siftd query -l <tag>`, `--all-tags`, and `--no-tag` now match tags applied at any conversation-bearing target_kind (`conversation`, `prompt`, `response`, `tool_call`, `exchange`). Previously, after the polymorphic storage refactor (v0.8.0), only conversation-scoped tags were visible to these filters; tags applied at event granularity were silently invisible. New `--on KIND` flag opts into legacy single-kind filtering.
- **Test suite stability under restricted environments** — 5 git tests now pass under sandboxes without git user config (subprocess passes `-c user.email/-c user.name`); 5 chmod-based readonly tests skip cleanly under root via `@pytest.mark.skipif(os.getuid() == 0)`.
- **Stale `siftd tags` references** purged from `README.md`, `docs/concepts/tags.md`, and `plugin/skills/siftd/reference/tags.md`. Stale docstring on `build_tags_parser` and stray blank-line residue in `cli/__init__.py` and `output/json_fmt.py` after the tool_search and tags-shim removals.

## [0.8.0] - 2026-05-06

> **Upgrade note.** This release ships a one-way schema migration (v3 → v7).
> Run any siftd command after upgrading to trigger it; a pre-migration backup
> is written next to your database as `<name>.bak.YYYYMMDD.db` automatically.
> On a 3 GB database the migration takes ~40 seconds and emits per-phase
> progress to stderr. Read-only commands (`query`, `doctor`, `peek`, `search`)
> auto-upgrade transparently if the file is writable, or raise a clear
> `SchemaUpgradeRequiredError` if not.

### Added

- **Polymorphic storage refactor (schema v3 → v7)** — Four parallel storage forks (events: `prompts`/`responses`/`tool_calls`; content: `prompt_content`/`response_content`; four `*_attributes` tables; four `*_tags` tables) dissolved into a unified polymorphic schema: `events` + sparse `event_response`/`event_tool_call`/`event_content` extensions + polymorphic `attributes` (target_kind, target_id) + `tag_assignments` (target_kind, target_id). Aggregations (exchange, turn) are query-time `parent_id` walks, never tables.
  - **Migration runs once on first open after upgrade.** Pre-migration backup via SQLite online backup API. Versions v4 (events schema), v5 (FTS5 simplification), v6 (legacy table drops + blob preservation), v7 (pending_tags exchange_index alignment).
  - **Granular tagging via colon-paths** — `siftd tag <conv>:<kind>:<n>` targets a specific prompt, response, tool_call, or exchange (1-indexed, deterministic ordering by `timestamp, id`). `<kind>` ∈ `{prompt, response, tool_call, exchange}`.
  - **Thinking blocks now FTS-searchable** — Live-write + migration + rebuild use uniform `$.text IS NOT NULL` filter; thinking content surfaces in `siftd query -s` results.
  - **`list_tags` returns `prompt_count` + `response_count` + `exchange_count`** alongside aggregate `usage_count`. Per-target-kind breakdown for tag inspection.
  - **Polymorphic cleanup triggers** — Cascade-orphan triggers on `events`, `workspaces`, `conversations` automatically clean orphaned `attributes` and `tag_assignments` rows. Replaces explicit cleanup calls.
  - **`siftd slice` opens source read-only** — Refuses with clear error if source `user_version < SCHEMA_VERSION` (no auto-upgrade, no backup file leakage).

- **Migrations as a first-class subsystem.** A `MIGRATIONS[v]` registry replaces the previous ad-hoc per-version code; each migration owns one transactional phase of work. Supporting tooling:
  - **`siftd db schema-version`** — Triage command. Reports current vs target version, lists applied / pending migrations, returns non-zero on schema-newer-than-binary (telling the user to upgrade siftd, not the DB).
  - **`siftd doctor` deep checks** — `db-fk-integrity`, `db-trigger-presence`, `db-blob-refcount-drift` audits. Run via `siftd doctor --deep`.
  - **Deep preflight gate on `db merge` / `db receive`** — Source databases are integrity-checked before merge so corruption isn't propagated; `PreflightError` carries the failing finding messages and source path so inbox failures stay traceable.
  - **Schema fixtures + parametrized upgrade tests** — `tests/fixtures/schemas/v{0..7}.sql` snapshot the schema at each version; tests walk every adjacent upgrade pair so migrations can't silently drift from the schema they target.
  - **Adapter golden fixtures** — Tiny canonical input/output pairs for each ingest adapter (`tests/fixtures/adapters/`) catch parser regressions when upstream CLIs change their log formats.

- **Auto-upgrade for read-only commands on stale-schema DBs** — `open_database(read_only=True)` peeks `user_version` and runs the migration in a transient write-mode open if the file is writable. If not writable, raises `SchemaUpgradeRequiredError` (re-exported via `siftd.api`) with a clear message instead of crashing later with `OperationalError("no such table: events")`. `auto_upgrade=False` opt-out for diagnostic callers (`db schema-version`, `db info`, `slice` source pre-check) that need to inspect the on-disk version without mutating it.

- **Schema v3: `content_blobs.ref_count` integrity** — Column now carries `NOT NULL DEFAULT 1 CHECK (ref_count >= 0)`. `release_content()` clamps via `MAX(ref_count - 1, 0)` and the delete trigger uses `<= 0` consistently. Migration garbage-collects any legacy `ref_count <= 0` rows (nulling dangling `tool_calls.result_hash` references first) before recreating the table with the new constraint; also patches the old delete trigger in-place for existing databases. Schema version bumped to 3.
- **Hash-collision detection (fail-closed)** — `store_content()` and `migrate_existing_results()` now verify existing blob content before reusing a hash. If two distinct content values produce the same SHA256 digest, a `BlobCollisionError` is raised instead of silently corrupting the stored blob.
- **`verify_migration` integrity report** — Two new keys: `ref_count_mismatches` (blobs where stored `ref_count` diverges from actual `tool_calls` reference count) and `negative_ref_counts` (pre-migration legacy corruption diagnostic; reports 0 on fully migrated databases).

### Changed

- **`siftd doctor fix` no longer auto-merges duplicate workspaces** — The duplicate-workspace finding is now informational-only. To merge, run `siftd migrate --merge-workspaces` manually.
- **Search-chunk JSON output omits `chunk_id` and `source_ids` by default** — These are storage-internal identifiers that were leaking through search results. Use `--debug-ids` (CLI) or `?debug_ids=1` (serve) to restore them. Conversation summaries/details are unchanged — `conversation_id` remains visible as the public addressable handle.
- **CLI logs to stderr at INFO** — `cli/__init__.py` `main()` configures the `siftd.*` logger with a `%(message)s` stderr handler so auto-upgrade and migration-progress events surface to users. Idempotent so test re-entry doesn't pile up duplicate handlers.

### Fixed

- **Migration v6 ref_count heal was O(M·N) and pinned a CPU for 44+ minutes** on a real 2.9 GB database before being killed. The naive correlated-subquery form scanned `event_tool_call` once per `content_blobs` row against an unindexed FK column. Rewritten as a single set-based `UPDATE` driven by a `WITH counts AS (… GROUP BY result_hash)` CTE, with a partial index on `event_tool_call(result_hash)` added in M6 (and to fresh schemas). Migration v3 → v7 against the same database now completes in ~40 seconds. Contract regression test asserts `EXPLAIN QUERY PLAN` of the heal query consults the index.
- **Schema migrations no longer run silently** — Each `MIGRATIONS[v]` phase emits an INFO log line with the row counts driving the work (e.g. `Migration v4: copying 34494 prompts, 454733 responses, 287176 tool_calls into events`). Plus two lines from the `open_database` runner: `Migrating schema vX → vY` and `Creating pre-migration backup: <name>`. Catches the previous failure mode where users assumed silent migrations were stuck and Ctrl-C'd them.
- **Doctor and similar read-only commands no longer create surprise WAL/SHM sidecars** under the new auto-upgrade path — the `_peek_user_version` helper opens the DB with `mode=ro&immutable=1`, mirroring the main RO connection, and the auto-upgrader runs `PRAGMA wal_checkpoint(TRUNCATE)` before closing so the upgraded `user_version` lands in the main DB file.
- **Doctor `CheckContext` lazy-init race under thread pool** — Concurrent doctor checks could double-initialize the per-context connection. Lock added.
- **Embeddings indexer connection lifetimes** — `try/finally` wrapping ensures connections are always closed on indexing failures.
- **Timestamp writes are UTC-aware** — Storage writers use `datetime.now(UTC).isoformat()` consistently; previously some paths emitted naive timestamps that compared incorrectly against ISO-Z reads.
- **Blob triggers dropped before `content_blobs` recreate in v3 migration** — Without dropping first, SQLite refused to recreate the table while triggers referenced it.

### Removed

- **Aider `analytics.jsonl` no longer discovered for ingest** — The file was yielded by discovery but produced zero conversations (parse was a no-op). Removed to eliminate a misleading no-op: the file appears in discovery output but nothing is ingested. Analytics ingestion is deferred until Aider publishes a stable schema for the file.

## [0.7.0] - 2026-04-24

### Changed

- **Search pipeline unified** — All search post-processing (metadata enrichment, file refs, context windows, conversation aggregation, thread tiering) moved from CLI to composable API primitives. CLI no longer contains direct SQL. `SearchChunk` and `ConversationSearchSummary` dataclasses in `domain/search_types.py` replace ad-hoc dicts as canonical result types. `--fts` path unified through same Operation IR as hybrid/semantic
- **Search API surface formalized** — Canonical search types and primitives (`SearchChunk`, `ConversationSearchSummary`, `search_chunks`, enrichment helpers, filtering, sorting, aggregation, and thread tiering) are exported through `siftd.api` and `siftd` while preserving lazy imports for optional embedding dependencies
- **Tag mutation extracted to API** — Three focused API functions (`apply_tags`, `rename_tag_safe`, `delete_tag_safe`) replace duplicated orchestration in CLI and serve. API owns connection lifecycle and transaction boundaries. Cross-owner protection SQL moved from serve route to `storage.tags.tag_used_by_other_owners` helper
- **Serve serializers made lossless** — Tags, tool search, and stats serializers now include all API dataclass fields. `dataclasses.asdict()` used as baseline in serialization layer. CLI rehydrate-with-defaults pattern replaced by strict API deserializers (`tag_info_from_dict`, `tool_search_payload_from_dict`, `dict_to_stats`)
- **`ScoreBreakdown` relocated to `domain/search_types`** — Breaks `search ↔ storage.embeddings` cycle and `output → search → storage` transitive coupling
- **`api → serialization` cycle broken** — `_stats_to_dict` inlined in `api.stats`, `serialization.stats.serialize_stats` delegates to it (correct one-way direction)
- **Ingest/backfill extracted to API** — `api.ingest.run_ingest` and `api.backfill.run_backfill` wrap ingestion pipeline with `db_path` lifecycle ownership. CLI no longer imports `siftd.ingestion` or `siftd.backfill` directly
- **Serve health and push logging moved behind API** — Health endpoint and push-log writer now go through `api.serve_status`. Health response shaped by `serialization.serialize_health_status` like every other route
- **Embedding availability moved behind API** — CLI status/search paths now use `siftd.api.embeddings_available` and API-exported index compatibility exceptions instead of importing optional embedding internals directly
- **Package root re-exports through API** — `siftd.apply_tag`, `siftd.list_tags`, `siftd.get_or_create_tag` now resolve via `siftd.api.tags` instead of `siftd.storage.tags`, plus new `apply_tags`, `rename_tag_safe`, `delete_tag_safe`. External `import siftd` consumers get connection-lifecycle-managed entry points
- **Config `↔` paths cycle broken** — `paths.db_path()` reads config.toml via stdlib `tomllib` instead of importing `siftd.config`
- **Sync config extracted** — 450 lines of sync-specific accessors (remotes, timeouts, SSH options, cursor mutations) moved to `config_sync.py`. `config.py` re-exports for backward compatibility

### Added

- **Anti-drift serializer tests** — Compare serializer output keys against `dataclasses.fields()` for `TagInfo`, `ToolSearchResult`, `DatabaseStats`, `SearchChunk`, `ConversationSearchSummary`. Prevents silent field omission when dataclasses change
- **Local/delegated JSON parity test** — Tool search `--json` output is schema-identical whether executed locally or via serve delegation
- **Tag mutation API tests** — Apply/remove/rename/delete with ownership protection, entity resolution, and edge cases
- **Tag mutation serialization** — `serialization/tags.py` with typed payload dataclasses and anti-drift tests
- **Ingest/backfill API tests and serializer drift tests** — Coverage for `IngestRunResult`, `BackfillRunResult` types
- **Dependency direction arch tests** — `api/` must not import `serialization/`, `storage/` must not import `api/`, `domain/` must be pure. Known `api↔serialization` cycle tracked as strict xfail (now resolved)
- **Boundary xfail cleanup** — Serve direct-storage and CLI direct-embeddings architecture tests now run as normal passing tests
- **Package-root storage-boundary arch test** — `siftd/__init__.py` is now scanned for direct `siftd.storage.*` imports, with `# arch: allow-storage` waiver
- **`asdict` matcher tightened** — `_find_dataclasses_asdict_calls` now catches both `dataclasses.asdict(x)` and bare `asdict(x)` (after `from dataclasses import asdict`); regression test pins both forms

### Fixed

- **VSCode empty-window sessions** — VSCode/Cursor/Windsurf chat discovery now includes `globalStorage/emptyWindowChatSessions`, so no-workspace chats are ingested instead of ignored
- **Codex tool-call preservation** — Codex CLI logs with tool calls before the first user prompt now get a synthetic prompt so those tool calls are attached to the conversation instead of dropped
- **Malformed JSONL tolerance** — Shared JSONL adapter loading skips malformed or non-object lines, which makes live/truncated logs from JSONL-backed adapters non-fatal during ingest
- **Optional embeddings imports** — Search and embedding helpers avoid importing optional embedding modules from broad package re-exports where possible, improving graceful behavior without the `[embed]` extra

## [0.6.4] - 2026-03-28

### Fixed

- **Sync: silent fallback to blocking merge** — When preflight capability negotiation failed (remote too old, SSH hiccup, missing `sync-status` command), push silently fell back to a blocking `receive_database()` merge over SSH. With large payloads this hangs until the 600s command timeout. Push now requires staged receive for SSH remotes and surfaces a clear error on version mismatch
- **Sync: zero-copy staging, race-safe inbox, HTTP staged mismatch** — `stage_payload` avoids unnecessary copy; inbox `processing` claim is atomic; HTTP push correctly routes through staged path when negotiated
- **Sync: cursor advancement and inbox recovery** — `last_sent` cursor tracks filter signature so filter changes invalidate stale cursors; stale `processing` rows are reclaimed after timeout; `last_sent` preferred over `last_push` for incremental slicing
- **Blob ref_count triggers and transaction atomicity** — `content_blobs` ref_count maintained by triggers; merge and ingest wrap related writes in explicit transactions
- **Storage lifecycle** — WAL-aware backup, sidecar cleanup on restore, migration column preservation, merge schema validation, workspace path-fallback
- **Ingest contract** — Explicit parse failures, race-safe multi-conversation rejection, session-dedup hash check, scoped adapter overrides
- **Tag lifecycle** — Cache invalidation on rename/delete, pending tag propagation, duplicate collapse
- **Search pipelines** — Retry, recency re-sort, candidate cap, and score writeback ported to API path; score propagation fix and render crash fix
- **SQL correctness** — Query-layer hardening across owner-scoped paths, boundary sanitization
- **Doctor** — False positive and negative fixes in health check modules

### Changed

- **Owner scoping unified** — SQL helpers for owner-scoped queries; htmx search, stats, tools, tags, and conversation routes all consistently scope by owner
- **Serve auth hardened** — Loopback bypass removed, owner scoping enforced on all write paths, delegation tokens, OIDC error redaction, fail-closed writes
- **Config permissions** — Config file permissions validated, cache TTL bounded

### Added

- **Architecture test** — CLI and serve must not import `siftd.search` directly (enforces API boundary)

## [0.6.3] - 2026-03-25

### Added

- **Sync protocol v2 — staged receive and capability negotiation**
  - `siftd db receive --stage` writes payload to inbox for deferred merge (fast ACK)
  - `siftd db process` merges all staged inbox payloads
  - `siftd db sync-status` reports receiver capabilities and inbox state as JSON
  - Pre-flight capability negotiation: push auto-detects staged support on the remote and adapts; falls back to blocking receive for old receivers
  - `SYNC_CAPABILITIES` replaces version-based negotiation — new features are capability strings, not version bumps
- **Split sync timeouts** — separate `connect_timeout_s` (TCP/SSH handshake) from `command_timeout_s` (total operation) at sync global, per-transport, and per-remote config levels
- **Per-remote sync filters** — `[sync.remotes.*.filters]` for workspace, tag, no_tag, owner scoping; CLI flags override config
- **Sync strategy config** — `strategy = "incremental" | "full"` at global and per-remote level; `--strategy` CLI flag on push/pull
- **`db send` filter flags** — `--tag`, `--no-tag`, `--owner` flags for filtered slice export over SSH
- **`GET /api/v1/sync/status`** — serve endpoint for HTTP capability negotiation
- **`no_tag` on pull endpoint** — `/api/v1/pull` now accepts `no_tag` query parameter
- **`sync_inbox` table** — tracks staged payload lifecycle (staged → processing → done/error)

### Fixed

- **Push timeout doom loop** — failed pushes now record `last_sent` before remote processing, so subsequent pushes are incremental even if merge times out
- **HTTP timeout not configurable** — `httpx.Client(timeout=300)` replaced with configurable `httpx.Timeout` using split connect/command values
- **`_build_ssh_options` return type** — annotation corrected from `dict` to `tuple[str, dict]`

## [0.6.2] - 2026-03-24

### Fixed

- **Homebrew install still broken** — `cryptography` can't reliably build from source even with `rust` + `openssl@3`. Formula now installs `cryptography`/`cffi`/`pycparser` via pip binary wheels before building remaining resources from source

## [0.6.1] - 2026-03-24

### Fixed

- **Homebrew install broken** — `cryptography` (transitive dep via asyncssh) failed to build from source. Formula now includes `rust` and `openssl@3` as build dependencies
- **Write routes crash without auth** — `require_write()` crashed when no auth middleware installed (Litestar `Request.user` raises instead of returning None)
- **`dev check` hid serve test failures** — Test scope widened from `not embeddings and not serve` to `not slow`

## [0.6.0] - 2026-03-24

### Added

- **htmx web UI** — Browse, search, and analyze conversations in the browser at `/`:
  - Conversation list with workspace/model/tag/date filters
  - Full detail view with collapsible turns, tool cards, and sticky header
  - Markdown rendering (mistune) and syntax highlighting (Prism.js)
  - Live search — semantic + FTS5 hybrid when embeddings available, FTS5 fallback
  - Search modes — chunks/conversations toggle with `aggregate_by_conversation()` API
  - Follow mode — live session tailing via `/follow` with 2s polling
  - Stats dashboard — summary cards, by-model token breakdown, by-workspace cost, top tools
  - Deep links — bookmarkable `?id=`, `?q=`, `?follow=` URLs via `hx-push-url`
  - Resizable panes — draggable divider between list and detail (JS, 15%-85% clamp)
  - Inline tagging — add/remove tags from conversation detail view
  - Export as document artifact from detail view
  - "The Instrument" design system with dedicated CSS (`siftd.css`)
  - Architecture tests enforcing route boundary separation
- **Authentication** — Three auth modes for `siftd serve`:
  - Static password (`serve.auth.static_token`) for local dev/testing
  - OIDC JWT validation against configurable issuer JWKS
  - RFC 7662 token introspection for OAuth2 deployments
  - Scope-based authorization: `required_scopes` gates all access (all-of), `write_scopes` gates tag/push operations (any-of)
  - Browser login form via htmx — 401 triggers token input, stored in sessionStorage
  - Loopback API bypass — CLI delegation on same machine works transparently with auth enabled
  - `env:VAR_NAME` syntax for secrets in config
- **Multi-tenancy** — Conversation ownership for shared databases:
  - `conversation_owners` table with push-time identity stamping
  - Owner-scoped queries across list, search, tool-search, and export
  - `--owner` CLI filter
  - `owner` promoted to first-class attribute on `ConversationSummary`
- **Operation IR** — `dispatch()` pattern for normalize→execute→render:
  - All commands migrated (Tier 1: stats/workspaces/tools/tags, Tier 2: detail/export/tool-search, Tier 3: tag writes, search)
  - Unified parameter names across CLI/HTTP/API — dissolves `_SERVE_PARAM_MAP`
  - HTML output format as fourth peer to terminal/markdown/JSON
- **Unified exception handling** — `safecall` module with codebase-wide migration
- **Serve as general daemon** — Stats cache, read-path delegation for query/workspaces/tools/tags/tool-search/export/detail, tag write delegation via `POST /api/v1/tag`
- **Serialization layer** — Extracted JSON output unification across CLI and API
- **Tool presenters** — Format-neutral extraction layer with 7 tool-specific extractors (file.read, file.edit, file.write, shell.execute, search.grep, file.glob, ui.todo) plus generic fallback. Consumed by both painted bridge and HTML formatter
- **Narrative emitter protocol** — `PaintedEmitter` and `HtmlEmitter` share `walk_narrative()` as single source of truth for fidelity gating
- **Configuration reference docs** — Auto-generated from config schema via `./dev docs`. All config keys documented with types, defaults, and descriptions
- **`get_config_table()`** — New API for reading TOML sections as dicts (e.g., `serve.auth`)

### Changed

- **URL restructure** — UI serves from `/` (was `/ui`), JSON API at `/api/v1/` (was `/v1/`). Health endpoint at `/api/v1/health`
- **Adapter SDK: record normalizer pattern** — Adapters that implement `normalize_record()` get `peek_scan`, `peek_exchanges`, and `peek_tail` for free via `make_peek_hooks()`. Replaces per-adapter custom peek implementations with a single SDK code path
- **Peek coverage: 3/8 → 7/8 adapters** — Pi Agent, Copilot CLI, and VSCode gain peek support. Claude Code, Codex CLI, and Gemini CLI migrated from custom peek to normalizer-derived
- **Adapter boilerplate reduction** — All adapters now use `build_harness()`, `flush_pending_calls()`, and `discover_files()` from the SDK. Net ~580 lines removed from adapters
- **Subagent detection promoted to SDK** — `SUBAGENT_PATH_MARKER` and `extra["agent_id"]` in `NormalizedRecord` enable any adapter to support session hierarchy, not just Claude Code
- **painted bridge simplified** — 7 `_render_*_lines` functions and duplicated JSON parsing replaced by single `_presentation_to_lines` consuming `ToolPresentation`. Net ~350 lines removed
- **Search findability** — Porter stemmer and tool descriptions in FTS5 index (+21% FTS5 recall). Tool summary embeddings (+44% semantic recall@10). AND→OR priority FTS5 query logic. Hybrid search trusts FTS5 ranking when it finds sufficient candidates
- **`siftd query` is ~50× faster** — Covering index on `response_attributes`, two-phase query, `WhereBuilder` JOIN tracking, `EXISTS` subquery for model filter, materialized `conversation_stats` table. Default query from ~3.5s to ~70ms
- **Storage test coverage** — 18.4% → 100% via 27 autoresearch runs. All 10 storage modules at 100% coverage
- **Adapter test coverage** — Per-adapter test files split from monolith. Claude Code 99.3%, Codex CLI 99.5%, VSCode 100%, OpenCode 99.4%, cross-format normalizer validation (50 tests)
- **CLI refactored to package** — `cli/` is now a proper package with focused submodules per command
- **Dead config removed** — `search.formatter` and `search.serve_delegate` config keys removed (superseded by Operation IR and `serve.delegate`)
- **Structured error responses** — `_dispatch()` catches exceptions and returns JSON errors instead of raw tracebacks
- **`~110 lines removed`** — Dead `_delegate_search_via_serve` code path removed

### Fixed

- **Cost coverage** — Pricing JOIN routed through harness source as fallback. NULL-safe cost expression (missing pricing → NULL, not 0.0). Bundled pricing seed for 10 models. Estimated 50% → 75% cost coverage
- **Editable install detection** — `siftd upgrade` now detects editable `uv tool` installs
- **HTML route escaping** — XSS-relevant escaping bugs fixed in html_routes
- **Connection leak** — Fixed in html_routes detail endpoint
- **`embed_installed()` / `_serve_installed()`** — Use `importlib.util.find_spec` instead of try/import to avoid side effects
- **`rename_tag` signature** — `conn` moved to keyword-only, consistent with other tag functions
- **Serve auth config loading** — `get_config()` returns None for dicts; fixed to use `get_config_table()`

## [0.5.5] - 2026-03-20

### Changed

- **Ingest is ~3× faster** — Full fresh ingest dropped from ~115s to ~38s (67% reduction) across ~6,400 files. Optimizations:
  - Cache workspace identity lookups to avoid repeated `git remote` subprocess calls
  - SQLite WAL mode with tuned pragmas (`synchronous=OFF` during bulk ingest, 64MB cache, 256MB mmap, deferred foreign keys)
  - In-process vocabulary caches for harness/provider/model/tool/tag lookups
  - Batched `os.urandom()` and unrolled encoding for ULID generation
  - `hashlib.file_digest()` for file hashing (Python 3.11+)
  - `INSERT OR IGNORE` for tag application instead of SELECT+INSERT
  - Early `len()` check in binary content filter to skip regex on short strings

## [0.5.4] - 2026-03-20

### Added

- **Multi-harness skill install** — `siftd install skill` now supports `--harness` to install the siftd skill/instructions for different agents: Claude Code (default), Pi Agent, Codex CLI, Gemini CLI, Copilot CLI, Aider. Claude Code and Pi get the structured skill (SKILL.md + reference/); other harnesses get a rendered plain-markdown instructions file.
- **`siftd install skill`** — Lightweight alternative to the full plugin. Installs just the /siftd decision tree and reference docs without hooks or commands.
- **`/siftd:query` and `/siftd:peek` commands** — New slash commands for browsing conversations and viewing live sessions.
- **Stop hook** — Auto-runs `siftd ingest -a claude_code` on session exit (~0.7s) to apply pending tags queued during the session.
- **Per-session hint dedup** — PostToolUse hints fire once per subcommand per session via marker files, reset on SessionStart.
- **Bare `siftd install`** — Shows available components and supported harnesses instead of an argparse error.

### Fixed

- **Live tagging bug** — `session-start.sh` now registers sessions unconditionally (not gated on `reason` field detection, which was fragile across Claude Code versions). Added DB fallback in `--current` session detection with stderr feedback when falling back to `--last 1`.
- **Subagent pending tags** — Tags queued against a parent session ID now apply to subagent conversations. When a subagent conversation is ingested, `_apply_pending_tags` falls back to the parent session ID (strips `::agent::` suffix) if no tags match the subagent's own external_id.
- **Single-scope harness defaulting** — Harnesses with exactly one supported scope (copilot_cli, aider) auto-default to it instead of failing when `--scope user` is the implicit default.
- **Symlink cleanup on plugin install** — Plugin install now removes symlinked standalone skills (was skipping them, causing duplicate /siftd entries).
- **`conversation_stats` commit convention** — `ensure_conversation_stats_table` and `rebuild_conversation_stats` now follow the project `commit=False` convention.

### Changed

- **Slimmed SKILL.md** — Reduced from 305 to 70 lines. SKILL.md is now a decision tree; exhaustive flag lists live in `reference/*.md`.
- **Tightened hook sensitivity** — Removed 16 generic patterns from UserPromptSubmit (false-positive-prone phrases like "what did we", "last time"). Kept only explicit "siftd" mentions and "past/earlier/previous session/conversation".
- **Commands stripped of static hints** — No more "Next steps" boilerplate in command output; PostToolUse hook provides contextual, deduplicated hints.
- **Plugin version** — Bumped to 1.1.0.

## [0.5.3] - 2026-03-20

### Fixed

- **`siftd query` is ~50× faster** — `siftd query` dropped from ~3.5s to ~70ms. Several compounding issues fixed:
  - Added covering index on `response_attributes(key, response_id, value)`, eliminating a full 479K-row table scan on every query
  - Rewrote `list_conversations` as a two-phase query: Phase 1 identifies conversation IDs cheaply; Phase 2 computes stats only for matched rows
  - `WhereBuilder` now tracks which JOINs each filter actually needs — the default query (no `--model`) no longer scans 363K response rows
  - `--model` filter rewritten from a JOIN to an `EXISTS` subquery that stops at first match
  - Added `conversation_stats` materialized table, rebuilt at the end of each `siftd ingest`. Query reads precomputed counts, tokens, model, and cost from a single row per conversation instead of aggregating the responses table on the fly. `siftd query --limit 0 --since 30d` (1600+ conversations) dropped from ~3s to ~46ms.

## [0.5.2] - 2026-03-19

### Fixed

- **`siftd upgrade` on Homebrew** — Runs `brew update` before `brew upgrade` so the tap formula is current. Suppresses stale "update available" notice after successful upgrade.

## [0.5.1] - 2026-03-19

### Changed

- **Export rewrite** — `siftd export` now renders full conversation exchanges as markdown by default (both user and assistant sides). Previous default showed only user prompts.
- **Narrative-aware rendering** — Export walks the full NarrativeBlock structure from the DB instead of collapsing to flat text. Thinking blocks show as `*[thinking]*` placeholders, tool calls as consolidated summaries like `*[file.read ×6, shell.execute ×2]*`
- **New export flags** — `--thinking` expands thinking blocks, `--tools` expands tool inputs/results, `--full` enables both, `--brief` truncates long text, `--json` for structured output. Timestamps included per turn.
- **Breaking:** Removed `--format`, `--prompts-only` flags. Old `prompts` and `exchanges` formats replaced by single markdown format.

### Fixed

- **Homebrew formula missing transitive deps** — Formula generator now walks the full dependency tree (BFS), fixing `wcwidth` missing from painted

## [0.5.0] - 2026-03-19

### Added

- **`siftd upgrade`** — Check for and install updates. Detects install method (uv tool, pipx, Homebrew, pip) and runs the right upgrade command. `--check` flag for check-only mode
- **Passive update check** — After any command, a background thread checks PyPI once every 24 hours. If a newer version exists, a one-line notice prints to stderr on the next invocation. Disable with `siftd config set update.check false` or `SIFTD_NO_UPDATE_CHECK=1`
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
- **Tool-specific presenters** — file.read, file.edit, file.write, shell.execute, search.grep, file.glob, and ui.todo render structured hints instead of raw input dumps
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

[Unreleased]: https://github.com/kgruel/siftd/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/kgruel/siftd/compare/v0.6.4...v0.7.0
[0.5.5]: https://github.com/kgruel/siftd/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/kgruel/siftd/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/kgruel/siftd/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/kgruel/siftd/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/kgruel/siftd/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/kgruel/siftd/compare/v0.4.7...v0.5.0
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
