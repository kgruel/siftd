# Project Principles

Extracted from development history via `strata ask`. Each principle links to the conversation where it was established or reinforced, and is tagged for semantic retrieval.

## Retrieve by tag

```bash
strata query -l principles:architecture   # architecture decisions
strata query -l principles:design          # design philosophy
strata query -l principles:cli             # CLI and UX conventions
strata query -l principles:adapters        # adapter patterns
strata query -l principles:testing         # testing philosophy
```

---

## Architecture

> Tag: `principles:architecture`

### Adapters own parsing and raw format knowledge — storage is adapter-agnostic

Each adapter knows its raw format (JSONL, SQLite, markdown). Everything downstream of the adapter is normalized domain objects. Storage never reaches into adapter internals.

- **Adherence:** Adapter returns `Conversation` domain objects, `store_conversation()` receives them
- **Violation:** Storage calling adapter private functions (was `backfill_response_attributes` calling `claude_code._load_jsonl`)
- **Source:** `01KFMBEQQ0E8` (domain model design), `01KFMBEQRGX7` (foundational architecture)

### Domain model mirrors log structure

Adapters hydrate a tree: `Conversation > Prompt > Response > ToolCall`. This mirrors how CLI coding tools actually structure their logs. The adapter's job is to map format-specific structure into this tree.

- **Adherence:** `conv.prompts[].responses[].tool_calls` traversal
- **Violation:** Flat/denormalized adapter output or storage-level relationship assembly
- **Source:** `01KFMBEQQ0E8` ("Option 3 — that's how these end logs are generally going to be available to us")

### Storage is normalized SQLite — not pluggable

Storage is the gravity well. SQL files, FTS5, attributes are all SQLite-specific. No real use case for alternatives. Explicitly deferred.

- **Adherence:** All SQL in sqlite.py, queries as `.sql` files, no abstraction layer
- **Violation:** Adding a Repository pattern or storage interface for speculative portability
- **Source:** `01KFV90WZKWQ` ("cases aren't parallel — there's only one gravity well"), CLAUDE.md

### Separate DB for embeddings

Search/embeddings concerns live in their own SQLite database, keeping them isolated from core storage. This avoids coupling the main schema to embedding-specific concerns.

- **Adherence:** `embeddings.py` manages its own SQLite file
- **Violation:** Mixing embedding tables into the main schema
- **Source:** `01KFV90X6H64` ("separate DB is probably best")

### Query-time computation over stored redundancy

Cost is derived via JOIN, not pre-computed in a column. Avoids stale data and schema coupling. If a value can be computed at query time from existing data, don't store it.

- **Adherence:** Cost computed via `pricing JOIN responses` at query time
- **Violation:** Adding a `cost_usd` column to responses and keeping it in sync
- **Source:** `01KFV90X5YNT`, CLAUDE.md

### Attributes for variable metadata

When the field set varies by provider or adapter, use key/value `*_attributes` tables instead of adding nullable columns. Cache token breakdowns are the canonical example.

- **Adherence:** `response_attributes` for cache_creation_input_tokens, cache_read_input_tokens
- **Violation:** Adding nullable `cache_read_tokens` column to `responses`
- **Source:** `01KFMBEQRGX7` ("cache token breakdowns are exactly the kind of per-response metadata that varies by provider")

### `commit=False` default — caller controls transactions

Storage functions don't auto-commit. The caller (orchestration layer) decides transaction boundaries. This enables batch operations and rollback.

- **Adherence:** `store_conversation(conn, conv, commit=False)`, caller calls `conn.commit()`
- **Violation:** Storage functions that commit internally
- **Source:** `01KFMBEQRGX7` ("B, and commit=False default")

### ULIDs for all primary keys

All entities use ULIDs (Universally Unique Lexicographically Sortable Identifiers). Shared utility in `ids.py`.

- **Adherence:** `_ulid()` from `strata.ids` for all inserts
- **Violation:** Auto-increment integers or UUIDs
- **Source:** CLAUDE.md

### XDG paths

Data at `~/.local/share/strata`, config at `~/.config/strata`. No hardcoded home-relative paths.

- **Source:** CLAUDE.md

---

## Design Philosophy

> Tag: `principles:design`

### Manual first, automate when patterns emerge

Labels are user-applied. Enrichment is deferred. Cost is approximate. Don't build automation until real usage reveals what's worth automating.

- **Adherence:** Tags are user-applied via `strata tag`, shell tags emerged from real usage patterns
- **Violation:** Building auto-labeling or LLM-based enrichment before manual workflows are established
- **Source:** `01KFV90X5YNT` ("labels will be manual via cli cmd for now"), CLAUDE.md

### Approximate is fine when labeled

