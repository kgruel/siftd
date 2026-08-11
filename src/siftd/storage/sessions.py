"""Live session tracking and pending tag storage.

Supports tagging conversations from within active sessions, with tags
applied at ingest time. Also supports exchange-level tagging.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from siftd.ids import ulid as _ulid

_logger = logging.getLogger(__name__)

# Pending tag entity_type values that the resolver knows about.
_VALID_PENDING_ENTITY_TYPES: frozenset[str] = frozenset({
    "conversation", "exchange", "prompt", "response", "tool_call",
})

# Symbolic markers for "tag the most recent <kind> at ingest time."
# Resolved against the events table when the session is ingested.
_VALID_LAST_MARKERS: frozenset[str] = frozenset({
    "last_prompt", "last_response", "last_exchange", "last_tool_call",
})

# How each late-bound marker resolves: marker → (target_kind, kind-to-fetch).
# last_exchange is anchored on the prompt event, so its target_kind differs
# from the kind queried. Lives here beside _VALID_LAST_MARKERS (the marker
# vocabulary's home); both the ingest drain and the doctor recovery path
# resolve markers through it.
LAST_MARKER_DISPATCH: dict[str, tuple[str, str]] = {
    "last_prompt": ("prompt", "prompt"),
    "last_response": ("response", "response"),
    "last_exchange": ("exchange", "prompt"),
    "last_tool_call": ("tool_call", "tool_call"),
}


@dataclass
class PendingTag:
    """A tag queued for application at ingest time."""

    tag_name: str
    entity_type: str  # 'conversation' | 'exchange' | 'prompt' | 'response' | 'tool_call'
    exchange_index: int | None  # None for conversation, 1-based for exchange
    last_marker: str | None = None  # 'last_prompt' | 'last_response' | 'last_exchange' | 'last_tool_call'


def ensure_session_tables(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Create active_sessions and pending_tags tables. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            harness_session_id TEXT PRIMARY KEY,
            adapter_name TEXT NOT NULL,
            workspace_path TEXT,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
    """)

    # Migration: add last_seen_at column if missing (for existing databases)
    cur = conn.execute("PRAGMA table_info(active_sessions)")
    columns = {row[1] for row in cur.fetchall()}
    if "last_seen_at" not in columns:
        conn.execute("ALTER TABLE active_sessions ADD COLUMN last_seen_at TEXT")
        # Initialize last_seen_at from started_at for existing rows
        conn.execute("UPDATE active_sessions SET last_seen_at = started_at WHERE last_seen_at IS NULL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_tags (
            id TEXT PRIMARY KEY,
            harness_session_id TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'conversation',
            exchange_index INTEGER,
            last_marker TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (harness_session_id, tag_name, entity_type, exchange_index, last_marker)
        )
    """)

    # In-place migration: rebuild legacy pending_tags tables that lack the
    # last_marker column. Schema-additive — existing rows retain their values
    # and apply via the NULL-last_marker path.
    #
    # Only rebuilds if the existing table has the post-v6 column set
    # (entity_type, exchange_index). Older tables are handled by the
    # dedicated migration phases in storage.sqlite.
    cur = conn.execute("PRAGMA table_info(pending_tags)")
    pt_columns = {row[1] for row in cur.fetchall()}
    has_post_v6 = {"entity_type", "exchange_index"}.issubset(pt_columns)
    if has_post_v6 and "last_marker" not in pt_columns:
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM pending_tags",
        ).fetchone()[0]
        _logger.info(
            "Rebuilding pending_tags to add last_marker column (preserving %d row(s))",
            existing_count,
        )
        conn.execute("""
            CREATE TABLE pending_tags_new (
                id TEXT PRIMARY KEY,
                harness_session_id TEXT NOT NULL,
                tag_name TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT 'conversation',
                exchange_index INTEGER,
                last_marker TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (harness_session_id, tag_name, entity_type, exchange_index, last_marker)
            )
        """)
        conn.execute("""
            INSERT INTO pending_tags_new
                (id, harness_session_id, tag_name, entity_type, exchange_index, last_marker, created_at)
            SELECT id, harness_session_id, tag_name, entity_type, exchange_index, NULL, created_at
            FROM pending_tags
        """)
        conn.execute("DROP TABLE pending_tags")
        conn.execute("ALTER TABLE pending_tags_new RENAME TO pending_tags")

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_tags_session
        ON pending_tags(harness_session_id)
    """)

    if commit:
        conn.commit()


