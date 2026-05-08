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


# Keys that are serve routing or render-only, not fn kwargs.
# Filtered by execute() so the params dict can serve both contexts.
# debug_ids: serve route passes it to render_context; CLI local path uses op.render_context directly.
_SERVE_ONLY_KEYS = frozenset({"action", "embeddings_only", "debug_ids"})


def execute(op: Operation) -> Any:
    """Call the API function with params.

    Strips serve-only routing keys (e.g. ``action``) that the fn
    doesn't accept.  These keys are used by try_serve for HTTP
    dispatch but aren't API function kwargs.
    """
    params = {k: v for k, v in op.params.items() if k not in _SERVE_ONLY_KEYS}
    return op.fn(**params)


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
    from siftd.api.caveats import run_producers

    result = execute(op)
    return result, run_producers(op, result)


def dispatch(op: Operation, *, fmt: Any) -> Any:
    """Execute + render in one call.

    Does not attempt serve delegation — the caller handles that via
    serve.delegation.try_serve(op) before calling dispatch. Caveat
    producers run between execute and render; their findings are threaded
    into render_context["caveats"] for the renderer to consume.
    """
    from siftd.api.caveats import run_producers

    result = execute(op)
    findings = run_producers(op, result)
    return render(result, op, fmt=fmt, findings=findings)


