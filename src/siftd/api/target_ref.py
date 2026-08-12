"""TargetRef — one grammar for "which thing gets the tag".

Five surface grammars express the same meaning: colon-path (``<conv>:<kind>:<n>``),
positional kind-word (``response <id>``), session markers (``last_response``),
the serve JSON body (``entity_type``/``entity_id``/``last``), and the HTML form
fields. This module is the single parse → resolve → alias layer they all fold
into. It absorbs the former ``api/granular_targets.py``.

Canonical addressing: the event ULID is the canonical address. Colon-path and
``last_*`` markers are input sugar; resolution happens here at input time, and
storage/wire formats always carry the ULID.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from siftd.api.conversations import AmbiguousPrefix, prefix_candidates, resolve_entity_id
from siftd.storage.filters import ALL_TAG_KINDS, EVENT_TAG_KINDS

# Colon-path *anchor* kinds — the 2nd segment always names an event kind.
# 'block' is addressable (bare/kind-narrowed id, or the optional 4th colon
# segment descending into the anchored event) but never an anchor itself.
GRANULAR_KINDS = EVENT_TAG_KINDS
_ADDRESSABLE_KINDS = ALL_TAG_KINDS
LAST_MARKERS = frozenset({"last_prompt", "last_response", "last_exchange", "last_tool_call"})

# Maps a granular target kind to the events.kind value used to query. 'exchange'
# anchors on the n-th prompt event.
_KIND_TO_EVENT_KIND: dict[str, str] = {
    "prompt": "prompt",
    "response": "response",
    "tool_call": "tool_call",
    "exchange": "prompt",
}


@dataclass(frozen=True)
class ResolvedTarget:
    """A fully-resolved tag target: canonical kind + ULID."""

    target_kind: str
    target_id: str


@dataclass(frozen=True)
class TargetRef:
    """A reference to a taggable target, in any surface grammar, pre-resolution.

    Exactly one addressing mode is populated:
      - ``raw_id`` (+ optional ``kind``): bare ULID or prefix.
      - ``conv_ref`` + ``kind`` + ``position`` (+ optional ``block_position``):
        colon-path. The optional 4th segment descends into a content block.
      - ``last_marker`` or ``exchange_index``: session-relative markers (deferred).
    """

    raw_id: str | None = None
    kind: str | None = None
    conv_ref: str | None = None
    position: int | None = None
    block_position: int | None = None
    last_marker: str | None = None
    exchange_index: int | None = None

    # -- parse: surface grammar → TargetRef --------------------------------

    @classmethod
    def from_colon_path(cls, raw: str) -> TargetRef | None:
        """Parse ``<conv_ref>:<kind>:<n>[:<b>]`` into a colon-path TargetRef.

        The optional 4th segment ``b`` (1-based) descends into content block ``b``
        of the addressed event — the block grammar (WS8). Returns None if the
        string does not match the colon-path pattern (two or three colons, the
        positional segments positive integers). Kind is not validated here —
        ``resolve`` raises on an unknown kind.
        """
        parts = raw.split(":")
        if len(parts) not in (3, 4):
            return None
        conv_ref, kind, n_str = parts[0], parts[1], parts[2]
        if not conv_ref or not kind:
            return None
        try:
            n = int(n_str)
        except ValueError:
            return None
        if n < 1:
            return None
        block_pos: int | None = None
        if len(parts) == 4:
            try:
                block_pos = int(parts[3])
            except ValueError:
                return None
            if block_pos < 1:
                return None
        return cls(conv_ref=conv_ref, kind=kind, position=n, block_position=block_pos)

    @classmethod
    def from_positional(cls, positional: list[str]) -> tuple[TargetRef, list[str]] | None:
        """Parse ``tag`` positional args into (TargetRef, tag_names).

        Supports:
          - ``<id> <tag> [tag2 ...]``            → bare id (kind unknown), tags
          - ``<kind> <id> <tag> [tag2 ...]``     → kind-narrowed id, tags

        A leading colon-path is *not* handled here (use ``from_colon_path``).
        Returns None if the shape is invalid (too few args).
        """
        if len(positional) < 2:
            return None
        if positional[0] in _ADDRESSABLE_KINDS:
            if len(positional) < 3:
                return None
            return cls(raw_id=positional[1], kind=positional[0]), positional[2:]
        return cls(raw_id=positional[0]), positional[1:]

    @classmethod
    def from_wire(cls, body: dict) -> TargetRef:
        """Parse a serve JSON tag body's target into a TargetRef.

        Recognizes ``entity_type``/``entity_id`` (the addressable target). The
        wire ``last`` key means "N most recent conversations" — a *selection*
        count, not a target address — so it is intentionally NOT handled here;
        that path is served by ``apply_tags(last=...)``, and session markers use
        ``from_markers``. Folding ``last`` into ``exchange_index`` would be a
        semantic pun (wire-last ≠ session-exchange), so it is omitted.
        """
        return cls(raw_id=body.get("entity_id"), kind=body.get("entity_type"))

    @classmethod
    def from_markers(
        cls,
        *,
        last_marker: str | None = None,
        exchange_index: int | None = None,
    ) -> TargetRef:
        """Build a session-marker TargetRef (deferred; resolves at ingest)."""
        if last_marker is not None and last_marker not in LAST_MARKERS:
            valid = ", ".join(sorted(LAST_MARKERS))
            raise ValueError(f"Invalid last marker {last_marker!r}. Valid: {valid}")
        return cls(last_marker=last_marker, exchange_index=exchange_index)

    @property
    def is_deferred(self) -> bool:
        """True for session markers that resolve at ingest, not here."""
        return self.last_marker is not None or self.exchange_index is not None


def resolve(
    conn: sqlite3.Connection,
    ref: TargetRef,
    *,
    owner: str | None = None,
) -> ResolvedTarget:
    """Resolve a TargetRef to (target_kind, target_id).

    Raises:
        AmbiguousPrefix: prefix collides (across kinds for a bare id).
        LookupError: no target matches.
        ValueError: unknown kind, or an attempt to resolve a deferred marker.
        IndexError: colon-path position out of range.
    """
    if ref.is_deferred:
        raise ValueError("session-marker targets resolve at ingest, not here")

    # Colon-path
    if ref.conv_ref is not None:
        conv_id = resolve_entity_id(conn, "conversation", ref.conv_ref, owner=owner)
        if conv_id is None:
            raise LookupError(f"conversation not found: {ref.conv_ref}")
        assert ref.kind is not None and ref.position is not None
        return _resolve_colon(conn, conv_id, ref.kind, ref.position, ref.block_position)

    if ref.raw_id is None:
        raise ValueError("empty target ref")

    # Kind-narrowed bare id
    if ref.kind is not None:
        if ref.kind not in _ADDRESSABLE_KINDS:
            valid = ", ".join(sorted(_ADDRESSABLE_KINDS))
            raise ValueError(f"Invalid target kind {ref.kind!r}. Valid: {valid}")
        resolved = resolve_entity_id(conn, ref.kind, ref.raw_id, owner=owner)
        if resolved is None:
            raise LookupError(f"{ref.kind} not found: {ref.raw_id}")
        return ResolvedTarget(ref.kind, resolved)

    # Bare id, kind unknown → cross-kind lookup (decision 2)
    return _resolve_cross_kind(conn, ref.raw_id, owner=owner)


def _resolve_colon(
    conn: sqlite3.Connection,
    conversation_id: str,
    kind: str,
    n: int,
    block_position: int | None = None,
) -> ResolvedTarget:
    """Resolve a colon-path ``<conv>:<kind>:<n>[:<b>]`` to (target_kind, target_id).

    With ``block_position`` set, descends into content block ``b`` (1-based by
    ``block_index``) of the addressed event, returning a ``block`` target.
    """
    if kind not in GRANULAR_KINDS:
        valid = ", ".join(sorted(GRANULAR_KINDS))
        raise ValueError(f"Invalid target kind {kind!r}. Valid: {valid}")
    if n < 1:
        raise ValueError(f"Index must be >= 1, got {n}")

    event_kind = _KIND_TO_EVENT_KIND[kind]
    row = conn.execute(
        "SELECT id FROM events "
        "WHERE conversation_id = ? AND kind = ? "
        "ORDER BY timestamp, id "
        "LIMIT 1 OFFSET ?",
        (conversation_id, event_kind, n - 1),
    ).fetchone()
    if row is None:
        raise IndexError(f"No {kind} at index {n} in conversation {conversation_id[:12]}")

    if block_position is not None:
        # block_index is 0-based and adapter-derived/contiguous — direct lookup,
        # no OFFSET ordering tricks. b (1-based) → block_index = b - 1.
        brow = conn.execute(
            "SELECT id FROM event_content WHERE event_id = ? AND block_index = ?",
            (row["id"], block_position - 1),
        ).fetchone()
        if brow is None:
            raise IndexError(
                f"No block at index {block_position} in {kind} {n} "
                f"of conversation {conversation_id[:12]}"
            )
        return ResolvedTarget("block", brow["id"])

    target_kind = "exchange" if kind == "exchange" else kind
    return ResolvedTarget(target_kind, row["id"])


def _resolve_cross_kind(
    conn: sqlite3.Connection,
    raw: str,
    *,
    owner: str | None = None,
) -> ResolvedTarget:
    """Resolve a bare ULID/prefix across conversations AND events in one lookup.

    A full ULID is unique across both tables in practice. A prefix colliding
    across kinds raises AmbiguousPrefix with kind-labeled candidates.
    """
    from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate

    candidates: list[tuple[str, str]] = []  # (kind, id)
    exact_counts: list[Callable[[], int]] = []

    conv_where: list[str] = []
    conv_params: list[object] = []
    include_conversations = True
    if owner:
        if has_conversation_owners_table(conn):
            conv_where.append(owner_predicate("c.id"))
            conv_params.append(owner)
        else:
            include_conversations = False
    if include_conversations:
        rows, exact_count = prefix_candidates(
            conn, from_sql="conversations c", id_expr="c.id", prefix=raw,
            where=conv_where, params=conv_params,
        )
        candidates.extend(("conversation", row["id"]) for row in rows)
        exact_counts.append(exact_count)

    # Events arm — scoped through the owning conversation with the SAME owner
    # predicate, so an owner-scoped caller can't resolve (and then tag) another
    # tenant's event by ULID prefix. Skipped entirely when the owners table is
    # absent but an owner is demanded (same stance as the conversations arm).
    include_events = not (owner and not has_conversation_owners_table(conn))
    if include_events:
        evt_where: list[str] = []
        evt_params: list[object] = []
        if owner:
            evt_where.append(owner_predicate("e.conversation_id"))
            evt_params.append(owner)
        rows, exact_count = prefix_candidates(
            conn,
            from_sql="events e",
            id_expr="e.id",
            prefix=raw,
            where=evt_where,
            params=evt_params,
            extra_columns=["e.kind AS kind"],
        )
        candidates.extend((row["kind"], row["id"]) for row in rows)
        exact_counts.append(exact_count)

    # Blocks arm (event_content) — scoped through the owning event's conversation
    # with the SAME owner predicate, mirroring the events arm's stance.
    include_blocks = not (owner and not has_conversation_owners_table(conn))
    if include_blocks:
        blk_where: list[str] = []
        blk_params: list[object] = []
        blk_from = "event_content ec"
        if owner:
            blk_from = "event_content ec JOIN events e2 ON e2.id = ec.event_id"
            blk_where.append(owner_predicate("e2.conversation_id"))
            blk_params.append(owner)
        rows, exact_count = prefix_candidates(
            conn, from_sql=blk_from, id_expr="ec.id", prefix=raw,
            where=blk_where, params=blk_params,
        )
        candidates.extend(("block", row["id"]) for row in rows)
        exact_counts.append(exact_count)

    if not candidates:
        raise LookupError(f"not found: {raw}")
    if len(candidates) == 1:
        kind, target_id = candidates[0]
        return ResolvedTarget(kind, target_id)

    # Exact total across both tables (each arm above is capped at 6, so
    # len(candidates) would undercount "and N more"). Each thunk only runs its
    # COUNT(*) here, on the ambiguous path — a unique resolution never pays for it.
    shown = candidates[:5]
    raise AmbiguousPrefix(
        raw,
        [i for _, i in shown],
        sum(exact_count() for exact_count in exact_counts),
        candidate_kinds=[k for k, _ in shown],
        noun="targets",
    )


def alias(conn: sqlite3.Connection, target_kind: str, target_id: str) -> str:
    """Reverse map: a resolved target → its ``<conv-prefix>:<kind>:<n>`` colon-path.

    For conversation/workspace targets, returns the short id (no colon-path).
    Position is computed with the same ``ORDER BY timestamp, id`` the forward
    resolver uses.
    """
    if target_kind in ("conversation", "workspace"):
        return target_id[:12]

    if target_kind == "block":
        return _alias_block(conn, target_id)

    event_kind = _KIND_TO_EVENT_KIND.get(target_kind, target_kind)
    row = conn.execute(
        "SELECT conversation_id, timestamp FROM events WHERE id = ?",
        (target_id,),
    ).fetchone()
    if row is None:
        return target_id[:12]
    conv = row["conversation_id"]
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM events "
        "WHERE conversation_id = ? AND kind = ? "
        "AND (timestamp < ? OR (timestamp = ? AND id <= ?))",
        (conv, event_kind, row["timestamp"], row["timestamp"], target_id),
    ).fetchone()["n"]
    return f"{conv[:12]}:{target_kind}:{n}"


def _alias_block(conn: sqlite3.Connection, block_id: str) -> str:
    """Reverse map a content-block id to its ``<conv>:<kind>:<n>:<b>`` colon-path.

    The 2nd segment is the owning event's real kind; ``b`` is 1-based
    (``block_index + 1``). Falls back to the short id if the block or its event
    is missing.
    """
    brow = conn.execute(
        "SELECT event_id, block_index FROM event_content WHERE id = ?",
        (block_id,),
    ).fetchone()
    if brow is None:
        return block_id[:12]
    erow = conn.execute(
        "SELECT conversation_id, kind, timestamp FROM events WHERE id = ?",
        (brow["event_id"],),
    ).fetchone()
    if erow is None:
        return block_id[:12]
    conv = erow["conversation_id"]
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM events "
        "WHERE conversation_id = ? AND kind = ? "
        "AND (timestamp < ? OR (timestamp = ? AND id <= ?))",
        (conv, erow["kind"], erow["timestamp"], erow["timestamp"], brow["event_id"]),
    ).fetchone()["n"]
    return f"{conv[:12]}:{erow['kind']}:{n}:{brow['block_index'] + 1}"