def register_session(
    conn: sqlite3.Connection,
    harness_session_id: str,
    adapter_name: str,
    workspace_path: str | None = None,
    *,
    commit: bool = False,
) -> str:
    """Upsert into active_sessions. Returns harness_session_id.

    On insert: sets both started_at and last_seen_at to now.
    On update: refreshes last_seen_at (keeps original started_at).
    """
    now = datetime.now(UTC).isoformat()

    conn.execute(
        """
        INSERT INTO active_sessions (harness_session_id, adapter_name, workspace_path, started_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (harness_session_id) DO UPDATE SET
            adapter_name = excluded.adapter_name,
            workspace_path = excluded.workspace_path,
            last_seen_at = excluded.last_seen_at
        """,
        (harness_session_id, adapter_name, workspace_path, now, now),
    )

    if commit:
        conn.commit()

    return harness_session_id


def unregister_session(
    conn: sqlite3.Connection,
    harness_session_id: str,
    *,
    commit: bool = False,
) -> bool:
    """Delete from active_sessions. Returns True if existed."""
    cur = conn.execute(
        "DELETE FROM active_sessions WHERE harness_session_id = ?",
        (harness_session_id,),
    )

    if commit:
        conn.commit()

    return cur.rowcount > 0


def queue_tag(
    conn: sqlite3.Connection,
    harness_session_id: str,
    tag_name: str,
    *,
    entity_type: str = "conversation",
    exchange_index: int | None = None,
    last_marker: str | None = None,
    commit: bool = False,
) -> str | None:
    """Insert into pending_tags. Returns ULID or None if duplicate.

    last_marker, when set, defers target resolution to ingest time:
    'last_prompt' / 'last_response' / 'last_exchange' / 'last_tool_call'.
    Mutually exclusive with exchange_index — both targeting modes can't apply.
    """
    if entity_type not in _VALID_PENDING_ENTITY_TYPES:
        raise ValueError(
            f"Unknown entity_type {entity_type!r}. "
            f"Valid: {sorted(_VALID_PENDING_ENTITY_TYPES)}",
        )
    if last_marker is not None and last_marker not in _VALID_LAST_MARKERS:
        raise ValueError(
            f"Unknown last_marker {last_marker!r}. "
            f"Valid: {sorted(_VALID_LAST_MARKERS)}",
        )
    if last_marker is not None and exchange_index is not None:
        raise ValueError(
            "queue_tag accepts at most one of exchange_index or last_marker, not both",
        )
    if exchange_index is not None and exchange_index < 1:
        raise ValueError(
            f"exchange_index must be >= 1 (1-based), got {exchange_index}",
        )

    # Check for duplicate explicitly (SQLite UNIQUE doesn't handle NULL correctly)
    cur = conn.execute(
        """
        SELECT 1 FROM pending_tags
        WHERE harness_session_id = ? AND tag_name = ? AND entity_type = ?
          AND (
              (exchange_index IS NULL AND ? IS NULL)
              OR exchange_index = ?
          )
          AND (
              (last_marker IS NULL AND ? IS NULL)
              OR last_marker = ?
          )
        """,
        (
            harness_session_id, tag_name, entity_type,
            exchange_index, exchange_index,
            last_marker, last_marker,
        ),
    )

    if cur.fetchone():
        return None  # Duplicate

    ulid = _ulid()
    now = datetime.now(UTC).isoformat()

    conn.execute(
        """
        INSERT INTO pending_tags
            (id, harness_session_id, tag_name, entity_type, exchange_index, last_marker, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ulid, harness_session_id, tag_name, entity_type, exchange_index, last_marker, now),
    )

    if commit:
        conn.commit()

    return ulid


def rename_pending_tag(
    conn: sqlite3.Connection,
    old_name: str,
    new_name: str,
    *,
    commit: bool = False,
) -> int:
    """Rename queued pending tags, collapsing duplicates onto the new name."""
    if old_name == new_name:
        return 0

    removed_duplicates = conn.execute(
        """
        DELETE FROM pending_tags
        WHERE tag_name = ?
          AND EXISTS (
              SELECT 1
              FROM pending_tags existing
              WHERE existing.harness_session_id = pending_tags.harness_session_id
                AND existing.tag_name = ?
                AND existing.entity_type = pending_tags.entity_type
                AND (
                    existing.exchange_index = pending_tags.exchange_index
                    OR (
                        existing.exchange_index IS NULL
                        AND pending_tags.exchange_index IS NULL
                    )
                )
                AND (
                    existing.last_marker = pending_tags.last_marker
                    OR (
                        existing.last_marker IS NULL
                        AND pending_tags.last_marker IS NULL
                    )
                )
          )
        """,
        (old_name, new_name),
    ).rowcount

    renamed = conn.execute(
        "UPDATE pending_tags SET tag_name = ? WHERE tag_name = ?",
        (new_name, old_name),
    ).rowcount

    if commit:
        conn.commit()

    return removed_duplicates + renamed


def delete_pending_tag(
    conn: sqlite3.Connection,
    tag_name: str,
    *,
    commit: bool = False,
) -> int:
    """Delete queued pending tags for a removed tag name."""
    removed = conn.execute(
        "DELETE FROM pending_tags WHERE tag_name = ?",
        (tag_name,),
    ).rowcount

    if commit:
        conn.commit()

    return removed


def get_pending_tags(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> list[PendingTag]:
    """Return list of pending tags for this session."""
    cur = conn.execute(
        """
        SELECT tag_name, entity_type, exchange_index, last_marker
        FROM pending_tags
        WHERE harness_session_id = ?
        ORDER BY created_at
        """,
        (harness_session_id,),
    )

    return [
        PendingTag(
            tag_name=row["tag_name"],
            entity_type=row["entity_type"],
            exchange_index=row["exchange_index"],
            last_marker=row["last_marker"],
        )
        for row in cur.fetchall()
    ]


def find_active_session(
    conn: sqlite3.Connection,
    workspace_path: str,
) -> str | None:
    """Find most recent active session for a workspace path.

    Fallback for when file-based session ID lookup fails (e.g.,
    spawned agents with different CWD or symlink resolution).
    """
    cur = conn.execute(
        """
        SELECT harness_session_id
        FROM active_sessions
        WHERE workspace_path = ?
        ORDER BY last_seen_at DESC
        LIMIT 1
        """,
        (workspace_path,),
    )
    row = cur.fetchone()
    return row["harness_session_id"] if row else None


def is_session_registered(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> bool:
    """Check if session exists in active_sessions."""
    cur = conn.execute(
        "SELECT 1 FROM active_sessions WHERE harness_session_id = ?",
        (harness_session_id,),
    )
    return cur.fetchone() is not None


def get_session_info(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> dict | None:
    """Get session info from active_sessions. Returns dict or None."""
    cur = conn.execute(
        """
        SELECT harness_session_id, adapter_name, workspace_path, started_at, last_seen_at
        FROM active_sessions
        WHERE harness_session_id = ?
        """,
        (harness_session_id,),
    )
    row = cur.fetchone()
    if row:
        return {
            "harness_session_id": row["harness_session_id"],
            "adapter_name": row["adapter_name"],
            "workspace_path": row["workspace_path"],
            "started_at": row["started_at"],
            "last_seen_at": row["last_seen_at"],
        }
    return None


def prune_stale_sessions(
    conn: sqlite3.Connection,
    max_age_hours: int = 48,
    *,
    commit: bool = False,
) -> int:
    """Delete active_sessions rows not seen for max_age_hours. Returns the count.

    The session's queued tags are left in pending_tags: a registration going
    stale says the harness stopped reporting, not that the intent to tag
    expired. The tags become "orphaned" (no equivalent key registered) and are
    then the recovery path's input.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    cur = conn.execute(
        "DELETE FROM active_sessions WHERE COALESCE(last_seen_at, started_at) < ?",
        (cutoff,),
    )
    if commit:
        conn.commit()
    return cur.rowcount


