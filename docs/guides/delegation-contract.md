# Delegation contract: operation has a local form and a wire form

Every CLI command in siftd that *can* be delegated to a remote `siftd-serve` exists in two forms, request and response:

- **Local form**: `op.params` dict → `op.fn(**local_kwargs(op))` → typed result. Runs in the CLI process against a local SQLite.
- **Wire form**: `wire_query(op)` → querystring → Litestar route handler → `op.fn` → serialized dict → `from_wire(op, body)` → typed result. Runs in the server process against the server's SQLite.

Both forms must produce the same typed result for the same logical request — so downstream renderers don't need to know which path produced their input. This document is the contract new code must follow.

## The named substrate

Four entry points name the local/wire seam. They live as close as practical to the code they pair with:

### Request side

- **`siftd.api.dispatch.local_kwargs(op) -> dict`** — kwargs for `op.fn(**...)` local execution. Strips annotation keys the local fn doesn't accept (`_LOCAL_FN_EXCLUDE` — `around`, `debug_ids`, `action`, `embeddings_only`). Lives next to `execute(op)`.
- **`siftd.serve.delegation.wire_query(op) -> dict`** — query params dict for GET delegation. Drops local-only keys (`db_path`), drops `None` values (urlencode would emit them as literal `"None"`), expands non-scalar types (`Fidelity` → `include_thinking` + `include_tool_content`), applies Python-keyword renames (`lambda_` → `lambda`). Lives next to `try_serve(op)`.
- **`siftd.serve.delegation.wire_body(op) -> dict`** — JSON body dict for POST delegation. Drops local-only keys; preserves `None` (JSON `null` is a legitimate body value).

### Response side

- **`siftd.api.dispatch.from_wire(op, body) -> Any`** — reconstructs the typed result from an HTTP delegation response body. Picks the right deserializer based on `op.render_method`. Lives next to `execute(op)`.

The per-render-method deserializers are in `siftd.api.deserialize`:

| `render_method` | Deserializer | Returns |
|---|---|---|
| `list` | `deserialize_conversation_list` | `list[ConversationSummary]` |
| `detail` | `deserialize_conversation_detail` | `ConversationDetail` |
| `export-artifact` | `deserialize_export_artifact` | `ExportArtifact` |
| anything else | (passthrough) | the raw dict |

For passthrough cases (`search`, `stats`, `tags`, `raw`), the CLI consumes the dict directly — no typed reconstruction is needed because the renderer for those modes was already dict-shaped.

## How the two forms connect at the CLI

The pattern for a delegated read in a CLI command:

```python
result = None
delegated = try_serve(op)
if delegated is not None and isinstance(delegated, dict):
    # Deserializers return None on schema mismatch (e.g. older/newer server
    # producing an unexpected body shape) rather than raising — the local
    # fallback below handles that uniformly. Callers MUST NOT wrap this in
    # a try/except for shape errors; the deserializer is the validation
    # boundary.
    result = from_wire(op, delegated)

if result is None:
    result = execute(op)  # local fallback

# Render once, regardless of which path produced result.
emit_output(fmt.render_detail(result.turns, op.fidelity, ...))
```

`try_serve` returns `None` when delegation isn't configured, the server isn't reachable, or the HTTP response is malformed at the transport layer. `from_wire` returns `None` when the response body's *shape* doesn't match what the render method expects (older/newer server, error body, unexpected structure). Both signals route to the same local-execute fallback, so the CLI doesn't need to distinguish them.

## The contract

When adding a new delegated route, or extending an existing route's accepted params:

1. **Declare every CLI param on the route.** For every key the CLI op.params dict carries (other than `_LOCAL_FN_EXCLUDE` annotations and `_WIRE_EXCLUDE` local-only keys like `db_path`/`embed_db`/`mode`/`around`), the route must have a corresponding `Parameter(query="...")` declaration. Litestar silently drops unknown query params; this is the bug class to defend against. (Pinned by `tests/test_op_route_parity.py`, which introspects Litestar route signatures and asserts each Op's `wire_query()` is a subset of the route's declared Parameters. `tests/test_serve_e2e_smoke.py` exercises the wire path end-to-end via TestClient as a complementary check.)

2. **Handle non-scalars in `_expand_for_wire`.** If the CLI op carries a value that isn't `str`/`int`/`float`/`bool`/`list[str]`/`None`, add an explicit translation rule in `serve/delegation.py:_expand_for_wire`. Don't rely on `urlencode`'s `str()` coercion — the result will be opaque garbage the route can't parse.

