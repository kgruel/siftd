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

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from siftd.doctor.checks import Finding

if TYPE_CHECKING:
    from siftd.api.dispatch import Operation

# Vocabulary alias — same shape as Finding, distinct namespace.
Caveat = Finding


@dataclass
class ProducerContext:
    """Shared state threaded through all producers for a single dispatch call.

    Holds a lazily-opened read-only SQLite connection so 10+ producers
    don't each open their own connection on the same call.
    """

    db_path: str | Path
    _conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)

    def db(self) -> sqlite3.Connection:
        if self._conn is None:
            from siftd.api.database import open_database
            self._conn = open_database(Path(self.db_path), read_only=True)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


@dataclass(frozen=True)
class ProducerSpec:
    """Registered caveat producer.

    Attributes:
        kind: Caveat kind name (e.g. "pricing-missing"). Matches Finding.check.
        fn: (op, result, ctx) -> list[Finding]. Result type depends on op.
        applies_to: (op) -> bool. Gates execution; the producer's compute is
            paid only when the predicate returns True.
    """

    kind: str
    fn: Callable[[Any, Any, ProducerContext], list[Finding]]
    applies_to: Callable[[Any], bool]


_producers: list[ProducerSpec] = []


def caveat_producer(
    kind: str,
    applies_to: Callable[[Any], bool],
) -> Callable[
    [Callable[[Any, Any, ProducerContext], list[Finding]]],
    Callable[[Any, Any, ProducerContext], list[Finding]],
]:
    """Register a function as a caveat producer for `kind`.

    `applies_to(op)` is the gate — return True to run the producer for this
    Operation. Predicates should be cheap (attribute checks, identity
    comparisons); expensive work belongs inside the producer body.
    """
    def decorator(fn):
        _producers.append(ProducerSpec(kind=kind, fn=fn, applies_to=applies_to))
        return fn
    return decorator


# Cap constants
_MAX_HINTS = 1
_MAX_INFOS = 3


def run_producers(op: Operation, result: Any, ctx: ProducerContext) -> list[Finding]:
    """Run all registered producers whose `applies_to` accepts the op.

    After collecting, applies cap policy:
    - unknown severities: uncapped pass-through (kept for forward compatibility)
    - errors: uncapped
    - warnings: uncapped
    - infos: capped at 3; overflow emits a single "findings-truncated" info
    - hints: capped at 1
    Assembly order: unknown → errors → warnings → infos (capped + overflow) → hints
    """
    raw: list[Finding] = []
    for spec in _producers:
        if spec.applies_to(op):
            raw.extend(spec.fn(op, result, ctx))

    errors = [f for f in raw if f.severity == "error"]
    warnings = [f for f in raw if f.severity == "warning"]
    infos = [f for f in raw if f.severity == "info"]
    hints = [f for f in raw if f.severity == "hint"]
    others = [
        f
        for f in raw
        if f.severity not in {"error", "warning", "info", "hint"}
    ]

    capped_hints = hints[:_MAX_HINTS]

    overflow: list[Finding] = []
    if len(infos) > _MAX_INFOS:
        n = len(infos) - _MAX_INFOS
        overflow = [Finding(
            check="findings-truncated",
            severity="info",
            message=f"+{n} more info finding{'s' if n != 1 else ''} (use --verbose to show all)",
            fix_available=False,
        )]
    capped_infos = infos[:_MAX_INFOS]

    return others + errors + warnings + capped_infos + overflow + capped_hints


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
def _pricing_caveats(op, summaries, ctx: ProducerContext) -> list[Finding]:
    """Per-row caveat: response references a model with no pricing data.

    Cost gating: if every row has a computed cost, short-circuit before
    touching the database. Otherwise one query against the pricing table
    serves the whole result set.
    """
    if not summaries:
        return []
    if not any(s.cost is None for s in summaries):
        return []

    from siftd.storage.sqlite import get_models_without_pricing

    unpriced_rows = get_models_without_pricing(ctx.db())
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


# ---------------------------------------------------------------------------
# Fresh-corpus producer (slice 1)
# ---------------------------------------------------------------------------

def _is_list_conversations_list_render(op) -> bool:
    """Predicate: list_conversations rendered as a list.

    Useful at any depth — warns when the result is a thin slice of
    the corpus.
    """
    from siftd.api.conversations import list_conversations
    return (
        op.fn is list_conversations
        and op.render_method == "list"
    )


@caveat_producer(kind="fresh-corpus", applies_to=_is_list_conversations_list_render)
def _fresh_corpus_caveats(op, result, ctx: ProducerContext) -> list[Finding]:
    """Caveat: result is a thin slice of the corpus.

    Emitted when result < 10 items and the total corpus is also < 10.
    If result >= 10, short-circuit to avoid the DB call.
    """
    if len(result) >= 10:
        return []

    if not Path(ctx.db_path).exists():
        return []

    cursor = ctx.db().cursor()
    total = cursor.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    if total >= 10:
        return []

    return [
        Finding(
            check="fresh-corpus",
            severity="info",
            message=f"Corpus contains {total} conversation{'s' if total != 1 else ''} — results reflect a narrow slice",
            fix_available=False,
            context={"total": total},
        )
    ]