def _covers_sql(outer: str, inner: str) -> str:
    """SQL fragment for "``outer`` names the same session as ``inner``".

    ``pending_tags`` and ``active_sessions`` are keyed by whatever id the
    harness reported, while adapters are free to namespace their
    ``external_id`` (claude_code writes ``claude_code::<uuid>``). So the match
    is "equal, or equal after an ``<adapter>::`` prefix" — expressed as a
    suffix comparison rather than a LIKE, so a session id containing ``%`` or
    ``_`` can't act as a wildcard.

    The rule has one home and is *composed* by its callers rather than
    retyped: directed, where only one side can carry the prefix
    (:func:`resolve_session_conversation`), or symmetric, where either can
    (:data:`_ORPHANED_PENDING_SCOPE`). :func:`_covered_keys` is the same rule
    in Python; ``tests/test_sessions.py`` pins the two together.

    ``outer`` and ``inner`` are SQL expressions — a column name or a named
    parameter — never user data.
    """
    return (
        f"({outer} = {inner} "
        f"OR substr({outer}, -(length({inner}) + 2)) = '::' || {inner})"
    )


def _covered_keys(key: str) -> list[str]:
    """Every session key ``key`` stands for: itself, then each ``::``-suffix.

    The Python side of :func:`_covers_sql` — ``inner in _covered_keys(outer)``
    is exactly what that fragment tests. Used to invert the match: given a
    conversation's ``external_id``, the queue keys that could name it, so a
    whole key set resolves in one pass (:func:`resolve_session_conversations`).
    """
    forms = [key]
    idx = key.find("::")
    while idx != -1:
        forms.append(key[idx + 2:])
        idx = key.find("::", idx + 2)
    return forms


