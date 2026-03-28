# Extract Tag Mutation To API Layer

## Objectives

- Extract tag mutation orchestration from `serve/routes.py:tag_write_route()` and `cli/tags.py:cmd_tag()` into API-layer functions.
- Keep behavior and contracts stable:
  - no user-facing CLI behavior changes
  - no `POST /api/v1/tag` JSON shape changes
- Reduce tag-mutation-related `serve -> storage` coupling (direct SQL + direct DB lifecycle in route).

## Current Flow Snapshot

- `src/siftd/serve/routes.py:204` currently owns:
  - action routing (`apply/remove/rename/delete`)
  - DB open/commit/close
  - cross-owner SQL guard (`_tag_used_by_other_owners`)
  - entity resolution (`last` / `entity_id`) and mutation loops
- `src/siftd/cli/tags.py:438` currently duplicates apply/remove orchestration for local fallback.
- `src/siftd/api/tags.py` exposes primitives (`apply_tag`, `remove_tag`, `rename_tag`, `delete_tag`) but no orchestration contract.

## Proposed API Surface (Focused, Not Kitchen-Sink)

Add three orchestration functions in `src/siftd/api/tags.py`.

```python
def apply_tags(
    *,
    db_path: Path,
    tags: list[str],
    entity_type: Literal["conversation", "workspace", "tool_call"] = "conversation",
    entity_id: str | None = None,
    last: int | None = None,
    owner: str | None = None,
    remove: bool = False,
) -> ApplyResult

def rename_tag_safe(
    *,
    db_path: Path,
    old_name: str,
    new_name: str,
    owner: str | None = None,
) -> RenameResult

def delete_tag_safe(
    *,
    db_path: Path,
    tag_name: str,
    owner: str | None = None,
) -> DeleteResult
```

### Result types

Use explicit dataclasses for action results and a small serializer adapter in serve route to preserve existing JSON payload shape:

- `ApplyResult` -> `{ "action": "apply"|"remove", "results": [...] }`
- `RenameResult` -> `{ "status": "renamed", "old_name": ..., "new_name": ... }`
- `DeleteResult` -> `{ "status": "deleted", "tag_name": ... }`

No flattened union kwargs; each function accepts only meaningful parameters.

## Connection Lifecycle Ownership

These API orchestration functions accept `db_path` only and own connection lifecycle end-to-end:

- open connection
- perform validation and mutation
- commit/rollback as needed
- close connection

Callers (CLI/serve) do not pass `conn` and do not manage transactions for these mutations.

## Ownership Validation Placement

Move cross-owner SQL logic out of serve route into API-owned data access:

- Add storage helper (preferred):
  - `storage.tags.tag_used_by_other_owners(conn, tag_id, owner) -> bool`
- `rename_tag_safe` and `delete_tag_safe` call this helper before mutating when `owner` is provided.

This removes tag-mutation-specific direct storage SQL usage from serve route.

## `--last` / Entity Resolution Responsibility

- CLI responsibility (caller-level):
  - keep existing argparse normalization quirks exactly as-is (`--last` string/int coercion, `--current/--session` behavior)
- API responsibility (orchestration-level):
  - `apply_tags` resolves concrete target IDs from `last` or `entity_id`
  - applies owner scoping via existing API resolution helpers
  - performs batch apply/remove loops and status accounting

This preserves CLI UX while centralizing mutation state machine logic.

## Serve Route After Extraction

`tag_write_route` becomes thin action-to-API delegation:

1. parse/validate JSON body shape
2. resolve `owner = _effective_owner(...)`
3. dispatch by action:
   - `apply/remove` -> `apply_tags(..., remove=...)`
   - `rename` -> `rename_tag_safe(...)`
   - `delete` -> `delete_tag_safe(...)`
4. serialize result dataclass to current endpoint JSON shape
5. keep stats-cache refresh as route concern

No direct tag-mutation SQL and no direct mutation transaction logic in route.

## CLI After Extraction

`cmd_tag()` and subcommands keep argument parsing and user messaging, but local fallback mutation logic simplifies to API calls:

- apply/remove local path -> `apply_tags(...)`
- rename local path -> `rename_tag_safe(...)`
- delete local path (after existing `--force` UX guard) -> `delete_tag_safe(...)`

This eliminates duplicated mutation loops/transaction control in CLI.

## Incremental Rollout

### Phase 1: Introduce apply/remove orchestration API

- Add `ApplyResult` + `apply_tags(...)`.
- Implement target resolution + loops + transaction ownership in API.
- Add tests for apply/remove parity.

### Phase 2: Migrate CLI apply/remove fallback

- Replace local apply/remove orchestration in `cmd_tag()` with `apply_tags(...)`.
- Preserve current print/exit behavior.

### Phase 3: Add safe rename/delete orchestration + ownership helper

- Add `RenameResult`, `DeleteResult`, `rename_tag_safe`, `delete_tag_safe`.
- Move cross-owner SQL into storage helper and call from API.
- Migrate serve rename/delete branches and CLI rename/delete fallback to API calls.

### Phase 4: Serve route thinning + cleanup

- Convert `tag_write_route` to action delegation + serialization only.
- Remove dead route-side helper SQL and mutation loops.
- Re-run architecture checks; tag-mutation-related serve->storage violations should drop.

### Phase 5: Evaluate write-side IR (deferred)

- Do **not** commit to write-IR migration in this task.
- After phases 1-4 and after write patterns are established more broadly, evaluate whether `POST /api/v1/tag` should move to Operation IR.

## Testing Strategy

### Behavior parity tests

- API tests for each orchestration function:
  - success + validation failures
  - owner-scoped safeguards for rename/delete
  - `last`/`entity_id` resolution behavior
- Serve tests for exact `/api/v1/tag` response JSON shapes and status behavior.
- CLI tests to confirm same output + exit codes with delegation on/off.

### Anti-drift serializer tests

For any dataclass result serialized by serve, add anti-drift tests mirroring the serializer strategy used elsewhere:

- compare serializer output keys against `dataclasses.fields()` for:
  - `ApplyResult`
  - `RenameResult`
  - `DeleteResult`
- if nested result item dataclasses exist, apply the same key parity assertion to nested serializers.

This guards against field drift between API result types and serve JSON payloads.

## Expected Architecture Impact

- Tag mutation orchestration is API-owned.
- Serve route no longer embeds tag mutation SQL/transaction logic.
- CLI no longer duplicates batch mutation state machine.
- `serve -> storage` violations tied to tag mutation are reduced, isolating any remaining non-tag serve-storage coupling as separate follow-up work.
