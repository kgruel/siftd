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
from datetime import UTC
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
# Active sessions producer (B7)
# ---------------------------------------------------------------------------

def _is_list_conversations_with_workspace_filter(op) -> bool:
    """Predicate: list_conversations rendered as list with workspace filter.

    Producer fires only when filtering by workspace — prevents noise from
    running against unscoped results.
    """
    from siftd.api.conversations import list_conversations
    return (
        op.fn is list_conversations
        and op.render_method == "list"
        and op.params.get("workspace") is not None
    )


@caveat_producer(kind="active-sessions", applies_to=_is_list_conversations_with_workspace_filter)
def _active_sessions_caveats(op, summaries, ctx: ProducerContext) -> list[Finding]:
    """Aggregated finding: active sessions not yet ingested in this workspace.

    Active sessions are by definition not yet ingested (still being written).
    Reports count of live sessions for awareness.
    """
    if not summaries:
        return []

    workspace = op.params.get("workspace")
    if not workspace:
        return []

    from siftd.peek import list_active_sessions

    active = list_active_sessions(workspace=workspace)
    if not active:
        return []

    n = len(active)
    return [
        Finding(
            check="active-sessions",
            severity="info",
            message=f"{n} active session{'s' if n != 1 else ''} in this workspace not yet ingested",
            fix_available=True,
            fix_command="siftd ingest",
            context={"count": n, "workspace": workspace},
        )
    ]


# ---------------------------------------------------------------------------
# Workspace identity producer (slice 1)
# ---------------------------------------------------------------------------

def _is_list_conversations_with_workspace(op) -> bool:
    """Predicate: list_conversations rendered as a list, depth>=2.

    Workspace column appears at depth>=2. Restricted to render_method == "list"
    for list-typed iteration.
    """
    from siftd.api.conversations import list_conversations
    return (
        op.fn is list_conversations
        and op.render_method == "list"
        and op.fidelity.depth >= 2
    )


