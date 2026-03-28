# ingest-api-gap Plan: Extract Ingest/Backfill to API Boundary

## Goal
Move ingest and backfill write orchestration behind `siftd.api` so non-CLI consumers (serve/SDK/automation) can trigger the primary write path without importing ingestion/backfill/adapter internals.

## Non-goals
- No behavioral changes to CLI output, flags, warnings, or exit codes.
- No ingestion pipeline rewrite (`siftd.ingestion.orchestration` logic stays as-is).
- No doctor UX redesign.

## Current Boundary Violations (verified)
`src/siftd/cli/data.py` currently imports and orchestrates internals directly:
- `siftd.adapters.registry` (`load_all_adapters`, `wrap_adapter_paths`)
- `siftd.ingestion` / `siftd.ingestion.orchestration` (`ingest_all`)
- `siftd.backfill` (`backfill_*` functions)
- `siftd.doctor.fixes` / `siftd.doctor.view` (doctor support functions)

This makes CLI the only first-class writer for ingest/backfill orchestration and leaves no API primitive for secondary consumers.

## Proposed API Surface

### 1) New ingest API module
Create `src/siftd/api/ingest.py`.

Public primitives:
- `run_ingest(...) -> IngestRunResult`
- `run_rebuild_fts(...) -> IngestRunResult`

Proposed signature shape:
- Writes own lifecycle: accepts `db_path: Path` (not `conn`).
- Supports current orchestrator inputs:
  - `adapter_names: list[str] | None`
  - `scan_paths: list[str] | None`
  - `filter_binary: bool | None`
  - `on_event: Callable[[IngestEvent], None] | None`

Proposed result dataclass:
- `IngestRunResult`
  - `db_path: Path`
  - `db_created: bool`
  - `mode: Literal["ingest", "rebuild_fts"]`
  - `adapters: list[str]`
  - `scan_paths: list[str]`
  - `stats: IngestStats | None` (None for `rebuild_fts`)
  - `elapsed_ms: int`

Errors:
- Add explicit API exception for adapter selection mismatch (for example `AdapterSelectionError`) carrying requested/available names.
- CLI catches this and preserves exact current message/exit code.

### 2) New backfill API module
Create `src/siftd/api/backfill.py`.

Public primitives:
- `run_backfill(...) -> BackfillRunResult`
- Optionally export narrow mode helpers too (`backfill_shell_tags_api`, etc.) if useful for tests, but `run_backfill` is the main write primitive.

Proposed operation enum:
- `Literal["response_attributes", "shell_tags", "derivative_tags", "filter_binary"]`

Proposed result dataclass:
- `BackfillRunResult`
  - `operation: ...`
  - `dry_run: bool`
  - `inserted_attributes: int`
  - `tagged_conversations: int`
  - `shell_tag_counts: dict[str, int]`
  - `filtered: int`
  - `skipped: int`
  - `errors: int`
  - `elapsed_ms: int`

Notes:
- Wrapper only: internally delegates to existing `siftd.backfill.*` functions unchanged.
- CLI keeps current branching/output logic; it only swaps direct backfill imports for API call(s).

### 3) Adapter listing/discovery stance
- Keep adapter listing in existing `siftd.api.adapters` (`list_adapters`, `list_builtin_adapters`).
- Do not expose raw adapter modules as API return values.
- Adapter resolution for ingest execution lives inside `api.ingest.run_ingest` as internal orchestration detail.

## Domain Logic vs CLI UX Split

Move to API (domain orchestration):
- Adapter loading + selection + path overrides.
- Ingest execution (`ingest_all`) and FTS rebuild execution.
- Backfill operation dispatch and mutation execution.
- Stats-cache refresh after ingest.
- Timing/count result construction.

Keep in CLI (presentation/UX):
- Text/JSON renderers (`_IngestTextRenderer`, `_IngestJsonRenderer`).
- User-facing warnings and prose strings (for example `--dry-run ignored without --filter-binary`).
- Exit code mapping and TTY tips.
- Doctor progress rendering and local cache messaging.

## Doctor Fixes Decision
- Keep doctor command orchestration as CLI tooling for now.
- Route mutation actions to API primitives where missing:
  - `_fix_ingest` should call new `api.ingest.run_ingest`.
- Keep `siftd.doctor.fixes` cache file handling CLI-side (stateful UX concern, not shared API need).
- Defer a broader `api.doctor.apply_fixes` abstraction unless a second consumer appears.

## Incremental Implementation Plan

### Phase 1: Ingest API extraction (first, highest impact)
1. Add `api/ingest.py` with `IngestRunResult`, exceptions, and `run_ingest`/`run_rebuild_fts` wrappers.
2. Export new symbols from `api/__init__.py`.
3. Rewire `cmd_ingest` to use API primitives while preserving existing renderers/output.
4. Update CLI tests to monkeypatch API boundary instead of ingestion/registry internals.
5. Remove `cli/data.py` direct imports from ingestion/registry for ingest path.

### Phase 2: Backfill API extraction
1. Add `api/backfill.py` with `BackfillRunResult` and `run_backfill`.
2. Export new symbols from `api/__init__.py`.
3. Rewire `cmd_backfill` to call API.
4. Keep CLI output text identical.
5. Update tests to patch API backfill functions instead of `siftd.backfill.*`.

### Phase 3: Doctor ingest fix boundary cleanup
1. Update `_fix_ingest` in `cli/data.py` to call `api.ingest.run_ingest`.
2. Leave doctor cache/view modules CLI-side.
3. Verify no behavioral change in `doctor fix` output.

### Phase 4: Architecture/test ratchet and anti-drift hardening
1. Update `tests/architecture/test_imports.py`:
   - Remove known violations for `("cli/data.py", "adapters")` and `("cli/data.py", "ingestion")` once eliminated.
   - Ratchet `max_allowed` down accordingly.
2. Add API module tests:
   - `tests/test_api_ingest.py`
   - `tests/test_api_backfill.py`
3. Add anti-drift serializer-contract tests for new result dataclasses:
   - Verify serialized key set equals `dataclasses.fields(...)` for each new payload type.
   - Follow pattern used in `tests/test_serialization_tags.py` and `tests/test_search_serializer_drift.py`.

## Behavioral Compatibility Checklist
- `siftd ingest` text mode unchanged.
- `siftd ingest --json` event stream unchanged.
- `siftd ingest --rebuild-fts` behavior unchanged.
- `siftd backfill` mode-specific text unchanged.
- `siftd doctor fix` still executes the same fix commands and summaries.

## Done Criteria
- CLI no longer imports `siftd.ingestion`, `siftd.backfill`, or `siftd.adapters.registry` in `cli/data.py` for ingest/backfill execution.
- Ingest and backfill write paths are callable via `siftd.api` primitives with `db_path` lifecycle ownership.
- New API result dataclasses have anti-drift tests.
- Existing CLI behavior/output remains unchanged.
