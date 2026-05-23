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

    def to_local(self) -> dict[str, Any]:
        """Return the kwargs to pass to ``self.fn`` for local execution.

        Strips ``OpSpec.local_excludes`` — annotation keys the local fn doesn't
        accept (CLI-only state, routing keys). Ops with no registered spec get
        ``self.params`` unchanged; local-only commands are valid.
        """
        from siftd.api.op_spec import apply_local, spec_for
        return apply_local(self.params, spec_for(self))

    def to_wire(self) -> dict[str, Any]:
        """Return the GET query-params dict for HTTP delegation of this Operation.

        Raises :class:`siftd.api.op_spec.MissingOpSpec` if the op has no spec
        — wire serialization without a spec would silently leak local-only
        keys (e.g. ``db_path``) onto the wire, recreating the bug class this
        substrate dissolves.

        See :mod:`siftd.api.op_spec` and ``docs/guides/delegation-contract.md``.
        """
        from siftd.api.op_spec import MissingOpSpec, apply_wire, spec_for
        spec = spec_for(self)
        if spec is None:
            raise MissingOpSpec(
                f"no OpSpec registered for {self.method} {self.path}"
            )
        return apply_wire(self.params, spec)

    def to_wire_body(self) -> dict[str, Any]:
        """Return the JSON body dict for HTTP POST delegation of this Operation.

        Raises :class:`siftd.api.op_spec.MissingOpSpec` on missing spec, same
        rationale as :meth:`to_wire`.
        """
        from siftd.api.op_spec import MissingOpSpec, apply_wire_body, spec_for
        spec = spec_for(self)
        if spec is None:
            raise MissingOpSpec(
                f"no OpSpec registered for {self.method} {self.path}"
            )
        return apply_wire_body(self.params, spec)


def execute(op: Operation) -> Any:
    """Call the API function with the operation's local kwargs."""
    return op.fn(**op.to_local())


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


