"""Operation dispatch — the IR between input contexts and output formats.

An Operation is the normalized intent: which API function to call, with what
params, at what fidelity, rendered how. Input contexts (CLI, HTTP, programmatic)
each normalize into an Operation. The dispatch loop executes and renders it.

    normalize(input) → Operation → execute → render(format, fidelity)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from painted import Fidelity


@dataclass(frozen=True)
class Operation:
    """Normalized intent — the IR between input and output contexts.

    Attributes:
        path: API endpoint path (e.g. "/api/v1/conversations"). Used for serve delegation.
        method: HTTP method — "GET" for reads, "POST" for writes.
        fn: The API function to call (e.g. list_conversations).
        params: kwargs for fn (filters, limits, db_path, etc.).
        render_method: Format protocol method name — "list", "detail",
            "search", "stats", or "raw" (passthrough).
        fidelity: Rendering depth/visibility controls.
        db: Resolved database path (for serve delegation db_path matching).
        render_context: Extra kwargs forwarded to the format renderer
            (e.g. detail_base, shell_base, query). Input-context concern —
            the caller knows what its link bases and UI controls are.
    """

    path: str
    method: str
    fn: Callable
    params: dict[str, Any]
    render_method: str
    fidelity: Fidelity
    db: Path
    render_context: dict[str, Any] = field(default_factory=dict)


# Annotation keys that travel in op.params but are NOT accepted by the local
# fn. The wire form may still carry them (the server can declare a Parameter
# to read them if it wants). See docs/guides/delegation-contract.md for the
# pattern this implements.
#
# - debug_ids: serve route passes it to render_context; CLI local path uses
#   op.render_context directly.
# - around: CLI post-processing annotation; caveat producers read it from
#   op.params; the local search fn doesn't accept it.
# - action / embeddings_only: routing keys that pick which wire endpoint or
#   wire shape; not local-fn kwargs.
_LOCAL_FN_EXCLUDE = frozenset({"action", "embeddings_only", "debug_ids", "around"})

# Deprecated alias — kept for any external readers; new code uses _LOCAL_FN_EXCLUDE.
_SERVE_ONLY_KEYS = _LOCAL_FN_EXCLUDE


def local_kwargs(op: Operation) -> dict[str, Any]:
    """Return the kwargs to pass to ``op.fn`` for local execution.

    The local form is ``op.params`` minus annotation keys that the fn
    doesn't accept. This is one half of the operation-local-form +
    operation-wire-form pair; see :func:`siftd.serve.delegation.wire_query`
    for the other half and ``docs/guides/delegation-contract.md`` for the
    contract.
    """
    return {k: v for k, v in op.params.items() if k not in _LOCAL_FN_EXCLUDE}


def execute(op: Operation) -> Any:
    """Call the API function with the operation's local kwargs."""
    return op.fn(**local_kwargs(op))


def from_wire(op: Operation, body: dict[str, Any]) -> Any:
    """Reconstruct a typed result from an HTTP delegation response body.

    The serve route returns a JSON-safe dict; this function maps it back to
    the typed object that ``op.fn`` would have returned for local execution,
    so downstream renderers don't need to know which path produced their
    input.

    Returns the body unchanged for render methods that don't have a typed
    deserializer registered (search, stats, tags, raw) — the caller works
    with the raw dict in those cases. See
    :mod:`siftd.api.deserialize` for the per-render-method
    deserializers.
    """
    from siftd.api.deserialize import from_wire as _from_wire
    return _from_wire(op.render_method, body)


def render(
    result: Any,
    op: Operation,
    *,
    fmt: Any,
    findings: list[Any] | None = None,
) -> Any:
    """Render result through the format protocol.

    For render_method="raw", returns the result unchanged.
    Otherwise calls fmt.render_{method}(result, fidelity, **render_context).

    `findings`, when non-empty, is threaded into render_context as
    "caveats" — producer output is canonical, so an existing
    render_context["caveats"] entry is overwritten.
    """
    if op.render_method == "raw":
        return result

    renderer = getattr(fmt, f"render_{op.render_method}", None)
    if renderer is None:
        return result

    ctx = dict(op.render_context)
    if findings:
        ctx["caveats"] = findings
    return renderer(result, op.fidelity, **ctx)


def execute_for_render(op: Operation) -> tuple[Any, list[Any]]:
    """Execute and run caveat producers, returning both.

    For CLI paths that bypass `dispatch.dispatch()` (e.g. cli/query.py
    has serve-fallback branching) but still want to surface caveats.
    """
    from siftd.api.caveats import ProducerContext, run_producers
    from siftd.paths import db_path as default_db_path

    result = execute(op)
    ctx = ProducerContext(db_path=op.params.get("db_path") or default_db_path())
    try:
        findings = run_producers(op, result, ctx)
    finally:
        ctx.close()
    return result, findings


def dispatch(op: Operation, *, fmt: Any) -> Any:
    """Execute + render in one call.

    Does not attempt serve delegation — the caller handles that via
    serve.delegation.try_serve(op) before calling dispatch. Caveat
    producers run between execute and render; their findings are threaded
    into render_context["caveats"] for the renderer to consume.
    """
    from siftd.api.caveats import ProducerContext, run_producers
    from siftd.paths import db_path as default_db_path

    result = execute(op)
    ctx = ProducerContext(db_path=op.params.get("db_path") or default_db_path())
    try:
        findings = run_producers(op, result, ctx)
    finally:
        ctx.close()
    return render(result, op, fmt=fmt, findings=findings)


