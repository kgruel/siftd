"""Caveats — editorial annotations threaded from execute through render.

A Caveat is a `Finding` with a `target`. Producers register against an
`(kind, applies_to)` pair via `@caveat_producer`; dispatch runs them after
execute() and threads their output into the renderer through
`render_context["caveats"]`. This is the substrate for editorial honesty:
the data layer reports values, the caveats layer reports the gaps.

Vocabulary distinction lives in the namespace (`siftd.api.caveats`), not
in a new structural type. `Caveat = Finding`; the alias preserves field
names so renderer code reads naturally.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from siftd.doctor.checks import Finding

if TYPE_CHECKING:
    from siftd.api.dispatch import Operation

# Vocabulary alias — same shape as Finding, distinct namespace.
Caveat = Finding


@dataclass(frozen=True)
class ProducerSpec:
    """Registered caveat producer.

    Attributes:
        kind: Caveat kind name (e.g. "pricing-missing"). Matches Finding.check.
        fn: (op, result) -> list[Finding]. Result type depends on op.
        applies_to: (op) -> bool. Gates execution; the producer's compute is
            paid only when the predicate returns True.
    """

    kind: str
    fn: Callable[[Any, Any], list[Finding]]
    applies_to: Callable[[Any], bool]


_producers: list[ProducerSpec] = []


def caveat_producer(
    kind: str,
    applies_to: Callable[[Any], bool],
) -> Callable[[Callable[..., list[Finding]]], Callable[..., list[Finding]]]:
    """Register a function as a caveat producer for `kind`.

    `applies_to(op)` is the gate — return True to run the producer for this
    Operation. Predicates should be cheap (attribute checks, identity
    comparisons); expensive work belongs inside the producer body.
    """
    def decorator(fn):
        _producers.append(ProducerSpec(kind=kind, fn=fn, applies_to=applies_to))
        return fn
    return decorator


def run_producers(op: Operation, result: Any) -> list[Finding]:
    """Run all registered producers whose `applies_to` accepts the op."""
    findings: list[Finding] = []
    for spec in _producers:
        if spec.applies_to(op):
            findings.extend(spec.fn(op, result))
    return findings


# ---------------------------------------------------------------------------
# Pricing producer (slice 1)
# ---------------------------------------------------------------------------

def _is_list_conversations_at_depth(op) -> bool:
    """Predicate: list_conversations rendered as a list, depth>=3.

    Restricted to `render_method == "list"` (not "detail") — a list-typed
    result is required for iteration. Restricted to depth>=3 because the
    cost column itself only renders at full depth.
    """
    from siftd.api.conversations import list_conversations
    return (
        op.fn is list_conversations
        and op.render_method == "list"
        and op.fidelity.depth >= 3
    )


@caveat_producer(kind="pricing-missing", applies_to=_is_list_conversations_at_depth)
def _pricing_caveats(op, summaries) -> list[Finding]:
    """Per-row caveat: response references a model with no pricing data.

    Cost gating: if every row has a computed cost, short-circuit before
    opening the database. Otherwise one query against the pricing table
    serves the whole result set.
    """
    if not summaries:
        return []
    if not any(s.cost is None for s in summaries):
        return []

    from siftd.api.database import open_database
    from siftd.paths import db_path as default_db_path
    from siftd.storage.sqlite import get_models_without_pricing

    db = op.params.get("db_path") or default_db_path()
    conn = open_database(db, read_only=True)
    try:
        unpriced_rows = get_models_without_pricing(conn)
    finally:
        conn.close()
    if not unpriced_rows:
        return []
    unpriced_models = {r["model_name"] for r in unpriced_rows}

    return [
        Finding(
            check="pricing-missing",
            severity="warning",
            message=f"No pricing data for {s.model}",
            fix_available=False,
            context={"model": s.model},
            target=s.id,
        )
        for s in summaries
        if s.model and s.model in unpriced_models and s.cost is None
    ]
