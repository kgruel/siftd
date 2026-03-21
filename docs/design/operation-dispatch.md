# Operation Dispatch — Design Sketch

## The Pattern

```
input context  →  normalize  →  Operation  →  execute  →  render(format, fidelity)
(CLI args)                      (IR)          (API fn)     (terminal/JSON/HTML)
(HTTP params)
(programmatic)
```

## What Already Exists

- `FilterArgs` (cli_filters.py) — normalized filter params, bridges argparse → API
- `Fidelity` (painted) — depth/visibility controls for rendering
- Format protocol (`render_list`, `render_detail`, `render_search`) — output dispatch
- `try_delegate` / `try_delegate_post` — serve delegation
- `select_format(json_mode, is_tty)` — format selection by context

## What's Missing

The **Operation** — the normalized intent that sits between input parsing and execution.
Currently each `cmd_*` function hand-wires: parse args → call API → select format → render.

## Proposed: `siftd/dispatch.py`

```python
@dataclass(frozen=True)
class Operation:
    """Normalized intent — the IR between input and output contexts."""
    fn: Callable              # API function
    params: dict[str, Any]    # kwargs for fn (from FilterArgs or direct)
    render_method: str        # "list" | "detail" | "search" | "stats" | "raw"
    fidelity: Fidelity        # rendering depth
    db: Path                  # resolved database path


def execute(op: Operation) -> Any:
    """Call the API function with params."""
    return op.fn(**op.params)


def render(result: Any, op: Operation, *, format: OutputFormat) -> str | dict:
    """Render through the format protocol."""
    renderer = getattr(format, f"render_{op.render_method}", None)
    if renderer is None:
        # "raw" or unknown → just return the result
        return result
    return renderer(result, op.fidelity)


def dispatch(op: Operation, *, format: OutputFormat) -> str | dict:
    """Execute + render in one call."""
    result = execute(op)
    return render(result, op, format=format)


def try_serve(op: Operation) -> Any | None:
    """Try delegating to serve. Returns result or None."""
    from siftd.serve.delegation import try_delegate
    # Map operation → endpoint path + params
    ...
```

## Endpoint Registry

```python
# endpoints.py — declarative endpoint definitions
from siftd.api import list_conversations, get_conversation, get_stats

ENDPOINTS = {
    "query.list": Endpoint(
        path="/v1/query",
        method="GET",
        fn=list_conversations,
        render_method="list",
        param_keys=["workspace", "model", "since", "before", "tags", ...],
    ),
    "query.detail": Endpoint(
        path="/v1/query",
        method="GET",
        fn=get_conversation,
        render_method="detail",
        param_keys=["id"],
    ),
    "stats": Endpoint(
        path="/v1/stats",
        method="GET",
        fn=get_stats,
        render_method="stats",
        param_keys=[],
    ),
}
```

## How CLI Uses It

```python
# cli_query.py — before (current)
def cmd_query(args):
    db = resolve_db(args)
    filters = extract_filter_args(args)
    fidelity = fidelity_from_args(args)

    # Try delegation
    result = try_delegate("/v1/query", params, db=db)
    if result is None:
        conversations = list_conversations(db_path=db, **asdict(filters))

    fmt = select_format(json_mode=args.json, is_tty=...)
    output = fmt.render_list(conversations, fidelity)
    emit_output(output)

# cli_query.py — after (with dispatch)
def cmd_query(args):
    op = Operation(
        fn=list_conversations,
        params={"db_path": resolve_db(args), **asdict(extract_filter_args(args))},
        render_method="list",
        fidelity=fidelity_from_args(args),
        db=resolve_db(args),
    )

    # Delegation is automatic
    result = try_serve(op) or execute(op)

    fmt = select_format(json_mode=args.json, is_tty=...)
    emit_output(render(result, op, format=fmt))
```

## How Serve Routes Use It

```python
# serve/routes.py — before (current, per-route handler)
@get("/v1/query")
async def query(db_path, workspace, model, ...):
    rows = list_conversations(db_path=db_path, workspace=workspace, ...)
    return serialize_conversation_list(rows)

# serve/routes.py — after (generic dispatch)
@get("/v1/query")
async def query(request, db_path):
    op = operation_from_request("query.list", request, db_path)
    result = execute(op)
    return render(result, op, format=json_format)
```

## How Content Negotiation Works

```python
# Single route, multiple formats
@get("/query")
async def query(request, db_path):
    op = operation_from_request("query.list", request, db_path)
    result = execute(op)
    fmt = format_from_request(request)  # JSON, HTML, or markdown
    return render(result, op, format=fmt)
```

## Open Questions

1. Where does `dispatch.py` live in the layer stack?
   - It imports from api (fn references), serialization (json format), painted (Fidelity)
   - Serve and CLI both import from it
   - Probably needs to be in `api` layer or a new `dispatch` layer

2. How do we handle the param normalization gap?
   - CLI: `extract_filter_args(args)` → FilterArgs
   - HTTP: query params → dict
   - Both need to produce the same kwargs for the API function
   - FilterArgs.to_api_kwargs() or a shared normalizer?

3. Should Operation carry the endpoint path?
   - If yes, try_serve can auto-map without a lookup table
   - If no, we need the endpoint registry for delegation

4. What about operations that don't fit render_*?
   - Stats has its own serializer (_stats_to_dict)
   - Tag writes return action results, not renderable data
   - "raw" render_method = pass through to caller?
