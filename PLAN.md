# Unify Search Pipeline: Move Post-Processing to API

## Scope and constraints

- Keep CLI behavior identical (flags, warnings, output shapes, tip/privacy messages).
- Keep `/api/v1/search` contract stable.
- API is the boundary: CLI/serve consume API primitives; no domain SQL or search-domain helper logic in CLI.
- Prefer moving existing logic over rewriting algorithms.

## Current state (verified)

- CLI owns domain post-processing (`_fetch_search_metadata`, `_enrich_context`, threshold/first, aggregation, thread tiering, file-ref/exchange/context enrichment).
- API retrieval returns `list[dict]`, but some API helpers expect object attributes.
- `ScoreBreakdown` lives in `siftd.search`, creating storage/output coupling cycles.
- `--fts` bypasses IR via a separate CLI path.

## Design decisions

### 1) Unified result type: mutable dataclasses in a neutral module

Use dataclasses as the canonical internal representation.

Location:
- `src/siftd/domain/search_types.py`

Core types:
- `ScoreBreakdown`
- `SearchChunk`
- `ConversationSearchSummary`

`SearchChunk` requirements:
- **Not frozen** (mutable).
- Retrieval fields are required (id/score/text/chunk/source_ids/etc.).
- Enrichment targets are optional and start as `None` (for example: `workspace_path`, `started_at`, `file_refs`, `exchanges`, `context_window`).
- Enrichment functions mutate these optional fields in place.

Compatibility:
- Keep temporary adapters at boundaries that still expect dicts.
- Keep compatibility aliases for old public names during migration window.

### 2) API surface is primitives; no consumer-specific wrappers

No `prepare_cli_*` API functions.

Expose composable API primitives only:

```python
def search_chunks(..., mode: Literal["fts", "hybrid", "semantic"], ...) -> list[SearchChunk]

def filter_by_threshold(results: list[SearchChunk], *, threshold: float | None) -> list[SearchChunk]

def first_mention(results: list[SearchChunk], *, threshold: float = 0.65, db_path: Path | None = None) -> SearchChunk | None

def enrich_search_metadata(conn: sqlite3.Connection, results: list[SearchChunk]) -> None

def enrich_file_refs(conn: sqlite3.Connection, results: list[SearchChunk]) -> None

def enrich_exchanges(conn: sqlite3.Connection, results: list[SearchChunk]) -> None

def enrich_context_window(conn: sqlite3.Connection, results: list[SearchChunk], n: int) -> None

def sort_chunks_by_time(results: list[SearchChunk]) -> list[SearchChunk]

def aggregate_by_conversation(results: list[SearchChunk], *, limit: int = 10) -> list[ConversationSearchSummary]

def compute_thread_tiers(results: list[SearchChunk]) -> tuple[list[SearchChunk], list[SearchChunk]]
```

### 3) Enrichment order dependency is explicit and documented

Compose primitives in this order to avoid drift:
1. Retrieval (`search_chunks`)
2. Relevance gates (`filter_by_threshold`, `first_mention`)
3. Metadata enrichment (`enrich_search_metadata`)
4. File ref enrichment (`enrich_file_refs`)
5. Optional content enrichment (`enrich_exchanges` or `enrich_context_window`)
6. View shaping (`aggregate_by_conversation` or `compute_thread_tiers`)
7. Optional ordering (`sort_chunks_by_time` for chunk view)

No wrapper types per stage; enrichment is in-place mutation on `SearchChunk`.

### 4) Break cycle by relocating `ScoreBreakdown`

Move `ScoreBreakdown` source of truth to `domain/search_types.py`.

Update imports:
- `storage/embeddings.py` -> `domain.search_types`
- `search.py` -> `domain.search_types`
- `output/json_fmt.py` -> `domain.search_types` (or structural `to_dict` protocol)

This removes `search <-> storage.embeddings` and `output -> search -> storage` coupling.

### 5) FTS-only path uses the same IR/API path

- Remove `_search_fts_only` execution path from CLI.
- CLI always executes one `Operation`; pass `mode="fts"` to API retrieval.
- Preserve current user-visible `--fts` behavior (ignored-flag warnings, JSON `mode: "fts5"`, delegation policy).

### 6) Serve route stays simple

- Keep `/api/v1/search` response shape unchanged.
- Keep `embeddings_only` query parameter behavior; map internally to `mode`.
- Serve composes only what it needs (typically retrieval + serialization), not CLI-specific enrichment steps.

### 7) Anti-drift principle for serialization

Apply anti-drift tests for search dataclasses, same pattern as lossless-serializers work:
- Any serializer for `SearchChunk` / `ConversationSearchSummary` must be tested against `dataclasses.fields(...)`.
- Tests fail when dataclass fields change but serializer output mapping is not updated.
- If a field is intentionally transformed/renamed, the test must include explicit mapping allowlists so drift is deliberate, not accidental.

This is a principle, not an optional testing add-on.

## Migration plan (incremental, ~5 phases)

### Phase 1: Types + cycle break

1. Add `domain/search_types.py` with mutable dataclasses.
2. Move `ScoreBreakdown` there and update imports across search/storage/output.
3. Keep compatibility aliases/adapters.

Validation:
- `tests/test_search.py`, `tests/test_output_formats.py`, `tests/test_formatters.py`, architecture import checks.

### Phase 2: Move helpers to API primitives (1:1 moves)

1. Relocate CLI helper logic into API primitives listed above.
2. Make `aggregate_by_conversation` consume dataclass results.
3. Preserve behavior exactly (ordering, thresholds, warnings).

Validation:
- Add/adjust API tests for each primitive.
- Keep existing CLI behavior tests passing.

### Phase 3: Rewire CLI to compose API primitives directly (includes FTS unification)

1. Remove CLI direct SQL + helper implementations.
2. Compose API primitives directly in `cmd_search` by output mode.
3. Route `--fts` through Operation IR + `search_chunks(mode="fts")`.
4. Preserve all current CLI-visible semantics.

Validation:
- `tests/cli/test_search_noembed.py`
- `tests/cli/test_cmd_search.py`
- `tests/test_unified_search.py`
- architecture boundary tests for CLI

### Phase 4: Align serve + html consumers

1. Ensure serve route uses unified retrieval API path with unchanged contract.
2. Update html consumer paths to use dataclass results cleanly.
3. Keep serve composition minimal and consumer-appropriate.

Validation:
- serve route/format tests.

### Phase 5: Cleanup + anti-drift hardening

1. Remove dead compatibility glue no longer needed.
2. Add/lock anti-drift serializer tests for search dataclasses.
3. Document field-mapping policy for intentional serializer transforms.

Validation:
- full `./dev check`.

## Definition of done

- CLI search has no direct SQL and no hidden domain helper logic.
- One canonical dataclass search result model is used internally.
- `--fts`, default, and `--semantic` run through the same IR/API execution path.
- `ScoreBreakdown` no longer originates from `siftd.search`.
- Serve contract remains unchanged.
- Anti-drift tests protect search serializers from silent field drift.