# A pending row is orphaned only when *no* equivalent key form is registered.
# The shipped session-start hook registers `<adapter>::<uuid>` while
# `siftd tag --session <uuid>` queues the bare uuid, so an exact-key scope
# calls a still-live session's rows orphaned — and recovery then resolves
# their `--last-*` markers against a half-written transcript, pinning the tag
# to a non-final turn. Symmetric, because either side may be the prefixed one.
_ORPHANED_PENDING_SCOPE = f"""
    NOT EXISTS (
        SELECT 1 FROM active_sessions s
        WHERE {_covers_sql("s.harness_session_id", "pending_tags.harness_session_id")}
           OR {_covers_sql("pending_tags.harness_session_id", "s.harness_session_id")}
    )
"""


def resolve_session_conversation(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> str | None:
    """Return the conversation id an ingested session belongs to, or None.

    The session key is matched against ``external_id`` by the rule
    :func:`_covers_sql` documents — directed, since it is the adapter that
    namespaces, so only ``external_id`` can carry the prefix.

    Subagent rows (``...::agent::<id>``) are excluded so a session-level tag
    lands on the parent conversation, matching the ingest-drain semantic.
    When a transcript has been re-ingested under several ids, the newest row
    wins — it is the one a query would return today.

    This is the single-key form, for the ingest drain. Resolving a whole
    queue with it is O(keys x conversations): see
    :func:`resolve_session_conversations`.
    """
    cur = conn.execute(
        f"""
        SELECT id FROM conversations
        WHERE {_covers_sql("external_id", ":sid")}
          AND instr(external_id, '::agent::') = 0
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        {"sid": harness_session_id},
    )
    row = cur.fetchone()
    return row["id"] if row else None


def resolve_session_conversations(
    conn: sqlite3.Connection,
    harness_session_ids: Iterable[str],
) -> dict[str, str]:
    """Set-wise :func:`resolve_session_conversation` — key to conversation id.

    Same rule, one pass. The suffix match is unindexable, so the single-key
    form full-scans ``conversations``; running it per key made both the
    ``pending-tags`` check and its fix O(sessions x conversations), which the
    fast doctor lane cannot afford. Here the match is inverted — each
    conversation is expanded to the keys it covers (:func:`_covered_keys`) and
    intersected with the wanted set — so the whole queue resolves in a single
    scan. Keys that resolve to nothing are absent from the result, so
    ``.get`` is the drop-in for the single-key call.

    The newest row wins per key, matching the single-key form's
    ``ORDER BY started_at DESC, id DESC`` (SQLite sorts NULL timestamps last
    under DESC, so a dated row outranks an undated one).
    """
    wanted = set(harness_session_ids)
    if not wanted:
        return {}

    winner: dict[str, tuple[tuple[int, str, str], str]] = {}
    for row in conn.execute(
        "SELECT id, external_id, started_at FROM conversations "
        "WHERE instr(external_id, '::agent::') = 0"
    ):
        external_id = row["external_id"]
        if external_id is None:
            continue
        keys = wanted.intersection(_covered_keys(external_id))
        if not keys:
            continue
        started_at = row["started_at"]
        rank = (0, "", row["id"]) if started_at is None else (1, started_at, row["id"])
        for key in keys:
            if key not in winner or winner[key][0] < rank:
                winner[key] = (rank, row["id"])

    return {key: conversation_id for key, (_rank, conversation_id) in winner.items()}


@dataclass
class AppliedPendingTag:
    """A queued tag that now holds on a real target; its queue row is consumed."""

    harness_session_id: str
    tag_name: str
    target_kind: str
    target_id: str
    already_present: bool  # the assignment already existed (manual recovery)


# What stops a queued row from being applied. The two need different
# handling, not just different wording: a target-pending row is one ingest
# away from landing, so it is neither something the fix can advertise nor
# something `--discard-unresolved` should sweep up.
UnresolvedKind = Literal["target-pending", "session-unresolvable"]

# The bucket `--discard-unresolved` clears, named once. Every other statement
# of the rule is a second copy waiting to drift out of step with this one.
DISCARDABLE_KIND: UnresolvedKind = "session-unresolvable"


@dataclass
class UnresolvedPendingTag:
    """A queued tag that could not be applied."""

    harness_session_id: str
    tag_name: str
    reason: str
    kind: UnresolvedKind


@dataclass
class PendingTagRecovery:
    """Outcome of :func:`recover_pending_tags`.

    ``unresolved`` and ``discarded`` partition the rows that were not applied:
    a row appears in exactly one, so no reader can describe a deleted row as
    kept. ``discarded`` carries the same detail as ``unresolved`` rather than a
    bare count — a delete is the outcome most worth being able to name after
    the fact.
    """

    applied: list[AppliedPendingTag]
    unresolved: list[UnresolvedPendingTag]
    """Not applied and still queued."""

    discarded: list[UnresolvedPendingTag]
    """Not applied and deleted, under the explicit ``discard_unresolved`` opt-in."""

    stale_sessions_pruned: int

    @property
    def already_present(self) -> int:
        return sum(1 for a in self.applied if a.already_present)


_PENDING_ROW_COLUMNS = (
    "id, harness_session_id, tag_name, entity_type, exchange_index, "
    "last_marker, created_at"
)


def _apply_pending_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    resolve_conversation: Callable[[str], str | None],
) -> tuple[list[AppliedPendingTag], list[tuple[str, UnresolvedPendingTag]], set[str]]:
    """Apply the rows whose target resolves; report the rest.

    Shared by the ingest drain (:func:`drain_pending_tags`, where the
    conversation is already known) and the recovery path
    (:func:`recover_pending_tags`, which looks it up per session id) so the
    two agree on targeting *and* on the rule that only an applied row is
    consumed. Returns (applied, unresolved, consumable row ids) — the caller
    owns the delete, since the two paths differ on what to do with the rest.

    Each unresolved entry is paired with its queue row id, so a caller that
    deletes some of them can partition by the classification already made here
    instead of re-deriving which rows those were.
    """
    from siftd.storage.tags import apply_tag, get_or_create_tag

    applied: list[AppliedPendingTag] = []
    unresolved: list[tuple[str, UnresolvedPendingTag]] = []
    consumed_ids: set[str] = set()

    for row in rows:
        sid = row["harness_session_id"]
        conversation_id = resolve_conversation(sid)

        # A str is the failure reason; a tuple is the resolved target.
        outcome: tuple[str, str] | str
        if conversation_id is None:
            kind: UnresolvedKind = "session-unresolvable"
            outcome = "no ingested conversation matches this session id"
        else:
            kind = "target-pending"
            outcome = _resolve_pending_target(conn, row, conversation_id)

        if isinstance(outcome, str):
            unresolved.append((
                row["id"],
                UnresolvedPendingTag(
                    harness_session_id=sid,
                    tag_name=row["tag_name"],
                    reason=outcome,
                    kind=kind,
                ),
            ))
            continue

        target_kind, target_id = outcome
        tag_id = get_or_create_tag(conn, row["tag_name"])
        assignment = apply_tag(
            conn, target_kind, target_id, tag_id, applied_at=row["created_at"],
        )
        applied.append(
            AppliedPendingTag(
                harness_session_id=sid,
                tag_name=row["tag_name"],
                target_kind=target_kind,
                target_id=target_id,
                already_present=assignment is None,
            )
        )
        consumed_ids.add(row["id"])

    return applied, unresolved, consumed_ids


def drain_pending_tags(
    conn: sqlite3.Connection,
    harness_session_ids: list[str],
    conversation_id: str,
    *,
    commit: bool = False,
) -> tuple[list[AppliedPendingTag], list[UnresolvedPendingTag]]:
    """Apply the tags queued for a session that has just been ingested.

    ``harness_session_ids`` is every key form the session may have been
    queued under (see the ingest drain's ``_session_key_candidates``). *All*
    of them are drained, not just the first with rows: shipped tooling writes
    both forms in a single session — the session-start hook registers
    ``<adapter>::<uuid>`` while ``siftd tag --session <uuid>`` writes the bare
    uuid — and both name the same session.

    Only rows that resolve to a real target are applied and consumed. A row
    whose target does not exist yet (no response in the transcript, exchange
    index past the end) is left queued, so the next ingest — or
    ``siftd doctor fix --pending-tags`` — can still land it. Deleting a
    queued tag is data loss, never a repair.
    """
    if not harness_session_ids:
        return ([], [])

    placeholders = ",".join("?" * len(harness_session_ids))
    rows = conn.execute(
        f"SELECT {_PENDING_ROW_COLUMNS} FROM pending_tags "
        f"WHERE harness_session_id IN ({placeholders}) ORDER BY created_at",
        harness_session_ids,
    ).fetchall()

    applied, unresolved, consumed_ids = _apply_pending_rows(
        conn, rows, lambda _sid: conversation_id,
    )
    _delete_pending_rows(conn, list(consumed_ids))

    if commit:
        conn.commit()

    # The drain deletes nothing it did not apply, so it has no use for the
    # queue row ids riding along with the unresolved entries.
    return applied, [entry for _row_id, entry in unresolved]


def recover_pending_tags(
    conn: sqlite3.Connection,
    *,
    max_age_hours: int = 48,
    discard_unresolved: bool = False,
    commit: bool = False,
) -> PendingTagRecovery:
    """Apply queued tags whose session has already been ingested.

    The queue only drains when a session's transcript is ingested again. A
    settled session never re-ingests (its hash is unchanged), so its rows
    would sit in pending_tags forever — this is the recovery path for them,
    and the only one that exists.

    Scope is the *orphaned* rows: those for which no equivalent session key
    is registered (:data:`_ORPHANED_PENDING_SCOPE`), which is exactly what the
    ``pending-tags`` doctor check counts. Sessions still registered are left
    alone — they may still be live, and their late-bound markers should
    resolve against the finished transcript, not a mid-flight one. Stale
    registrations are pruned first (see :func:`prune_stale_sessions`), which
    brings their rows into scope.

    Rows that resolve are applied and consumed. Rows that don't are reported
    and *kept*, unless ``discard_unresolved`` is set — deleting a queued tag
    is data loss, never a repair, so it takes an explicit opt-in, and even
    then only :data:`DISCARDABLE_KIND` rows go. An assignment that already
    exists (a hand-recovered tag) counts as applied: the intent holds, so the
    row has done its job and is consumed.

    A row that was deleted is reported as ``discarded``, never as
    ``unresolved`` — the two lists partition what was not applied, so no
    reader can present a deleted row as one that was kept.
    """
    stale_pruned = prune_stale_sessions(conn, max_age_hours)

    rows = conn.execute(
        f"""
        SELECT {_PENDING_ROW_COLUMNS}
        FROM pending_tags
        WHERE {_ORPHANED_PENDING_SCOPE}
        ORDER BY created_at
        """,
    ).fetchall()

    # One resolution pass, shared with the check via
    # :func:`count_orphaned_pending_tags` — the buckets it reports and the
    # outcome here are then the same computation, not two that agree by
    # inspection.
    resolved = resolve_session_conversations(
        conn, {row["harness_session_id"] for row in rows},
    )
    applied, unresolved, consumed_ids = _apply_pending_rows(conn, rows, resolved.get)

    _delete_pending_rows(conn, list(consumed_ids))

    # Partition on the classification _apply_pending_rows already made, rather
    # than restating "which rows does the flag clear" as a second predicate
    # over the raw rows. The scope is DISCARDABLE_KIND: a row whose session did
    # resolve is only waiting on a target the transcript does not hold yet, so
    # the next ingest can still land it — sweeping it up here would discard a
    # tag that is still live, which is the opposite of what this flag is for.
    kept: list[UnresolvedPendingTag] = []
    discarded: list[UnresolvedPendingTag] = []
    discard_ids: list[str] = []
    for row_id, entry in unresolved:
        if discard_unresolved and entry.kind == DISCARDABLE_KIND:
            discarded.append(entry)
            discard_ids.append(row_id)
        else:
            kept.append(entry)
    _delete_pending_rows(conn, discard_ids)

    if commit:
        conn.commit()

    return PendingTagRecovery(
        applied=applied,
        unresolved=kept,
        discarded=discarded,
        stale_sessions_pruned=stale_pruned,
    )


def _resolve_pending_target(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    conversation_id: str,
) -> tuple[str, str] | str:
    """Resolve one pending row to (target_kind, target_id), or a failure reason.

    Mirrors the ingest drain's targeting: late-bound marker, then plain
    conversation, then 1-based exchange index. A row whose target doesn't
    exist yet (no response in the transcript, index past the end) resolves to
    a reason, not a target — nothing is applied, so nothing is consumed.
    """
    from siftd.storage.events import get_last_event_id, get_prompt_by_index

    if row["last_marker"]:
        dispatch = LAST_MARKER_DISPATCH.get(row["last_marker"])
        if dispatch is None:
            return f"unknown marker {row['last_marker']!r}"
        target_kind, fetch_kind = dispatch
        event_id = get_last_event_id(conn, conversation_id, fetch_kind)
        if event_id is None:
            return f"conversation has no {fetch_kind} event to carry {row['last_marker']}"
        return (target_kind, event_id)

    if row["entity_type"] == "conversation":
        return ("conversation", conversation_id)

    if row["entity_type"] == "exchange":
        try:
            prompt_id = get_prompt_by_index(conn, conversation_id, row["exchange_index"])
        except ValueError as e:
            return f"invalid exchange index: {e}"
        if prompt_id is None:
            return f"exchange {row['exchange_index']} not found in the conversation"
        return ("exchange", prompt_id)

    return f"unsupported entity_type {row['entity_type']!r}"


def _delete_pending_rows(conn: sqlite3.Connection, ids: list[str]) -> int:
    """Delete pending_tags rows by id. Returns the number deleted."""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"DELETE FROM pending_tags WHERE id IN ({placeholders})", ids,
    )
    return cur.rowcount


@dataclass
class OrphanedPendingCounts:
    """Orphaned pending rows, split by what stops each from being applied."""

    recoverable: int = 0
    """Session and target both resolve — ``doctor fix --pending-tags`` applies these."""

    target_pending: int = 0
    """Session resolves, target does not exist yet; a later ingest may land them."""

    session_unresolvable: int = 0
    """Session matches no ingested conversation; only ``--discard-unresolved`` moves these."""


def count_orphaned_pending_tags(conn: sqlite3.Connection) -> OrphanedPendingCounts:
    """Bucket the pending rows for unregistered sessions by what blocks them.

    Only ``recoverable`` is something the fix can move, and the check may only
    advertise a fix for that bucket: a warning the fix cannot clear leaves
    ``siftd doctor --strict`` (documented for CI) permanently red with no
    non-destructive way out. Two distinct things used to be counted as
    recoverable — a row whose *session* resolves but whose *target* does not
    (an ``--exchange`` past the end, ``--last-tool-call`` before any tool ran)
    is exactly as unfixable today as a typo'd session id, and only differs in
    whether a later ingest might change that.

    Classified per row through the same resolvers the fix runs
    (:func:`resolve_session_conversations`, then
    :func:`_resolve_pending_target`), so the buckets reported here and the
    outcome of :func:`recover_pending_tags` are one computation rather than
    two that have to be kept in agreement.
    """
    rows = conn.execute(
        f"SELECT {_PENDING_ROW_COLUMNS} FROM pending_tags WHERE {_ORPHANED_PENDING_SCOPE}"
    ).fetchall()

    counts = OrphanedPendingCounts()
    if not rows:
        return counts

    resolved = resolve_session_conversations(
        conn, {row["harness_session_id"] for row in rows},
    )
    for row in rows:
        conversation_id = resolved.get(row["harness_session_id"])
        if conversation_id is None:
            counts.session_unresolvable += 1
        elif isinstance(_resolve_pending_target(conn, row, conversation_id), str):
            # A str is the failure reason; a tuple is a resolved target.
            counts.target_pending += 1
        else:
            counts.recoverable += 1
    return counts


def get_stale_sessions_count(
    conn: sqlite3.Connection,
    max_age_hours: int = 48,
) -> int:
    """Count sessions older than max_age_hours.

    Uses last_seen_at (not started_at) to determine staleness.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    cur = conn.execute(
        "SELECT COUNT(*) FROM active_sessions WHERE COALESCE(last_seen_at, started_at) < ?",
        (cutoff,),
    )
    return cur.fetchone()[0]