Approximate cost tracking is useful. Don't over-engineer precision until billing context demands it. Just make sure it's labeled as approximate.

- **Adherence:** Base pricing table, approximate cost in queries, documented as approximate
- **Violation:** Building per-response billing with subscription vs API detection before there's a real need
- **Source:** `01KFV90X5YNT` ("define provider in adapter, base pricing table, call it approximate"), CLAUDE.md

### Defer explicitly — document what and why

"Later, if patterns emerge" is valid. But say it explicitly and write it down. The ROADMAP.md "Explicitly Deferred" table is the canonical pattern.

- **Adherence:** Deferred items listed with rationale in ROADMAP.md
- **Violation:** Silently omitting features or leaving decisions undocumented
- **Source:** `01KFV90X5YNT` ("3 we can defer, not needed for now"), CLAUDE.md

### Flat tags, not hierarchies

Tag hierarchy was analyzed and explicitly rejected. The `namespace:value` convention (e.g., `shell:test`, `principles:architecture`) provides sufficient organization without tree structure.

- **Adherence:** Flat tag strings with colon-separated namespaces
- **Violation:** Building tag parent-child relationships or inheritance
- **Source:** `01KFV90WMMTC` (tag-hierarchy task closed: "flat tags sufficient")

### Vocabulary matters — rename when the word is wrong

When a term doesn't fit, rename it. `label` became `tag` because that's what users call it. `benchmark` became `bench`/`workbench` because it's not rigorous validation.

- **Adherence:** Consistent terminology across CLI, docs, and code
- **Violation:** Mixed terms for the same concept
- **Source:** `01KFV90XJKHM` (label-to-tag rename), `01KFVTV32HX7` (doctor vocabulary)

### Queries are user-defined `.sql` files

The system is a data platform, not a reporting tool. Users write `.sql` files with `$var` substitution and place them in `~/.config/strata/queries/`.

- **Adherence:** Query runner with var detection, listing, and substitution
- **Violation:** Hardcoded report functions or dashboard features
- **Source:** CLAUDE.md

---

## CLI / UX

> Tag: `principles:cli`

### Every command should have tips/hints for the next step

After output, show what the user might want to do next. Progressive disclosure — don't overwhelm, but don't leave users stranded.

- **Adherence:** `strata ask` shows "Tip: Tag useful results for future retrieval: strata tag <id> research:<topic>"
- **Violation:** Command exits silently with no guidance on what to do with the output
- **Source:** `01KFXBA5WJ4Y` ("the tip works, and we should also do simple progressive examples")

### Progressive examples in help text

Help epilogs should show composed workflows, not just flag documentation. Real sequences of real work.

- **Adherence:** `strata query` epilog shows workflow: search -> drill down -> tag -> retrieve tagged
- **Violation:** Help text that only lists `--flag` descriptions
- **Source:** `01KFXBA5WJ4Y` ("simple progressive examples in the help files or more composed ones")

### CLI is a thin wrapper over Python API

Business logic lives in `api/`, `storage/`, `domain/`. CLI functions handle argument parsing and output formatting. This keeps the API usable programmatically.

- **Adherence:** `cmd_*` functions call into `api.conversations`, `storage.sqlite`, etc.
- **Violation:** Business logic (filtering, aggregation, computation) inside `cmd_*` functions
- **Source:** `01KFVTV32HX7` ("provided we continue to maintain a Python API exposed -> CLI as thin wrapper")

### Doctor pattern for diagnostics

Health checks, maintenance, and fixes follow the doctor pattern: discoverable checks, optional fixes, drop-in extensibility.

- **Adherence:** `strata doctor` runs checks, `--list` shows available, `--fix` applies fixes, checks are classes
- **Violation:** One-off maintenance scripts or hardcoded health checks
- **Source:** `01KFVTV32HX7` ("fix/check drop-in pattern is the way"), `01KFVTV3089H`

---

## Adapters

> Tag: `principles:adapters`

### Adapter contract: `can_handle`, `parse`, `discover`, `HARNESS_SOURCE`, `DEDUP_STRATEGY`

Every adapter implements this interface. Registry validates it. No partial implementations.

- **Adherence:** All 4 built-in adapters (claude_code, codex_cli, gemini_cli, aider) follow the contract
- **Violation:** Adapter missing required attributes or methods
- **Source:** CLAUDE.md, `01KFV90WZKWQ`

### Adapter declares dedup strategy

The adapter knows whether its format allows file-level hash dedup or needs session-based "latest wins" (e.g., Gemini overwrites files). The orchestration layer respects the declared strategy.

