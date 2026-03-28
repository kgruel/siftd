# lossless-serializers plan

## Goal
Make serve serialization lossless for the affected API dataclasses, remove local-vs-delegated schema drift, and keep endpoint/CLI compatibility.

## Constraints to preserve
- Serve JSON contracts are additive only (add keys, do not remove/rename existing keys).
- CLI output format stays the same for users (especially `siftd tool-search --json`).
- Keep serialization logic in `src/siftd/serialization/` (no serializer logic in serve routes).

## Current drift map

### 1) Tags (`TagInfo`)
- Dataclass fields (`src/siftd/api/tags.py`):
  - `name`, `description`, `created_at`, `conversation_count`, `workspace_count`, `tool_call_count`, `prompt_count`
- Serve serializer (`src/siftd/serialization/serve_fmt.py::render_tags`) currently omits:
  - `description`, `created_at`
- CLI delegated rehydrate (`src/siftd/cli/tags.py`) compensates with defaults:
  - `description=t.get("description")`
  - `created_at=t.get("created_at", "")`

### 2) Tool search (`ToolSearchResult` + payload shape)
- Dataclass fields (`src/siftd/api/tool_search.py`):
  - `tool_call_id`, `conversation_id`, `response_id`, `timestamp`, `tool_name`, `tool_family`, `status`, `path`, `basename`, `ext`, `command`, `command_verb`, `pattern`, `arg`, `result_snippet`, `workspace_path`, `rank`
- Serve serializer (`render_tool_search`) currently omits:
  - `response_id`, `ext`, `pattern`, `arg`
- CLI local JSON path (`src/siftd/cli/tool_search.py`) emits:
  - `query`, `fields`, `bare_terms`, `unknown_fields`, `results`, optional `groups`
- CLI delegated JSON path currently prints serve payload directly, which is a different schema.

### 3) Stats (`DatabaseStats`)
- Serialization already goes through canonical `serialize_stats()`.
- Deserialization currently uses private `_dict_to_stats` in `src/siftd/api/stats.py`.
- CLI imports private symbol across boundary (`src/siftd/cli/meta.py`).

## Design decisions

### A) Dataclass-first serialization contract (strict rule)
- API dataclasses are the source of truth for wire shape everywhere.
- Serializers must derive their field set from dataclass definitions (not hand-maintained key lists).
- Transformations are allowed (e.g., `Path` -> `str`, tuple -> list), but field presence is still governed by the dataclass contract.
- `dataclasses.asdict()` is acceptable inside `serialization/` as the baseline mechanism; any explicit mapping is for value coercion only, not field selection.
- This follows architecture rules (ban is only for `serve/routes.py`, not `serialization/`).

### B) Remove default-based rehydrate
- Prefer shared, strict deserializers over per-call `.get(..., default)`.
- Fail fast on missing required keys instead of silently fabricating defaults.
- CLI should only consume raw dict directly when the command is intentionally passthrough JSON.

### C) Deserializer location (explicit)
- Deserializers live in `api/` modules alongside the dataclass definitions.
- `serialization/` remains the outbound serialization boundary used by serve/output.
- CLI consumes API deserializers (stays inside allowed layer dependencies), not ad-hoc local rehydrate code.
- For stats specifically: promote `_dict_to_stats` to public `dict_to_stats` and keep `_dict_to_stats` as a compatibility alias during migration.

## Planned changes by concept

### 1) Tags (independent, low risk)
1. Update `render_tags` to include all `TagInfo` fields (lossless), with keys derived from `TagInfo` dataclass fields.
2. Add a strict API deserializer in `api/tags.py` (e.g., `tag_info_from_dict`) and use it in CLI delegated paths.
3. Keep text output unchanged; additional fields are internal/JSON completeness only.
4. Add tests:
   - serve payload includes `description` and `created_at`
   - delegated tag-list path no longer relies on fabricated defaults.

### 2) Tool search (independent, medium risk)
1. Update `render_tool_search` result item serialization to include full `ToolSearchResult` field set, derived from the dataclass contract.
2. Expand top-level serve payload to include parsed query components needed for parity:
   - `fields`, `bare_terms`, `unknown_fields` (additive; keep existing `query`/`result_count`).
3. Add strict API deserializers in `api/tool_search.py` (for `ToolSearchResult` and parsed payload shape).
4. Refactor CLI `tool-search --json` to use one shared payload builder for both local and delegated paths:
   - canonical CLI shape remains `query + fields + bare_terms + unknown_fields + results (+groups when grouped)`.
   - delegated path should deserialize/normalize first, then emit via same builder as local path.
5. Keep endpoint compatibility by retaining existing serve keys (`query`, `result_count`, `results`) while adding new ones.
6. Add tests:
   - serve formatter key completeness for `ToolSearchResult`
   - delegated vs local CLI JSON schema equality test (same keys and field presence).

### 3) Stats (independent, low risk)
1. Introduce public `dict_to_stats`/`deserialize_stats` in `api.stats`.
2. Keep `_dict_to_stats` as thin alias (deprecation path) for compatibility.
3. Update `cli/meta.py` to import public symbol only.
4. Update tests to target public deserializer path; keep round-trip coverage.

## Anti-drift pattern (reusable project principle)
Treat dataclass-contract tests as a reusable policy for any new serialized dataclass.

Pattern:
1. For each serialized dataclass, assert serializer output keys == `dataclasses.fields(cls)`.
2. For transformed values, assert coercion semantics separately (type/value tests), not by dropping fields.
3. For delegated CLI flows, add parity tests ensuring delegated JSON == local JSON contract.

Initial coverage:
- `TagInfo` serializer keys == `dataclasses.fields(TagInfo)`
- `ToolSearchResult` serializer keys == `dataclasses.fields(ToolSearchResult)`
- `DatabaseStats` top-level and nested contract checks (`TableCounts`, `TokenCoverage`, etc.)
- Delegated/local JSON parity for tool-search.

Follow-on adoption:
- Apply this same pattern to new dataclasses introduced by search unification work (and any other new serialization surfaces) so drift is caught by default.

## Migration strategy
- No DB/data migration required.
- Ship in three small PRs/commits (tags -> tool-search -> stats), each independently releasable.
- Each step is additive for serve JSON and should not break existing clients.

## Acceptance criteria
- Tags: serve `/api/v1/tags` includes `description` and `created_at`; CLI behavior unchanged.
- Tool search: serve results include all `ToolSearchResult` fields; `siftd tool-search --json` output is schema-stable across local and delegated execution.
- Stats: CLI no longer imports private `_dict_to_stats`; public API deserializer exists and is used.
- Anti-drift dataclass-contract tests are added as a reusable pattern and fail when fields are added without serializer/deserializer updates.
