# Operation IR Rollout Plan

Operation IR proven on `cmd_query` list mode (commit bc94b8c). This plan
covers migrating remaining commands to the pattern.

## Pattern per command

```python
op = Operation(path=..., method=..., fn=..., params={...},
               render_method=..., fidelity=..., db=db)
result = try_serve(op) or execute(op)
output = render(result, op, fmt=select_format(...))
```

## Commands to migrate

### Tier 1 — straightforward, same shape as query list (all done)

| Command | fn | render_method | Notes |
|---------|-----|---------------|-------|
| `db stats` | `get_stats` | `stats` | **Done.** 3-tier fallback via Operation |
| `db workspaces` | `list_workspaces` | `raw` | **Done.** list_workspaces adapted for db_path |
| `tools` (summary) | `get_tool_tag_summary` | `raw` | **Done.** Two modes, two Operations |
| `tools` (by-ws) | `get_tool_tags_by_workspace` | `raw` | **Done.** Same command, different fn |
| `tag list` | `list_tags` | `raw` | **Done.** Simple listing; drill-down stays as-is |

### Tier 2 — need minor adaptation

| Command | fn | render_method | Notes |
|---------|-----|---------------|-------|
| `query <id>` | `get_conversation` | `detail` | **Done.** Fidelity-dependent; --json delegates |
| `export` | `export_conversations` | `detail` | **Done.** Multiple conversations; --json delegates |
| `tool-search` | `search_tool_calls` | `raw` | **Done.** Returns (query_obj, results) tuple |
| `search` | `hybrid_search` | `search` | **Deferred.** Multi-step pipeline (filter→FTS5→embed→rerank) needs `hybrid_search()` API extraction first |

### Tier 3 — writes (tag apply/remove/rename)

| Command | fn | method | Notes |
|---------|-----|--------|-------|
| `tag apply` | apply_tag loop | POST | Already delegates via try_delegate_post |
| `tag remove` | remove_tag loop | POST | Same route, different action |
| `tag rename` | rename_tag | POST | Already delegates |

### Not migrating

- `ingest` — write pipeline, not an Operation
- `backfill` — batch mutation, not query-shaped
- `migrate` — schema migration
- `peek` — filesystem, not DB
- `db vacuum/backup/restore` — infrastructure ops
- `db push/pull/send/receive` — sync ops with binary I/O

## Serve route convergence

When migrating serve routes, replace hand-wired `list_conversations(...)`
calls with `execute(op)` + `render(result, op, fmt=json_fmt)`. This makes
routes use the same path as CLI, just with JSON format selected.

For routes that currently use `serialize_conversation_list()` directly,
the render path through json_fmt already delegates to serialization —
so the behavior is identical, just expressed through the Operation pattern.

## Serve param remapping

Operation params use API fn kwargs (`limit`, `tags`, `conversation_id`).
Serve routes use HTTP conventions (`n`, `tag`, `id`). `try_serve()` remaps
via `_SERVE_PARAM_MAP` in `serve/delegation.py`:

    limit → n, last → n, conversation_id → id, conversation_ids → id,
    query → q, tags → tag, exclude_tags → no_tag, oldest_first → oldest

Unknown params (e.g. `include_thinking`) pass through harmlessly — serve
routes ignore unrecognized query params.

## FilterArgs migration

Move FilterArgs to `domain/` layer so both CLI and serve can import it.
Currently in `cli_filters.py` (CLI layer). The `from_query_params()`
classmethod was added but serve can't import it yet due to layer boundary.

Alternative: keep FilterArgs in CLI, add a parallel constructor in the
serve route that builds the same dict shape. The Operation params dict
is the real shared type, not FilterArgs itself.

## Verification

After each command migration:
1. `./dev check` passes
2. Arch tests pass (no new import violations)
3. `siftd serve &` + command works (delegation path)
4. Without serve, command works (local path)
