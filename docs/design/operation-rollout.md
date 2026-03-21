# Operation IR — Status & Next Steps

## Current state

All 12 CLI commands migrated to the Operation IR pattern. Every command
builds an Operation and uses `try_serve(op)` / `execute(op)`.

```python
op = Operation(path=..., method=..., fn=..., params={...},
               render_method=..., fidelity=..., db=db)
result = try_serve(op) or execute(op)
output = render(result, op, fmt=select_format(...))
```

### Operations inventory

| Path | Method | fn | render | Module |
|------|--------|----|--------|--------|
| `/v1/stats` | GET | `get_stats` | stats | cli_meta |
| `/v1/workspaces` | GET | `list_workspaces` | raw | cli_meta |
| `/v1/tools` | GET | `get_tool_tag_summary` | raw | cli_query |
| `/v1/tools` | GET | `get_tool_tags_by_workspace` | raw | cli_query |
| `/v1/query` | GET | `get_conversation` | detail | cli_query |
| `/v1/query` | GET | `list_conversations` | list | cli_query |
| `/v1/search` | GET | `hybrid_search` | search | cli_search |
| `/v1/tool-search` | GET | `search_tool_calls` | raw | cli_tool_search |
| `/v1/tags` | GET | `list_tags` | raw | cli_tags |
| `/v1/tag` | POST | `rename_tag` | raw | cli_tags |
| `/v1/tag` | POST | `apply_tag`/`remove_tag` | raw | cli_tags |
| `/v1/export` | GET | `export_conversations` | detail | cli_export |

### Infrastructure

- `api/dispatch.py` — Operation dataclass, `execute()`, `render()`, `dispatch()`
- `serve/delegation.py` — `try_serve()` with `_SERVE_PARAM_MAP` (GET remaps, POST doesn't)
- `_SERVE_ONLY_KEYS` = `{action, embeddings_only}` — stripped by `execute()`

### Not migrating

- `ingest` — write pipeline, not an Operation
- `backfill` — batch mutation, not query-shaped
- `migrate` — schema migration
- `peek` — filesystem, not DB
- `db vacuum/backup/restore` — infrastructure ops
- `db push/pull/send/receive` — sync ops with binary I/O

---

## Next steps — dependency graph

```
                    ┌──────────────────┐
                    │ param alignment   │  ← independent
                    └──────────────────┘

┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│   fidelity   │───▸│  render methods   │───▸│   dispatch()  │
└─────────────┘     └──────────────────┘     └──────────────┘
                                                     │
                                              ┌──────▼───────┐
                                              │  route gen    │
                                              └──────────────┘
```

Sequential: fidelity → render methods → dispatch → route generation.
Independent: param alignment (can happen anytime).

### Phase 1: Fidelity audit

7 of 12 Operations use `Fidelity()` placeholder. Audit which actually
benefit from depth/visibility controls vs which are truly passthrough:

| render_method | Operations | Fidelity needed? |
|---------------|-----------|-----------------|
| `raw` (7) | tools, workspaces, tag list, tool-search, tag writes | Probably not — flat data |
| `detail` (2) | query detail, export | Yes — already wired |
| `list` (1) | query list | Yes — already wired |
| `search` (1) | search | Yes — already wired |
| `stats` (1) | db stats | Maybe — depth could control section visibility |

The `raw` Operations may never need fidelity. The question is whether
`raw` dissolves into specific render methods (e.g. `render_tools`,
`render_tags`) or stays as passthrough forever.

### Phase 2: Render methods on format protocol

Move rendering from scattered `print()` in each CLI module into format
protocol methods. For each `raw` Operation, either:

1. Add `render_{name}` to the format protocol (terminal_fmt, json_fmt, markdown_fmt)
2. Keep `raw` — the data is simple enough that the CLI prints it directly

Candidates for format protocol:
- `render_stats` — already exists on json_fmt; add to terminal_fmt
- `render_tools` — summary + by-workspace modes
- `render_workspaces` — workspace list
- `render_tags` — tag list with counts
- `render_tool_search` — grouped/ungrouped results

This is where CLI modules shrink. cmd_status goes from 80 lines of
print statements to `dispatch(op, fmt=fmt)`.

### Phase 3: Use dispatch()

Once render methods work, the per-command pattern simplifies:

```python
result = try_serve(op)
if result is not None:
    # deserialize serve response to domain objects
    ...
else:
    result = dispatch(op, fmt=select_format(...))
```

Or for commands where serve returns pre-rendered JSON:
```python
result = try_serve(op) or dispatch(op, fmt=fmt)
```

### Phase 4: Serve route generation

Derive routes from Operation definitions. A route becomes:

```python
@operation_route("/v1/query", method="GET")
async def query(params) -> dict:
    op = Operation.from_http(params)
    return dispatch(op, fmt=json_fmt)
```

Depends on:
- dispatch() working end-to-end (Phase 3)
- Content negotiation design (Accept header → format selection)
- FilterArgs layer placement (domain/ so serve can import it)

### Independent: Param alignment

`_SERVE_PARAM_MAP` has 10 entries bridging API kwargs ↔ HTTP conventions:

    limit → n, query → q, tags → tag, exclude_tags → no_tag,
    conversation_id → id, conversation_ids → id, last → n,
    oldest_first → oldest, lambda_ → lambda, backend_name → backend

`_SERVE_ONLY_KEYS` has 2 entries: `{action, embeddings_only}`

Options:
1. **Standardize route params** to match API kwargs — dissolves the map
   but is a breaking HTTP API change
2. **Accept the mapping** as the cost of HTTP conventions — it's
   explicit, centralized, and only grows when new params diverge
3. **Move to POST bodies for complex queries** — POST bodies already
   skip remapping. Complex filter sets could POST instead of GET.

Current friction is low. Revisit if the map exceeds ~15 entries.

## Remaining cleanup

- `_search_fts_only` — ~80 lines, called for explicit `--fts` flag.
  Could dissolve into `hybrid_search(mode="fts")` + main render path
  once FTS-specific rendering (unsupported-flag warnings, `mode="fts5"`
  JSON annotation) moves to the format protocol.