3. **Strip None values.** Already handled by `_expand_for_wire`. Don't add per-call workarounds; rely on the centralized rule.

4. **Coerce wire types in the route.** Litestar coerces simple types (str, int, bool) from query strings automatically. For union types like `int | str` (e.g. `anchor_value`, which is int for `at_turn` and str for `around`), the route receives a string and must coerce explicitly — see `routes.py:conversation_detail` for the pattern (try `int()`, return 400 on failure).

5. **Add an e2e smoke test.** In `tests/test_serve_e2e_smoke.py`, add a test that exercises the new wire path through Litestar's `TestClient`. This catches what unit tests via `.fn()` cannot:
   - URL encoding behavior (booleans → `"True"` / `"False"` strings)
   - Litestar query parsing + default-value resolution
   - Status code mapping for user-input errors
   - Auth middleware integration

   Patterns to copy: `TestAnchorWindowOverHttp` (parity), `TestDelegationWireRoundTrip` (`_remap_params` → URL → route round-trip), `TestFullDelegationLoop` (route → from_wire → typed object).

6. **Map user-input errors to 4xx, not 5xx.** Custom exceptions that represent bad user input must inherit `ValueError` OR be added to the explicit catch list in `routes.py:_dispatch`. Today `AnchorError` is caught explicitly. New input-validation exceptions need the same treatment.

7. **Register a deserializer if the response is a typed object.** If the route returns more than a plain dict (e.g. a dataclass like `ConversationDetail`), add a `deserialize_X` function in `api/deserialize.py` and wire it into the `from_wire(render_method, body)` dispatch table. The deserializer should be the inverse of the matching serializer in `siftd/serialization/`. Round-trip tests in `tests/test_wire_form_roundtrip.py` pin the contract.

8. **Deserializers return `None` on schema mismatch — never raise.** The CLI fallback path treats `None` from `from_wire` as "fall back to local execute." A deserializer that raises (KeyError, AttributeError, TypeError, etc.) would crash the user with an opaque error instead. Always:
   - validate the body is a `dict` before subscripting,
   - validate required fields exist before constructing the dataclass,
   - return `None` on any mismatch,
   - and for list-typed responses, distinguish "malformed → `None`" from "legitimately empty → `[]`" so the CLI can tell the difference between a fallback signal and a valid empty result.

   This is pinned by `tests/test_wire_form_roundtrip.py::TestMalformedInputReturnsNone`.

## Architectural rule for deserializers

Deserializers live under `api/`, not `serialization/`, because they construct api-layer dataclasses at runtime. The one-way `api → serialization` dependency in `tests/architecture/test_imports.py` enforces this: serializers can take api types as parameters via `TYPE_CHECKING` imports (they only need the structural shape), but deserializers need the constructors at runtime.

## Known lossy round-trips

Strict structural equality across a `local → serialize → deserialize → render` cycle is not always possible. Document any lossy fields at the top of the deserializer module. As of this writing:

- **`ConversationDetail.total_input_tokens` + `total_output_tokens`**: the serialized form carries a single `total_tokens` sum at the conversation level. The deserializer reconstructs the splits by summing per-turn data (which IS preserved). When no turns are present, the split defaults to `(total_tokens, 0)`.

- **`Turn.narrative` with expanded tool calls**: when `include_tool_content=True`, the server emits one `{"type": "tool_call", ...}` block per call; the local fetch produces one `tool_calls` `NarrativeBlock` per response with all calls inside. The deserializer's `_coalesce_adjacent_tool_calls` merges adjacent same-event_id blocks on receive to restore the local-form shape.

When extending the wire form, prefer round-trip preservation over compact serialization. Lossy fields above are tolerated because their information is recoverable from per-turn data; new lossy fields should generally be added to the serializer instead.

## Open questions

- **Tighter `Fidelity` detection?** `_expand_for_wire` duck-types via `hasattr(v, "shows")`. An `isinstance(v, Fidelity)` check would be more precise; today's duck-typing is adequate because nothing else has a `.shows()` method, but the contract would be safer as the codebase grows.

- **Should `_LOCAL_FN_EXCLUDE` move to a per-op declaration?** Today it's a module-level frozenset. A future refactor could let each Operation subclass declare its own exclusions, which would scale better if the set of annotation keys grows. The cost is more ceremony for ops that don't need any exclusion (the common case).

These are explicitly open. The contract above is the minimum discipline new code must follow.