@caveat_producer(kind="workspace-identity", applies_to=_is_list_conversations_with_workspace)
def _workspace_identity_caveats(op, summaries, ctx: ProducerContext) -> list[Finding]:
    """Per-workspace caveat: workspace_id is unresolvable (no entry in workspaces table).

    A conversation with workspace_id = NULL or orphaned to a missing workspace
    cannot be filtered or grouped by workspace. One query identifies unresolvable
    workspace_ids across the whole result set.
    """
    if not summaries:
        return []

    conv_ids = [s.id for s in summaries]

    conn = ctx.db()

    # Find workspace_ids referenced by these conversations
    placeholders = ",".join("?" * len(conv_ids))
    workspace_rows = conn.execute(
        f"""SELECT DISTINCT c.workspace_id FROM conversations c
           WHERE c.id IN ({placeholders}) AND c.workspace_id IS NOT NULL""",
        conv_ids,
    ).fetchall()

    if not workspace_rows:
        return []

    workspace_ids = {r["workspace_id"] for r in workspace_rows}

    # Find which workspace_ids don't exist in the workspaces table
    ws_placeholders = ",".join("?" * len(workspace_ids))
    existing_rows = conn.execute(
        f"""SELECT id FROM workspaces WHERE id IN ({ws_placeholders})""",
        list(workspace_ids),
    ).fetchall()
    existing_ids = {r["id"] for r in existing_rows}

    unresolvable_ids = workspace_ids - existing_ids

    return [
        Finding(
            check="workspace-identity",
            severity="info",
            message=f"Workspace {wid[:8]} has no entry in workspaces table — workspace filter may not resolve correctly",
            fix_available=False,
            context={"workspace_id": wid},
        )
        for wid in sorted(unresolvable_ids)
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


# ---------------------------------------------------------------------------
# Embeddings staleness producer (B2)
# ---------------------------------------------------------------------------

def _is_search_chunks_for_search_render(op) -> bool:
    """Predicate: search_chunks rendered as search.

    Embeddings staleness matters most when the user is searching.
    """
    from siftd.api.search import search_chunks
    return (
        op.fn is search_chunks
        and op.render_method == "search"
    )


@caveat_producer(kind="embeddings-stale", applies_to=_is_search_chunks_for_search_render)
def _embeddings_stale_caveats(op, result, ctx: ProducerContext) -> list[Finding]:
    """Caveat: conversations in main db not indexed in embeddings db.

    When embeddings are not available or the embeddings db is missing,
    search results may be incomplete. Alerts users to index conversations.
    """
    from siftd.embeddings.availability import embeddings_available
    if not embeddings_available():
        return []

    from siftd.paths import embeddings_db_path
    embed_path = op.params.get("embed_db_path") or embeddings_db_path()
    if not Path(ctx.db_path).exists() or not Path(embed_path).exists():
        return []

    from siftd.api.database import open_database
    embed_conn = open_database(Path(embed_path), read_only=True)
    try:
        from siftd.storage.embeddings import get_indexed_conversation_ids
        indexed_ids = get_indexed_conversation_ids(embed_conn)
    finally:
        embed_conn.close()

    # Compare against conversations in main db
    conn = ctx.db()
    main_ids = {row[0] for row in conn.execute(
        "SELECT DISTINCT conversation_id FROM events WHERE kind = 'prompt'"
    ).fetchall()}

    missing = main_ids - indexed_ids
    if not missing:
        return []

    n = len(missing)
    return [Finding(
        check="embeddings-stale",
        severity="warning",
        message=f"{n} conversation{'s' if n != 1 else ''} not indexed — search results may be incomplete",
        fix_available=True,
        fix_command="siftd search --index",
        context={"count": n},
    )]


# ---------------------------------------------------------------------------
# Pending tags producer (B3)
# ---------------------------------------------------------------------------

def _is_list_conversations_simple(op) -> bool:
    """Predicate: list_conversations rendered as a list."""
    from siftd.api.conversations import list_conversations
    return (
        op.fn is list_conversations
        and op.render_method == "list"
    )


@caveat_producer(kind="pending-tags", applies_to=_is_list_conversations_simple)
def _pending_tags_caveats(op, result, ctx: ProducerContext) -> list[Finding]:
    """Caveat: pending tag intents not yet applied.

    Checks pending_tags table and reports count of queued tagging actions
    awaiting the next ingest.
    """
    if not Path(ctx.db_path).exists():
        return []

    count = ctx.db().execute("SELECT COUNT(*) FROM pending_tags").fetchone()[0]
    if count == 0:
        return []

    return [
        Finding(
            check="pending-tags",
            severity="info",
            message=f"{count} pending tag intent{'s' if count != 1 else ''} — run 'siftd ingest' to apply",
            fix_available=True,
            fix_command="siftd ingest",
            context={"count": count},
        )
    ]


# ---------------------------------------------------------------------------
# Ingest-status producer (B4)
# ---------------------------------------------------------------------------

def _is_list_conversations_list_render_for_ingest(op) -> bool:
    """Predicate: list_conversations rendered as a list.

    Ingest-status findings are relevant at any depth and filter state.
    """
    from siftd.api.conversations import list_conversations
    return (
        op.fn is list_conversations
        and op.render_method == "list"
    )


@caveat_producer(kind="ingest-status", applies_to=_is_list_conversations_list_render_for_ingest)
def _ingest_status_caveats(op, result, ctx: ProducerContext) -> list[Finding]:
    """Caveats for ingest state: errors in ingested_files or stale last-ingest time.

    Two findings emitted if applicable:
    - ingest-errors: files failed ingestion (warning severity)
    - ingest-never-run: no ingest recorded (info severity)
    - ingest-stale: last ingest > 7 days ago (info severity)
    """
    if not Path(ctx.db_path).exists():
        return []

    conn = ctx.db()
    findings = []

    # Part 1: ingestion errors
    from siftd.storage.sqlite import get_ingest_errors
    errors = get_ingest_errors(conn)
    if errors:
        n = len(errors)
        findings.append(Finding(
            check="ingest-errors",
            severity="warning",
            message=f"{n} file{'s' if n != 1 else ''} failed ingestion — run 'siftd doctor' for details",
            fix_available=True,
            fix_command="siftd doctor",
            context={"count": n},
        ))

    # Part 2: stale last-ingest (>7 days or never)
    from siftd.storage.queries import fetch_last_ingest_time
    last = fetch_last_ingest_time(conn)
    if last is None:
        findings.append(Finding(
            check="ingest-never-run",
            severity="info",
            message="No ingest recorded — run 'siftd ingest' to populate the database",
            fix_available=True,
            fix_command="siftd ingest",
        ))
    else:
        from datetime import datetime
        last_dt = datetime.fromisoformat(last).replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - last_dt).days
        if age_days > 7:
            findings.append(Finding(
                check="ingest-stale",
                severity="info",
                message=f"Last ingest was {age_days} days ago — run 'siftd ingest' to catch up",
                fix_available=True,
                fix_command="siftd ingest",
                context={"age_days": age_days},
            ))

    return findings


# ---------------------------------------------------------------------------
# FTS stale producer (B5)
# ---------------------------------------------------------------------------

def _is_search_chunks(op) -> bool:
    """Predicate: search_chunks operation (FTS keyword search in use)."""
    from siftd.api.search import search_chunks
    return op.fn is search_chunks


@caveat_producer(kind="fts-stale", applies_to=_is_search_chunks)
def _fts_stale_caveats(op, result, ctx: ProducerContext) -> list[Finding]:
    """Caveat: FTS index out of sync with event_content.

    Missing: content blocks not yet indexed.
    Orphaned: FTS entries pointing to deleted content blocks.
    """
    if not Path(ctx.db_path).exists():
        return []

    from siftd.storage.fts import get_fts_sync_status
    status = get_fts_sync_status(ctx.db())
    missing = status["missing_count"]
    orphaned = status["orphaned_count"]

    if missing == 0 and orphaned == 0:
        return []

    parts = []
    if missing:
        parts.append(f"{missing} content block{'s' if missing != 1 else ''} not indexed")
    if orphaned:
        parts.append(f"{orphaned} orphaned FTS entr{'ies' if orphaned != 1 else 'y'}")

    return [Finding(
        check="fts-stale",
        severity="warning",
        message=f"FTS index out of sync: {', '.join(parts)} — run 'siftd db vacuum' to rebuild",
        fix_available=True,
        fix_command="siftd db vacuum",
        context={"missing_count": missing, "orphaned_count": orphaned},
    )]
