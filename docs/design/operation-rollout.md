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

### Phase 1: Fidelity audit ✓

**Done.** Results:

| render_method | Operations | Fidelity status |
|---------------|-----------|-----------------|
| `raw` (7) | tools, workspaces, tag list, tool-search, tag writes | Placeholder — correct, fidelity unused |
| `detail` (2) | query detail, export | Real fidelity from args — correct |
| `list` (1) | query list | Real fidelity from args — correct |
| `search` (1) | search | Fixed: was placeholder, now real fidelity flows through Operation |
| `stats` (1) | db stats | Placeholder — escalate to render method if complexity warrants |

**Decision:** `raw` is a legitimate render method (identity function). Don't promote
to format protocol render methods unless output complexity warrants it. Stats and
tool-search are escalation candidates if needed later.

### Phase 2: Render methods — dissolved

Phase 2 dissolves. The three render methods that matter (`detail`, `list`, `search`)
already exist and are wired correctly. `raw` stays as-is — the data is simple enough
that CLI modules handle their own output. Escalate individual Operations to format
protocol methods when their output complexity justifies it (stats, tool-search are
candidates).

### Phase 3: Use dispatch() — dissolved

**Finding:** Every CLI command has meaningful post-processing between
`execute()` and render — threshold filtering, aggregation, enrichment,
mode branches, stderr hints. `dispatch()` (execute + render) is too
simple for any CLI command.

This is correct by design. The two contexts have different patterns:

- **CLI:** `try_serve(op) → execute(op) → post-process → render`
- **Serve:** `Operation.from_http(params) → dispatch(op, fmt=json_fmt)`

`dispatch()` is the serve-side shortcut. HTTP handlers don't do CLI
post-processing. CLI commands are already using the right pattern.

`dispatch()` stays in `api/dispatch.py` for Phase 4 consumption.

### Phase 4: Param alignment

**Done.** Unified CLI/HTTP/API param names. One name per concept flows
through all three contexts:

| Unified | Replaces | Notes |
|---------|----------|-------|
| `n` | `limit`, `last` | `limit` and `last` are CLI sugar |
| `q` | `query` | positional in CLI |
| `tag` | `tags` | list param, `--tag X --tag Y` |
| `no_tag` | `exclude_tags` | `--no-tag X` |
| `id` | `conversation_id`, `conversation_ids` | positional or `--id` |
| `oldest` | `oldest_first` | boolean flag |
| `backend` | `backend_name` | `--backend ollama` |

`_SERVE_PARAM_MAP` reduced from 10 entries to 1: `lambda_` → `lambda`
(Python keyword — only survivor).

`_SERVE_ONLY_KEYS` unchanged: `{action, embeddings_only}`

### Phase 5: Serve route generation

Derive routes from Operation definitions. A route becomes:

```python
@operation_route("/v1/query", method="GET")
async def query(params) -> dict:
    op = Operation.from_http(params)
    return dispatch(op, fmt=json_fmt)
```

Depends on:
- Param alignment decision (Phase 4)
- Content negotiation design (Accept header → format selection)
- FilterArgs layer placement (domain/ so serve can import it)

## Remaining cleanup

- `_search_fts_only` — ~80 lines, called for explicit `--fts` flag.
  Could dissolve into `hybrid_search(mode="fts")` + main render path
  once FTS-specific rendering (unsupported-flag warnings, `mode="fts5"`
  JSON annotation) moves to the format protocol.
