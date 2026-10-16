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
of them — which is not hypothetical. Both replacement paths shipped a half of
it: ingest carried tags and dropped ownership (#54), and `api/merge.py`, which
replaces on the same natural key, hand-rolled ownership through a pair of temp
tables while hard-deleting the target's tag assignments (#77). Each looked
complete from inside, because neither had a list to be incomplete against.

Two sites replace a conversation with this snapshot, and they differ only in how they get their ids:

- ``ingestion.orchestration._take_conversation_for_replacement`` — the door in
  ingest, which guards, snapshots and deletes together; the replacement keeps
  the transcript's ``external_id`` under a fresh conversation ULID.
- ``api.merge._replace_stale_conversations`` — the merge's stale sweep, which
  snapshots the *target* conversation and restores against the *source* id,
  because the replacement arrives from the other database under its own ULID.

The snapshot must be taken before the delete in both, and that is structural
rather than stylistic: the ``tr_polymorphic_*_cleanup`` triggers are AFTER
DELETE triggers, so they fire whatever the ``foreign_keys`` pragma is set to,
and a post-delete read finds nothing to carry.

A third site moves rather than replaces. A row another path claims cannot be
deleted, so when its transcript belongs to a conversation stored beside it,
``release_carryover`` takes the carried assignments off the row and the
caller re-attaches them to the new conversation. It removes exactly the rows a
snapshot selects, and reports the synthetic and block assignments it leaves
behind as neither carried nor dropped, because they stay where they are.

`tests/architecture/test_replacement_carry.py` holds this list to its
population, asking the schema directly rather than trusting a registry (the
one in `storage/sqlite.py` names five pre-v4 tables and omits two live ones),
and counts the sites above by their calls to the two functions here — not by
ingest's wrapper, which merge does not use and which is therefore how a second
door stayed invisible until #77.

The reverse question is answered by :func:`unrestorable_user_tags`: before a
replacement is made, it counts the user-applied assignments the carry above
would not re-attach, so a caller that must not lose them (``siftd ingest
--reparse``) can decline the replacement instead of finding the loss after the
delete. It reads the same rules as :func:`restore_conversation`, and shares its
legacy-suffix match, so the count and the restore cannot disagree. Asked with
``row_is_kept``, it counts what :func:`release_carryover` would remove instead,
which is less than a delete takes: the third site above removes assignments
without deleting the row, and a gate that only asked about deletes let it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field

from siftd.domain import Response
from siftd.domain.models import CALL_OCCURRENCE_ATTRIBUTE, MERGED_EXTERNAL_IDS_ATTRIBUTE
from siftd.storage.filters import EVENT_TAG_KINDS
from siftd.storage.sql_helpers import has_conversation_owners_table
from siftd.storage.tags import apply_tag, get_tag_assignments, is_shell_tag_rederived

logger = logging.getLogger(__name__)

# The v4 migration (storage/sqlite.py, step 3.5) renamed each duplicate tool_call
# external_id to ``<id>:<rank>`` with rank >= 2, so the events table could hold it
# under UNIQUE (conversation_id, kind, external_id). A re-parse emits the bare id
# only, so a tag on such a legacy row has to fall back to the bare id to follow
# the call it duplicated. A call the assembler gave that suffix as a distinct
# call's identity states ``call_occurrence`` and matches exactly instead.
_LEGACY_DUPLICATE_SUFFIX = re.compile(r"^(?P<base>.+):(?:[2-9]|[1-9][0-9]+)$")


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

    events: list[tuple[str, str, str, str, str, bool]] = field(default_factory=list)
    """(target_kind, event_kind, event_external_id, tag_id, applied_at, exact).

    ``exact`` says the id is a distinct call's own, so no legacy alias applies.
    """

    owners: list[tuple[str, str | None, str]] = field(default_factory=list)
    """(user_id, push_id, assigned_at) from ``conversation_owners``."""

    dropped_events: int = 0
    """Event assignments whose event has no external_id, so cannot be re-pointed."""

    dropped_blocks: int = 0

    def parts(self) -> list[str]:
        """The nonzero things this holds, named — for a loss warning.

        One enumeration, and everything else derives from it. Truthiness and
        the warning text used to enumerate the fields separately, in different
        modules, and adding ``owners`` to one and not the other made a
        carryover that held *only* ownership truthy with nothing to say: the
        empty-transcript branch fired and named an empty list, un-reporting
        exactly the loss #54 is about. A fifth carried fact is now one line
        here rather than three edits that can half-land.

        Enumerated rather than templated, so a carryover holding only
        assignments that could never be re-pointed doesn't report "0
        conversation tag(s) and 0 element tag(s)" — a warning that names
        everything except the loss it fired for.
        """
        named = [
            (len(self.conversation), "conversation tag(s)"),
            (len(self.events), "element tag(s)"),
            (len(self.owners), "ownership row(s)"),
            (self.dropped_events, "synthetic-event tag(s)"),
            (self.dropped_blocks, "block tag(s)"),
        ]
        return [f"{count} {label}" for count, label in named if count]

    def __bool__(self) -> bool:
        """True when it holds anything — to carry *or* to report.

        The dropped counters are part of it. A carryover holding only
        assignments that cannot be re-pointed (block tags, events with no
        external_id) has nothing to restore but everything to say, and
        reading as empty is exactly how that loss went unannounced in the
        empty-transcript branch of re-ingest.
        """
        return bool(self.parts())

    def describe(self) -> str:
        """``parts()`` as one clause. Never empty when the carryover is truthy."""
        return ", ".join(self.parts())

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

    # CROSS JOIN, and it is load-bearing rather than decorative: it is SQLite's
    # documented way to fix the join order, and without it the plan depends on
    # whether `sqlite_stat1` exists. With no stats the planner drives from
    # `tag_assignments`, seeking `idx_tag_assignments_target` on `target_kind`
    # alone — every event-kind assignment in the whole database, per call — and
    # probes `events` by id. Driving from `events` instead uses
    # `idx_events_conversation_kind` and then both columns of the tag index.
    #
    # Measured on 300 conversations / 6,000 event tags with no stats: 1654µs
    # against 17µs per conversation, same rows. Write paths now refresh stats
    # as they finish (`refresh_planner_stats`, #80), but a merge's replacements
    # run *before* its refresh: a database last written before that change, or
    # one whose refresh failed, still reaches this with none. The pin stays.
    event_kinds = ",".join("?" * len(EVENT_TAG_KINDS))
    for row in conn.execute(
        f"""
        SELECT ta.target_kind, e.kind, e.external_id, ta.tag_id, ta.applied_at,
               EXISTS (
                   SELECT 1 FROM attributes a
                   WHERE a.target_kind = 'tool_call' AND a.target_id = e.id AND a.key = ?
               ) AS exact
        FROM events e
        CROSS JOIN tag_assignments ta ON ta.target_id = e.id
        WHERE ta.target_kind IN ({event_kinds}) AND e.conversation_id = ?
        """,
        (CALL_OCCURRENCE_ATTRIBUTE, *sorted(EVENT_TAG_KINDS), conversation_id),
    ).fetchall():
        if row["external_id"] is None:
            # Synthetic event — nothing stable to rejoin on.
            carryover.dropped_events += 1
            continue
        carryover.events.append(
            (row["target_kind"], row["kind"], row["external_id"], row["tag_id"], row["applied_at"], bool(row["exact"]))
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

    # `open_database`'s write branch ensures this table, so on the ingest path
    # the guard is always true — it is here for a connection that never took
    # that branch (a read-only open, a raw sqlite3 handle in a test), not
    # because CLI-only databases lack the table. They have it, empty.
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


def release_carryover(
    conn: sqlite3.Connection,
    conversation_id: str,
    carryover: ConversationCarryover,
) -> ConversationCarryover:
    """Take what ``carryover`` holds off a conversation that is kept, and return what was taken.

    The counterpart of a replacement for a row that cannot be deleted: a row
    another path claims must stay, so its assignments move instead, to the
    conversation that now holds its transcript (the caller re-attaches the
    returned carryover). Only what the snapshot carries is removed, the same
    rows it selected: synthetic-event and block assignments were never carried,
    so they stay on the row, and the returned carryover reports them as neither
    dropped nor carried.
    """
    conn.execute(
        "DELETE FROM tag_assignments WHERE target_kind = 'conversation' AND target_id = ?",
        (conversation_id,),
    )
    event_kinds = ",".join("?" * len(EVENT_TAG_KINDS))
    conn.execute(
        f"""
        DELETE FROM tag_assignments
        WHERE target_kind IN ({event_kinds})
          AND target_id IN (
              SELECT id FROM events
              WHERE conversation_id = ? AND external_id IS NOT NULL
          )
        """,
        (*sorted(EVENT_TAG_KINDS), conversation_id),
    )
    if carryover.owners and has_conversation_owners_table(conn):
        conn.execute("DELETE FROM conversation_owners WHERE conversation_id = ?", (conversation_id,))
    return dataclasses.replace(carryover, dropped_events=0, dropped_blocks=0)


def _find_event(
    conn: sqlite3.Connection,
    conversation_id: str,
    event_kind: str,
    external_id: str,
    *,
    exact: bool = False,
) -> sqlite3.Row | None:
    """The replacement's event for a carried assignment, or None.

    Exact match first. Then the two ids an event can answer to besides its own:
    a legacy duplicate suffix on a tool call is retried on its bare id (the
    migration renamed nothing else; not when ``exact``, where the suffix names a
    distinct call), and a response also answers to the id of any later chunk
    merged into it.
    """
    sql = "SELECT id FROM events WHERE conversation_id = ? AND kind = ? AND external_id = ?"
    row = conn.execute(sql, (conversation_id, event_kind, external_id)).fetchone()
    if row is None and event_kind == "response":
        row = conn.execute(
            """
            SELECT e.id FROM events e
            JOIN attributes a ON a.target_id = e.id AND a.target_kind = 'response' AND a.key = ?
            WHERE e.conversation_id = ? AND e.kind = 'response'
              AND CASE WHEN json_valid(a.value)
                       THEN EXISTS (SELECT 1 FROM json_each(a.value) WHERE value = ?)
                       ELSE 0 END
            """,
            (MERGED_EXTERNAL_IDS_ATTRIBUTE, conversation_id, external_id),
        ).fetchone()
    if row is None and event_kind == "tool_call" and not exact:
        match = _LEGACY_DUPLICATE_SUFFIX.match(external_id)
        if match:
            row = conn.execute(sql, (conversation_id, event_kind, match["base"])).fetchone()
    return row


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
    for target_kind, event_kind, external_id, tag_id, applied_at, exact in carryover.events:
        row = _find_event(conn, conversation_id, event_kind, external_id, exact=exact)
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


def restore_and_report(
    conn: sqlite3.Connection,
    conversation_id: str,
    carryover: ConversationCarryover | None,
    *,
    subject: str,
    operation: str,
) -> int:
    """:func:`restore_conversation`, plus the warnings for what it could not carry.

    This is what a replacement door calls; ``restore_conversation`` is the
    mechanism under it. The split matters because the loss is only knowable
    here — ``unmatched`` is computed by the restore, the dropped counters by
    the snapshot, and a door that reported one without the other would announce
    part of its own data loss.

    It is one function rather than one per door for the reason the module
    exists: merge grew its own version of this warning and it had already
    diverged, lumping element and block losses into a single count with a
    vaguer cause. The two are not interchangeable — an unmatched element tag
    means the event is gone from the replacement, which is usually correct,
    while a dropped block tag is a capability that is simply not built yet, and
    only the second is repaired by a future release.

    ``subject`` names the conversation the way its door addresses it (a
    transcript's ``external_id`` for ingest, the source conversation id for
    merge); ``operation`` is the verb that reads correctly in the sentence.
    """
    unmatched = restore_conversation(conn, conversation_id, carryover)
    if carryover is None:
        return 0

    lost = unmatched + carryover.dropped_events
    if lost:
        logger.warning(
            f"{lost} element tag(s) on {subject} could not be carried across "
            f"{operation} (the event is no longer present, or is synthetic)"
        )
    if carryover.dropped_blocks:
        logger.warning(
            f"{carryover.dropped_blocks} block tag(s) on {subject} were dropped "
            f"by {operation} — block-level re-pointing is not implemented yet"
        )
    return unmatched


def merged_external_ids(response: Response) -> list[str]:
    """The ids of the later chunks merged into ``response``, as the assembler recorded them."""
    raw = response.attributes.get(MERGED_EXTERNAL_IDS_ATTRIBUTE)
    return json.loads(raw) if raw else []


def _replacement_carries(
    event_kind: str,
    external_id: str,
    replacement_keys: frozenset[tuple[str, str]],
    *,
    exact: bool = False,
) -> bool:
    """Whether a replacement's events include the one an assignment points at.

    Mirrors :func:`_find_event`, which :func:`restore_conversation` uses, so the
    count and the restore agree: exact match first, and a legacy duplicate
    suffix on a tool call falls back to its bare id unless ``exact``. The keys include the ids
    merged chunks answer to (see :func:`merged_external_ids`), so a chunk id
    matches exactly here.
    """
    if (event_kind, external_id) in replacement_keys:
        return True
    if event_kind == "tool_call" and not exact:
        match = _LEGACY_DUPLICATE_SUFFIX.match(external_id)
        return match is not None and ("tool_call", match["base"]) in replacement_keys
    return False


def unrestorable_user_tags(
    conn: sqlite3.Connection,
    conversation_id: str,
    replacement_keys: frozenset[tuple[str, str]],
    *,
    replacement_exists: bool = True,
    row_is_kept: bool = False,
) -> int:
    """Count the user-applied tag assignments a replacement would not carry.

    ``replacement_keys`` is the ``(event kind, external_id)`` set the re-parse
    will store for this conversation. This answers "would a delete-then-insert
    lose anything a person applied?" before the delete, so a caller can decline
    a replacement instead of finding the loss afterwards.

    ``replacement_exists=False`` says the re-parse stores no conversation at all
    (it parses to nothing), so there is nothing to carry anything onto.

    ``row_is_kept=True`` asks the question of :func:`release_carryover` instead
    of a delete: the row stays, and only the assignments the snapshot selects are
    taken off it (event assignments with an ``external_id``). Synthetic-event and
    block assignments are not removed, so they are not lost, and a replacement
    always exists to take the rest.

    What counts, and why:

    - **Conversation-level tags count only when there is no replacement.**
      :func:`restore_conversation` re-attaches them to a replacement
      unconditionally; with no replacement they go with the deleted row.
    - **Event-level tags count unless the replacement carries the event.** An
      event with no ``external_id`` can never be matched, so every tag on it
      counts. Otherwise it counts when the replacement has no event of that
      ``(kind, external_id)``.
    - **Block-level tags always count.** Re-pointing them is not implemented
      (see :class:`ConversationCarryover`), so a replacement loses every one.
    - **Re-derived tool-call tags never count.** A ``shell:<category>`` tag that
      :func:`~siftd.storage.tags.is_shell_tag_rederived` says the re-parse
      reproduces is the parser's output, not a person's, so losing it loses
      nothing. Note that the tag *name* alone does not separate the two.
    """
    lost = 0

    if not replacement_exists:
        lost += conn.execute(
            "SELECT COUNT(*) FROM tag_assignments "
            "WHERE target_kind = 'conversation' AND target_id = ?",
            (conversation_id,),
        ).fetchone()[0]

    # CROSS JOIN for the same planner reason as snapshot_conversation: the
    # events-first plan, not the tag-index-first one, is the fast one.
    event_kinds = ",".join("?" * len(EVENT_TAG_KINDS))
    for row in conn.execute(
        f"""
        SELECT e.kind, e.external_id, t.name AS tag_name,
               tl.name AS tool_name, etc.input AS tool_input,
               EXISTS (
                   SELECT 1 FROM attributes a
                   WHERE a.target_kind = 'tool_call' AND a.target_id = e.id AND a.key = ?
               ) AS exact
        FROM events e
        CROSS JOIN tag_assignments ta ON ta.target_id = e.id
        JOIN tags t ON t.id = ta.tag_id
        LEFT JOIN event_tool_call etc ON etc.event_id = e.id
        LEFT JOIN tools tl ON tl.id = etc.tool_id
        WHERE ta.target_kind IN ({event_kinds}) AND e.conversation_id = ?
        """,
        (CALL_OCCURRENCE_ATTRIBUTE, *sorted(EVENT_TAG_KINDS), conversation_id),
    ).fetchall():
        if is_shell_tag_rederived(row["tag_name"], row["tool_name"], row["tool_input"]):
            continue
        if row["external_id"] is None:
            # Never re-pointable. A delete loses it; a release leaves it on the row.
            if not row_is_kept:
                lost += 1
        elif not _replacement_carries(row["kind"], row["external_id"], replacement_keys, exact=bool(row["exact"])):
            lost += 1

    if not row_is_kept and conn.execute(
        "SELECT 1 FROM tag_assignments WHERE target_kind = 'block' LIMIT 1"
    ).fetchone():
        lost += conn.execute(
            """
            SELECT COUNT(*) FROM tag_assignments ta
            JOIN event_content ec ON ec.id = ta.target_id
            JOIN events e ON e.id = ec.event_id
            WHERE ta.target_kind = 'block' AND e.conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()[0]

    return lost
