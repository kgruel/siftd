"""What a conversation replacement has to carry across delete-then-insert.

Ingest replaces a changed transcript by deleting the conversation and storing
the parse as a new one, with fresh ULIDs throughout. Everything hung off the
old conversation goes with it — by ``ON DELETE CASCADE`` for the declared
children, by the ``tr_polymorphic_*_cleanup`` triggers for the polymorphic
ones — and most of that is *correct*, because the replacement's own parse
supplies it again. What has to survive is the part the transcript does not
contain: facts a person or a server attached to the conversation, which no
re-parse can reproduce.

Two such facts exist today, and the second was missing until #54:

- **tag assignments**, re-pointed by rejoining on the identifiers the
  replacement rows share with their predecessors (the conversation by
  ``external_id``, events by ``UNIQUE (conversation_id, kind, external_id)``);
- **ownership**, copied straight across — ``conversation_owners`` keys on the
  conversation id the caller already has, so there is nothing to rejoin on.

That asymmetry is why this is one snapshot object rather than two helpers: the
mechanics differ per fact, but "take everything, delete, put everything back"
is one operation, and splitting it is how a third site comes to carry only one
of them. ``ingestion.orchestration._take_conversation_for_replacement`` is the
single door — it guards, snapshots, and deletes together.

The enumeration this module is answerable to is *what a delete actually
removes*, not what any registry declares:
``tests/test_live_tagging.py::test_every_cascade_child_is_carried_or_declared``
derives it by deleting a conversation and diffing row counts, then requires
every table to be carried here or listed with a reason it need not be.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from siftd.storage.sql_helpers import has_conversation_owners_table
from siftd.storage.tags import EVENT_TAG_KINDS, apply_tag, get_tag_assignments


@dataclass
class ConversationCarryover:
    """The facts a replacement must move from the old conversation to the new.

    ``dropped_blocks`` counts block-level assignments (``target_kind='block'``,
    keyed by ``event_content.id``) that this deliberately does not carry —
    re-pointing those is deferred to 0.13.0, and counting them keeps the loss
    visible instead of silent.
    """

    conversation: list[tuple[str, str]] = field(default_factory=list)
    """(tag_id, applied_at) for the conversation row itself."""

    events: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    """(target_kind, event_kind, event_external_id, tag_id, applied_at)."""

    owners: list[tuple[str, str | None, str]] = field(default_factory=list)
    """(user_id, push_id, assigned_at) from ``conversation_owners``."""

    dropped_events: int = 0
    """Event assignments whose event has no external_id, so cannot be re-pointed."""

    dropped_blocks: int = 0

    def __bool__(self) -> bool:
        """True when it holds anything — to carry *or* to report.

        The dropped counters are part of it. A carryover holding only
        assignments that cannot be re-pointed (block tags, events with no
        external_id) has nothing to restore but everything to say, and
        reading as empty is exactly how that loss went unannounced in the
        empty-transcript branch of re-ingest.
        """
        return bool(self.conversation or self.events or self.owners or self.dropped)

    @property
    def dropped(self) -> int:
        return self.dropped_events + self.dropped_blocks


def snapshot_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> ConversationCarryover:
    """Capture what a conversation would lose to a delete."""
    carryover = ConversationCarryover(
        conversation=get_tag_assignments(conn, "conversation", conversation_id),
    )

    event_kinds = ",".join("?" * len(EVENT_TAG_KINDS))
    for row in conn.execute(
        f"""
        SELECT ta.target_kind, e.kind, e.external_id, ta.tag_id, ta.applied_at
        FROM tag_assignments ta
        JOIN events e ON e.id = ta.target_id
        WHERE ta.target_kind IN ({event_kinds}) AND e.conversation_id = ?
        """,
        (*sorted(EVENT_TAG_KINDS), conversation_id),
    ).fetchall():
        if row["external_id"] is None:
            # Synthetic event — nothing stable to rejoin on.
            carryover.dropped_events += 1
            continue
        carryover.events.append(
            (row["target_kind"], row["kind"], row["external_id"], row["tag_id"], row["applied_at"])
        )

    # The count below joins every content block of the conversation. Block
    # tagging is new and rare, so ask the indexed question first — "does any
    # block tag exist at all" rides idx_tag_assignments_target and answers no
    # for most databases without touching event_content.
    if conn.execute(
        "SELECT 1 FROM tag_assignments WHERE target_kind = 'block' LIMIT 1"
    ).fetchone():
        carryover.dropped_blocks = conn.execute(
            """
            SELECT COUNT(*) FROM tag_assignments ta
            JOIN event_content ec ON ec.id = ta.target_id
            JOIN events e ON e.id = ec.event_id
            WHERE ta.target_kind = 'block' AND e.conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()[0]

    # conversation_owners only exists on a database that has served a push;
    # a CLI-only database predates it and has nothing to carry.
    if has_conversation_owners_table(conn):
        carryover.owners = [
            (row["user_id"], row["push_id"], row["assigned_at"])
            for row in conn.execute(
                "SELECT user_id, push_id, assigned_at FROM conversation_owners"
                " WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
        ]

    return carryover


def restore_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
    carryover: ConversationCarryover | None,
    *,
    commit: bool = False,
) -> int:
    """Re-attach a carryover to the replacement conversation.

    Returns the number of event assignments that could not be re-pointed
    (their event is not in the replacement conversation). Explicit re-attach,
    same style as :mod:`siftd.storage.migrate_workspaces` — there is no FK to
    cascade from, and the replacement rows carry new ULIDs regardless.

    Ownership needs no rejoin: ``conversation_owners`` keys on the conversation
    id, which the caller passes in, so it is a copy rather than a match. It
    therefore cannot contribute to the unmatched count.
    """
    if carryover is None:
        return 0

    for tag_id, applied_at in carryover.conversation:
        apply_tag(conn, "conversation", conversation_id, tag_id, applied_at=applied_at)

    unmatched = 0
    for target_kind, event_kind, external_id, tag_id, applied_at in carryover.events:
        row = conn.execute(
            "SELECT id FROM events WHERE conversation_id = ? AND kind = ? AND external_id = ?",
            (conversation_id, event_kind, external_id),
        ).fetchone()
        if row is None:
            unmatched += 1
            continue
        apply_tag(conn, target_kind, row["id"], tag_id, applied_at=applied_at)

    if carryover.owners and has_conversation_owners_table(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO conversation_owners"
            " (conversation_id, user_id, push_id, assigned_at) VALUES (?, ?, ?, ?)",
            [(conversation_id, user_id, push_id, assigned_at)
             for user_id, push_id, assigned_at in carryover.owners],
        )

    if commit:
        conn.commit()

    return unmatched