- **Adherence:** `DEDUP_STRATEGY = "hash"` (claude_code) vs `DEDUP_STRATEGY = "session"` (gemini_cli)
- **Violation:** Storage layer deciding dedup strategy independent of adapter
- **Source:** `01KFMBEQRGX7` ("3b seems to be the way... latest wins", "Adapter control seems correct")

### Drop-in plugin system

New adapters can be added as `.py` files in `~/.config/strata/adapters/` or via entry points. No code changes to core required.

- **Adherence:** Plugin loader in discovery.py, validation in registry.py
- **Violation:** Requiring source code changes to add new adapters
- **Source:** `01KFV90WPS3J` ("adapters should be drop-ins")

---

## Testing

> Tag: `principles:testing`

### Integration tests first

Most code coverage should come from integration tests that exercise real flows through the system: ingest -> store -> query -> verify.

- **Adherence:** `test_integration.py` (end-to-end), `test_api.py` (real SQLite), `test_ingestion.py` (real pipeline)
- **Violation:** Unit-testing internals already covered by integration tests

### Mocks only at edges

Mock external boundaries (filesystem discovery, network). Never mock internal modules. If a test needs internal mocking to pass, that's a design smell.

- **Adherence:** `test_peek.py` mocks `load_all_adapters` (filesystem boundary), `test_exclude_active.py` mocks `list_active_sessions`
- **Violation:** Mocking `store_conversation()` to test an API layer function

### Shared fixtures, no duplication

Common setup lives in `conftest.py`. No copy-pasting `test_db` setup across files.

- **Adherence:** Shared `test_db`, `make_conversation()`, `FIXTURES_DIR` in conftest.py
- **Violation:** Three files each defining their own database fixture

### Parametrize for variant coverage

Use `@pytest.mark.parametrize` instead of copy-pasting test functions with different inputs.

- **Adherence:** `test_models.py` covers 10+ model patterns in 3 parametrized tests
- **Violation:** 10 separate `test_*` functions that differ only in input/expected

### xdist-safe

Tests must be safe for parallel execution. No shared mutable state, no hardcoded paths, no port conflicts. Use `tmp_path`.

- **Adherence:** All tests use `tmp_path`, no global state
- **Violation:** Tests sharing a hardcoded database path

---

## Process

### HANDOFF.md is authoritative project state

Every session starts with "pick up @HANDOFF.md". It's updated at end of sessions to reflect current state.

- **Adherence:** HANDOFF.md reflects latest work, open threads, next steps
- **Violation:** Stale handoff that doesn't match codebase state

### Work in topic branches

Development happens in `wip/<topic>` branches. Subtask merges target main independently.

- **Adherence:** Branch-per-session, subtask worktrees
- **Violation:** Working directly on main

---

## Conversation References

| ID | Workspace | Date | Key Decisions | Tags |
|----|-----------|------|---------------|------|
| `01KFMBEQRGX7` | tbd-v2 | 2026-01-22 | Foundational: domain objects, adapter boundaries, commit=False, transaction design, dedup strategies, attributes pattern | `principles:architecture`, `principles:design`, `principles:adapters` |
| `01KFMBEQQ0E8` | tbd-v2 | 2026-01-22 | Domain model mirrors log structure, storage is separate, adapter returns objects | `principles:architecture` |
| `01KFMBF1PJFS` | tbd-v2 | 2026-01-22 | Protocol-based interfaces, domain object design plan | `principles:architecture` |
| `01KFV90X5YNT` | tbd-v2 | 2026-01-23 | Cost deferral ("approximate is fine"), manual labels, explicit deferral pattern | `principles:design` |
| `01KFV90X6H64` | tbd-v2 | 2026-01-23 | Separate embeddings DB decision | `principles:architecture` |
| `01KFV90WZKWQ` | tbd-v2 | 2026-01-23 | Storage not pluggable ("only one gravity well"), adapter plugin patterns | `principles:architecture`, `principles:adapters` |
| `01KFV90WPS3J` | tbd-v2 | 2026-01-25 | Adapters as drop-ins (like queries/) | `principles:adapters` |
| `01KFV90WMMTC` | tbd-v2 | 2026-01-25 | Flat tags sufficient, tag hierarchy rejected | `principles:design` |
| `01KFV90XJKHM` | tbd-v2 | 2026-01-25 | label -> tag rename, vocabulary matters | `principles:design` |
| `01KFVTV32HX7` | tbd-v2 | 2026-01-25 | Doctor pattern, CLI vocabulary, thin wrapper over API | `principles:cli` |
| `01KFVTV3089H` | tbd-v2 | 2026-01-25 | Doctor check/fix implementation, drop-in extensibility | `principles:cli` |
| `01KFXBA5WJ4Y` | tbd-v2 | 2026-01-26 | Progressive disclosure, tips in output, progressive examples in help | `principles:cli` |
