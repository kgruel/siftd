"""Tag CRUD operations for siftd storage."""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from siftd.ids import ulid as _ulid
from siftd.storage.filters import ALL_TAG_KINDS, EVENT_TAG_KINDS
from siftd.storage.sql_helpers import has_conversation_owners_table, owner_predicate

_VALID_TARGET_KINDS = ALL_TAG_KINDS

# In-process cache for tag name -> id lookups.
# Only valid within a single connection lifetime. Cleared on module reload.
_tag_cache: dict[str, str] = {}


def _invalidate_tag_cache(*names: str) -> None:
    """Drop cached tag-name lookups that are no longer valid."""
    for name in names:
        _tag_cache.pop(name, None)


def get_or_create_tag(conn: sqlite3.Connection, name: str, description: str | None = None) -> str:
    """Get or create a tag by name, return id (ULID)."""
    if name in _tag_cache:
        return _tag_cache[name]

    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        _tag_cache[name] = row["id"]
        return row["id"]

    ulid = _ulid()
    conn.execute(
        "INSERT INTO tags (id, name, description, created_at) VALUES (?, ?, ?, ?)",
        (ulid, name, description, datetime.now(UTC).isoformat())
    )
    _tag_cache[name] = ulid
    return ulid


def get_tag_id(conn: sqlite3.Connection, name: str) -> str | None:
    """Return tag id for name, or None if not found."""
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    return row["id"] if row else None


def apply_tag(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
    tag_id: str,
    *,
    applied_at: str | None = None,
    commit: bool = False,
) -> str | None:
    """Apply a tag to an entity via tag_assignments. Returns assignment id or None if already applied.

    target_kind: 'conversation' | 'workspace' | 'prompt' | 'response' | 'tool_call' | 'exchange'
    target_id: ULID of the target entity
    tag_id: ULID of the tag (pre-resolved; use get_or_create_tag if needed)
    """
    if target_kind not in _VALID_TARGET_KINDS:
        raise ValueError(f"Unknown target_kind {target_kind!r}. Valid: {sorted(_VALID_TARGET_KINDS)}")
    ulid = _ulid()
    ts = applied_at or datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT OR IGNORE INTO tag_assignments (id, target_kind, target_id, tag_id, applied_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ulid, target_kind, target_id, tag_id, ts),
    )
    if cur.rowcount == 0:
        return None  # Already applied
    if commit:
        conn.commit()
    return ulid


def remove_tag(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> bool:
    """Remove a tag from an entity. Returns True if a row was deleted, False if not applied.

    target_kind: 'conversation' | 'workspace' | 'prompt' | 'response' | 'tool_call' | 'exchange'
    """
    if target_kind not in _VALID_TARGET_KINDS:
        raise ValueError(f"Unknown target_kind {target_kind!r}. Valid: {sorted(_VALID_TARGET_KINDS)}")
    cur = conn.execute(
        "DELETE FROM tag_assignments WHERE target_kind = ? AND target_id = ? AND tag_id = ?",
        (target_kind, target_id, tag_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def get_tags_for(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
) -> list[sqlite3.Row]:
    """Return tag rows for a given target."""
    return conn.execute(
        "SELECT t.name, t.description, ta.applied_at "
        "FROM tag_assignments ta JOIN tags t ON t.id = ta.tag_id "
        "WHERE ta.target_kind = ? AND ta.target_id = ?",
        (target_kind, target_id),
    ).fetchall()


def get_tag_assignments(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
) -> list[tuple[str, str]]:
    """Return (tag_id, applied_at) pairs assigned to a target.

    Unlike :func:`get_tags_for` (which resolves names for display), this
    returns the raw assignment identity so a caller can re-point the same
    assignments at a replacement row via :func:`apply_tag`. Used by ingest
    when a changed transcript forces delete-then-insert of a conversation:
    the AFTER DELETE cleanup trigger takes the assignments with it, so they
    must be snapshotted first.
    """
    if target_kind not in _VALID_TARGET_KINDS:
        raise ValueError(f"Unknown target_kind {target_kind!r}. Valid: {sorted(_VALID_TARGET_KINDS)}")
    return [
        (row["tag_id"], row["applied_at"])
        for row in conn.execute(
            "SELECT tag_id, applied_at FROM tag_assignments "
            "WHERE target_kind = ? AND target_id = ?",
            (target_kind, target_id),
        ).fetchall()
    ]


@dataclass
class ConversationTagSnapshot:
    """Tag assignments held by a conversation and its events, taken pre-delete.

    Ingest replaces a changed transcript's conversation with delete-then-insert,
    and the ``tr_polymorphic_*_cleanup`` AFTER DELETE triggers take every
    assignment with it while the replacement rows get fresh ULIDs. So the
    assignments have to be captured before the delete and re-pointed after the
    insert, keyed by something the replacement rows share with their
    predecessors: the conversation is found by ``external_id`` (the caller
    already has its new id), and events by
    ``UNIQUE (conversation_id, kind, external_id)``.

    ``dropped_blocks`` counts block-level assignments (``target_kind='block'``,
    keyed by ``event_content.id``) that this snapshot deliberately does not
    carry — re-pointing those is deferred to 0.13.0, and counting them keeps
    the loss visible instead of silent.
    """

    conversation: list[tuple[str, str]] = field(default_factory=list)
    """(tag_id, applied_at) for the conversation row itself."""

    events: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    """(target_kind, event_kind, event_external_id, tag_id, applied_at)."""

    dropped_events: int = 0
    """Event assignments whose event has no external_id, so cannot be re-pointed."""

    dropped_blocks: int = 0

    def __bool__(self) -> bool:
        """True when the snapshot holds anything — to carry *or* to report.

        The dropped counters are part of it. A snapshot holding only
        assignments that cannot be re-pointed (block tags, events with no
        external_id) has nothing to restore but everything to say, and
        reading as empty is exactly how that loss went unannounced in the
        empty-transcript branch of re-ingest.
        """
        return bool(self.conversation or self.events or self.dropped)

    @property
    def dropped(self) -> int:
        return self.dropped_events + self.dropped_blocks


def snapshot_conversation_tags(
    conn: sqlite3.Connection,
    conversation_id: str,
) -> ConversationTagSnapshot:
    """Capture the assignments a conversation would lose to a delete."""
    snapshot = ConversationTagSnapshot(
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
            snapshot.dropped_events += 1
            continue
        snapshot.events.append(
            (row["target_kind"], row["kind"], row["external_id"], row["tag_id"], row["applied_at"])
        )

    snapshot.dropped_blocks = conn.execute(
        """
        SELECT COUNT(*) FROM tag_assignments ta
        JOIN event_content ec ON ec.id = ta.target_id
        JOIN events e ON e.id = ec.event_id
        WHERE ta.target_kind = 'block' AND e.conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()[0]

    return snapshot


def restore_conversation_tags(
    conn: sqlite3.Connection,
    conversation_id: str,
    snapshot: ConversationTagSnapshot | None,
    *,
    commit: bool = False,
) -> int:
    """Re-point a snapshot's assignments at the replacement rows.

    Returns the number of event assignments that could not be re-pointed
    (their event is not in the replacement conversation). Explicit re-point,
    same style as :mod:`siftd.storage.migrate_workspaces` — there is no FK to
    cascade from.
    """
    if snapshot is None:
        return 0

    for tag_id, applied_at in snapshot.conversation:
        apply_tag(conn, "conversation", conversation_id, tag_id, applied_at=applied_at)

    unmatched = 0
    for target_kind, event_kind, external_id, tag_id, applied_at in snapshot.events:
        row = conn.execute(
            "SELECT id FROM events WHERE conversation_id = ? AND kind = ? AND external_id = ?",
            (conversation_id, event_kind, external_id),
        ).fetchone()
        if row is None:
            unmatched += 1
            continue
        apply_tag(conn, target_kind, row["id"], tag_id, applied_at=applied_at)

    if commit:
        conn.commit()

    return unmatched


def rename_tag(conn: sqlite3.Connection, old_name: str, new_name: str, *, commit: bool = False) -> bool:
    """Rename a tag. Returns True if renamed, False if old_name not found.

    Raises ValueError if new_name already exists.
    """
    from siftd.storage.sessions import rename_pending_tag

    # Check new_name doesn't already exist
    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (new_name,))
    if cur.fetchone():
        raise ValueError(f"Tag '{new_name}' already exists")

    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (old_name,))
    row = cur.fetchone()
    if not row:
        return False

    tag_id = row["id"]
    cur = conn.execute("UPDATE tags SET name = ? WHERE name = ?", (new_name, old_name))
    rename_pending_tag(conn, old_name, new_name)
    _invalidate_tag_cache(old_name, new_name)
    _tag_cache[new_name] = tag_id
    if commit:
        conn.commit()
    return cur.rowcount > 0


def delete_tag(conn: sqlite3.Connection, name: str, *, commit: bool = False) -> int:
    """Delete a tag and all its associations. Returns count of entity associations removed."""
    from siftd.storage.sessions import delete_pending_tag

    cur = conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    if not row:
        return -1  # tag not found

    tag_id = row["id"]

    # Delete all assignments from the polymorphic table
    cur = conn.execute("DELETE FROM tag_assignments WHERE tag_id = ?", (tag_id,))
    removed = cur.rowcount

    # Delete the tag itself
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    delete_pending_tag(conn, name)
    _invalidate_tag_cache(name)

    if commit:
        conn.commit()
    return removed


def _tag_owner_participation(
    conn: sqlite3.Connection,
    tag_id: str,
    owner: str | None,
    *,
    others: bool,
) -> bool:
    """True when the tag has an assignment on an entity owned by ``owner``
    (``others=False``) or by anyone else (``others=True``).

    One arm per ownership join topology: conversation (direct), event kinds
    (events → conversation), block (event_content → events → conversation),
    workspace (participation — any conversation in the workspace).
    """
    if not owner:
        return False
    if not has_conversation_owners_table(conn):
        return False

    op = "!=" if others else "="
    event_kinds = ",".join(f"'{k}'" for k in sorted(EVENT_TAG_KINDS))
    arms = (
        # conversation-kind: direct ownership join
        "SELECT 1 FROM tag_assignments ta "
        "JOIN conversation_owners co ON co.conversation_id = ta.target_id "
        f"WHERE ta.tag_id = ? AND ta.target_kind = 'conversation' AND co.user_id {op} ? LIMIT 1",
        # event-kind: ownership via event → conversation
        "SELECT 1 FROM tag_assignments ta "
        "JOIN events e ON e.id = ta.target_id "
        "JOIN conversation_owners co ON co.conversation_id = e.conversation_id "
        f"WHERE ta.tag_id = ? AND ta.target_kind IN ({event_kinds}) "
        f"AND co.user_id {op} ? LIMIT 1",
        # block-kind: ownership via event_content → event → conversation
        "SELECT 1 FROM tag_assignments ta "
        "JOIN event_content ec ON ec.id = ta.target_id "
        "JOIN events e ON e.id = ec.event_id "
        "JOIN conversation_owners co ON co.conversation_id = e.conversation_id "
        f"WHERE ta.tag_id = ? AND ta.target_kind = 'block' AND co.user_id {op} ? LIMIT 1",
        # workspace-kind: participation — any conversation in the workspace
        "SELECT 1 FROM tag_assignments ta "
        "JOIN conversations c ON c.workspace_id = ta.target_id "
        "JOIN conversation_owners co ON co.conversation_id = c.id "
        f"WHERE ta.tag_id = ? AND ta.target_kind = 'workspace' AND co.user_id {op} ? LIMIT 1",
    )
    return any(conn.execute(sql, (tag_id, owner)).fetchone() for sql in arms)


def tag_used_by_other_owners(
    conn: sqlite3.Connection,
    tag_id: str,
    owner: str | None,
) -> bool:
    """Return True when a tag is associated with entities owned by other users."""
    return _tag_owner_participation(conn, tag_id, owner, others=True)


def owner_uses_tag(
    conn: sqlite3.Connection,
    tag_id: str,
    owner: str | None,
) -> bool:
    """Return True when this tag is associated with an entity ``owner`` owns.

    The owner-scoped analogue of the existence check in :func:`set_tag_pin`, and
    the positive inverse of :func:`tag_used_by_other_owners` (``co.user_id = ?``
    rather than ``!= ?``). ``list_tags`` is owner-scoped, so the pin write guards
    on the owner actually using the tag — otherwise a crafted request could pin a
    foreign tenant's tag and surface its name (a cross-tenant existence oracle),
    exactly as the workspace-pin participation guard prevents for workspaces.
    """
    return _tag_owner_participation(conn, tag_id, owner, others=False)


def ensure_tag_pins_table(conn: sqlite3.Connection) -> None:
    """Create the per-owner tag-pin table if absent. Idempotent.

    Pins are owner-scoped UI preference state (which tags a user keeps in their
    'pinned' zone). The ``tags`` table is global — it has no owner column — so a
    pin cannot live there without pinning a tag for *every* tenant. This mirrors
    how ``conversation_owners`` is handled: ensured on every write-open (see
    ``open_database``), guarded by :func:`has_tag_pins_table` on reads so a
    read-only open of a DB that has had no write since this shipped degrades to
    'nothing pinned' instead of raising 'no such table'.

    ``owner`` is stored as ``''`` for the unscoped/local (no-auth) case, matching
    the ``if owner`` scoping convention used everywhere else — keeping the column
    NOT NULL so the composite PRIMARY KEY de-dupes correctly (SQLite treats NULLs
    as distinct, which would let the same tag be pinned twice).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_pins (
            owner      TEXT NOT NULL,
            tag_id     TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            pinned_at  TEXT NOT NULL,
            PRIMARY KEY (owner, tag_id)
        )
        """
    )


def has_tag_pins_table(conn: sqlite3.Connection) -> bool:
    """Return True if the tag_pins table exists (created lazily on write-open)."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_pins'"
        ).fetchone()
        is not None
    )


def pin_tag(
    conn: sqlite3.Connection,
    *,
    owner: str | None,
    tag_id: str,
    pinned_at: str | None = None,
    commit: bool = False,
) -> bool:
    """Pin a tag for an owner. Returns True if newly pinned, False if already pinned."""
    ensure_tag_pins_table(conn)
    ts = pinned_at or datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT OR IGNORE INTO tag_pins (owner, tag_id, pinned_at) VALUES (?, ?, ?)",
        (owner or "", tag_id, ts),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def unpin_tag(
    conn: sqlite3.Connection,
    *,
    owner: str | None,
    tag_id: str,
    commit: bool = False,
) -> bool:
    """Unpin a tag for an owner. Returns True if a pin was removed."""
    if not has_tag_pins_table(conn):
        return False
    cur = conn.execute(
        "DELETE FROM tag_pins WHERE owner = ? AND tag_id = ?",
        (owner or "", tag_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


# Join-chain shapes for list_tags' per-kind COUNT subqueries: how ta.target_id
# reaches conversations.started_at (for time filtering) / conversation_owners
# (for owner scoping). tool_call/exchange/prompt/response share the events
# anchor (an exchange's target_id is its prompt event's id); block descends
# one hop further via event_content. Kept private to this module — a global
# kind->join-chain map was judged DEFER, no next kind is coming.
_CHAIN_NONE = "none"  # target_id IS the conversation id (conversation kind)
_CHAIN_WORKSPACE = "workspace_reverse"  # target_id is a workspace id
_CHAIN_EVENT = "event"  # target_id is an events.id
_CHAIN_BLOCK = "block"  # target_id is an event_content.id

_CHAIN_JOINS: dict[str, tuple[list[str], str]] = {
    _CHAIN_NONE: (["JOIN conversations c ON c.id = ta.target_id"], "c.id"),
    _CHAIN_WORKSPACE: (["JOIN conversations c ON c.workspace_id = ta.target_id"], "c.id"),
    _CHAIN_EVENT: (
        ["JOIN events e ON e.id = ta.target_id", "JOIN conversations c ON c.id = e.conversation_id"],
        "e.conversation_id",
    ),
    _CHAIN_BLOCK: (
        [
            "JOIN event_content ec ON ec.id = ta.target_id",
            "JOIN events e ON e.id = ec.event_id",
            "JOIN conversations c ON c.id = e.conversation_id",
        ],
        "e.conversation_id",
    ),
}
# Whether since/before filtering means anything for this chain (a workspace
# assignment has no conversation anchor — time-invariant by design), and
# whether an owner predicate alone justifies the join (vs. `conversation`,
# where target_id is already usable directly with no join at all).
_CHAIN_TIME_CAPABLE = {_CHAIN_NONE: True, _CHAIN_WORKSPACE: False, _CHAIN_EVENT: True, _CHAIN_BLOCK: True}
_CHAIN_OWNER_NEEDS_JOIN = {_CHAIN_NONE: False, _CHAIN_WORKSPACE: True, _CHAIN_EVENT: True, _CHAIN_BLOCK: True}

# kind -> join-chain shape. Order determines column order in list_tags' SELECT.
_COUNT_KINDS: dict[str, str] = {
    "conversation": _CHAIN_NONE,
    "workspace": _CHAIN_WORKSPACE,
    "tool_call": _CHAIN_EVENT,
    "exchange": _CHAIN_EVENT,
    "prompt": _CHAIN_EVENT,
    "response": _CHAIN_EVENT,
    "block": _CHAIN_BLOCK,
}


def _tag_count_subquery(
    kind: str,
    chain: str,
    *,
    since: str | None,
    before: str | None,
    owner: str | None,
) -> tuple[str, list[object]]:
    """Build one ``(SELECT COUNT... )`` subquery for a list_tags count column."""
    time_capable = _CHAIN_TIME_CAPABLE[chain]
    needs_join = (bool(since or before) and time_capable) or (owner and _CHAIN_OWNER_NEEDS_JOIN[chain])

    joins: list[str] = []
    owner_col = "ta.target_id"
    select = "COUNT(*)"
    if needs_join:
        joins, owner_col = _CHAIN_JOINS[chain]
        if chain == _CHAIN_WORKSPACE:
            select = "COUNT(DISTINCT ta.target_id)"

    where = ["ta.tag_id = t.id", f"ta.target_kind = '{kind}'"]
    params: list[object] = []
    if owner:
        where.append(owner_predicate(owner_col))
        params.append(owner)
    if time_capable:
        if since:
            where.append("c.started_at >= ?")
            params.append(since)
        if before:
            where.append("c.started_at < ?")
            params.append(before)

    sql = f"SELECT {select} FROM tag_assignments ta {' '.join(joins)} WHERE {' AND '.join(where)}"
    return sql, params


def list_tags(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    before: str | None = None,
    owner: str | None = None,
) -> list[dict]:
    """List all tags with usage counts.

    Args:
        conn: Database connection.
        since: Only count associations where the conversation started after this ISO date.
        before: Only count associations where the conversation started before this ISO date.
        owner: Only count associations owned by this user_id.

    ``workspace_count`` ignores since/before: a workspace assignment has no
    conversation anchor, so a time-filtered workspace count has no coherent
    meaning. Every other count column is time-filtered.
    """
    if owner and not has_conversation_owners_table(conn):
        return []

    count_sqls: dict[str, str] = {}
    all_params: list[object] = []
    for kind, chain in _COUNT_KINDS.items():
        sql, params = _tag_count_subquery(kind, chain, since=since, before=before, owner=owner)
        count_sqls[kind] = sql
        all_params.extend(params)

    # pinned flag (owner-scoped). tag_pins is created lazily on a write-open and
    # may be absent on a read-only open of a DB unwritten since this shipped —
    # guard the join so the read degrades to "nothing pinned" rather than raising.
    if has_tag_pins_table(conn):
        pin_select = "(tp.tag_id IS NOT NULL)"
        pin_join = "LEFT JOIN tag_pins tp ON tp.tag_id = t.id AND tp.owner = ?"
        pin_params: list[object] = [owner or ""]
    else:
        pin_select = "0"
        pin_join = ""
        pin_params = []

    count_columns = ",\n            ".join(
        f"({count_sqls[kind]}) as {kind}_count" for kind in _COUNT_KINDS
    )
    sql = f"""
        SELECT
            t.name,
            t.description,
            t.created_at,
            {count_columns},
            {pin_select} as pinned
        FROM tags t
        {pin_join}
        ORDER BY t.name
    """
    all_params.extend(pin_params)

    cur = conn.execute(sql, all_params)
    count_columns_out = [f"{kind}_count" for kind in _COUNT_KINDS]
    rows = [
        {
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
            **{col: row[col] for col in count_columns_out},
            "pinned": bool(row["pinned"]),
        }
        for row in cur.fetchall()
    ]
    if owner:
        # Drop tags this owner doesn't use — but never a pinned one, or the pin
        # would orphan: the tag vanishes from the owner's view yet the tag_pins
        # row persists (untagging doesn't cascade), leaving it impossible to
        # unpin. A pinned-but-unused tag stays, shown with a zero dominant count.
        rows = [r for r in rows if r["pinned"] or any(r[col] for col in count_columns_out)]
    return rows


def tag_activity_series(
    conn: sqlite3.Connection,
    *,
    weeks: int = 12,
    owner: str | None = None,
) -> dict[str, list[int]]:
    """Per-tag activity sparkline: distinct conversations bearing each tag,
    bucketed by conversation ``started_at`` into ``weeks`` week-buckets.

    The window ends at the most recent conversation (data-relative, not
    wall-clock ``now()``) so the series is deterministic and testable. Returns
    ``{tag_name: [oldest, ..., newest]}`` of length ``weeks``; tags with no
    activity in the window are omitted. Owner-scoped when ``owner`` is set.

    A conversation "bears" a tag when the tag is applied to the conversation
    directly (``target_kind='conversation'``), to any of its events
    (tool_call/prompt/response/exchange), or to a content block of one — the
    union of the association paths the flat counts in :func:`list_tags` keep
    separate.
    """
    if owner and not has_conversation_owners_table(conn):
        return {}

    ref_where = f" WHERE {owner_predicate('c.id')}" if owner else ""
    owner_params = [owner] if owner else []
    ref = conn.execute(
        f"SELECT MAX(c.started_at) FROM conversations c{ref_where}",
        owner_params,
    ).fetchone()[0]
    if not ref:
        return {}

    owner_clause = f" AND {owner_predicate('c.id')}" if owner else ""
    rows = conn.execute(
        f"""
        SELECT t.name AS name,
               CAST((julianday(?) - julianday(c.started_at)) / 7 AS INTEGER) AS weeks_ago,
               COUNT(DISTINCT tc.conversation_id) AS n
        FROM (
            SELECT ta.tag_id AS tag_id, ta.target_id AS conversation_id
            FROM tag_assignments ta
            WHERE ta.target_kind = 'conversation'
            UNION
            SELECT ta.tag_id AS tag_id, e.conversation_id AS conversation_id
            FROM tag_assignments ta
            JOIN events e ON e.id = ta.target_id
            WHERE ta.target_kind IN ('tool_call', 'prompt', 'response', 'exchange')
            UNION
            SELECT ta.tag_id AS tag_id, e.conversation_id AS conversation_id
            FROM tag_assignments ta
            JOIN event_content ec ON ec.id = ta.target_id
            JOIN events e ON e.id = ec.event_id
            WHERE ta.target_kind = 'block'
        ) tc
        JOIN tags t ON t.id = tc.tag_id
        JOIN conversations c ON c.id = tc.conversation_id
        WHERE julianday(?) - julianday(c.started_at) < ?{owner_clause}
        GROUP BY t.name, weeks_ago
        """,
        [ref, ref, weeks * 7, *owner_params],
    ).fetchall()

    series: dict[str, list[int]] = {}
    for r in rows:
        wk = r["weeks_ago"]
        if wk is None or wk < 0 or wk >= weeks:
            continue
        series.setdefault(r["name"], [0] * weeks)[weeks - 1 - wk] = r["n"]
    return series


def list_tags_by_workspace(
    conn: sqlite3.Connection,
    *,
    target_kinds: tuple[str, ...] | None = None,
    prefix: str | None = None,
    workspace_filter: str | None = None,
    owner: str | None = None,
    all_tags: tuple[str, ...] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Tag counts grouped by workspace, resolved through events.

    target_kinds filters to specific event kinds (tool_call, prompt, response,
    exchange). Conversation-level tags are excluded — they are not event-backed.
    all_tags restricts the count to events whose target_id carries every
    listed tag (AND filter). Workspaces are ranked by total count descending
    and capped at `limit` (default 20; pass 0 or negative for no cap).
    """
    if owner and not has_conversation_owners_table(conn):
        return []

    where: list[str] = []
    params: list[object] = []

    if prefix:
        where.append("t.name LIKE ?")
        params.append(f"{prefix}%")
    if target_kinds:
        placeholders = ", ".join("?" * len(target_kinds))
        where.append(f"ta.target_kind IN ({placeholders})")
        params.extend(target_kinds)
    if workspace_filter:
        where.append("w.path LIKE ?")
        params.append(f"%{workspace_filter}%")
    if owner:
        where.append(owner_predicate("e.conversation_id"))
        params.append(owner)
    if all_tags:
        placeholders = ", ".join("?" * len(all_tags))
        where.append(
            "ta.target_id IN (SELECT ta2.target_id FROM tag_assignments ta2 "
            f"JOIN tags t2 ON t2.id = ta2.tag_id WHERE t2.name IN ({placeholders}) "
            "GROUP BY ta2.target_id HAVING COUNT(DISTINCT t2.name) = ?)"
        )
        params.extend(all_tags)
        params.append(len(all_tags))

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(w.path, '(no workspace)') as workspace,
            t.name as tag,
            ta.target_kind as target_kind,
            COUNT(ta.id) as count
        FROM tag_assignments ta
        JOIN tags t ON t.id = ta.tag_id
        JOIN events e ON e.id = ta.target_id
        JOIN conversations c ON c.id = e.conversation_id
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        {where_clause}
        GROUP BY w.id, t.id, ta.target_kind
        ORDER BY workspace, count DESC
        """,
        params,
    ).fetchall()

    seen: list[str] = []
    by_workspace: dict[str, list[dict]] = {}
    for row in rows:
        ws = row["workspace"]
        if ws not in by_workspace:
            seen.append(ws)
            by_workspace[ws] = []
        by_workspace[ws].append({
            "name": row["tag"],
            "count": row["count"],
            "target_kind": row["target_kind"],
        })

    result = []
    for ws in seen:
        tags = by_workspace[ws]
        result.append({
            "workspace": ws,
            "total": sum(t["count"] for t in tags),
            "tags": tags,
        })
    result.sort(key=lambda r: r["total"], reverse=True)
    if limit > 0:
        result = result[:limit]
    return result


def tag_shell_command(
    conn: sqlite3.Connection,
    tool_call_id: str,
    tool_name: str,
    input_data: dict | None,
) -> str | None:
    """Tag a shell.execute tool call with its category at ingest time.

    Args:
        conn: Database connection
        tool_call_id: The tool_call's event ULID
        tool_name: Canonical tool name (e.g., "shell.execute")
        input_data: The tool call input dict

    Returns:
        The category name if tagged, None otherwise.
    """
    from siftd.domain.shell_categories import (
        SHELL_TAG_PREFIX,
        categorize_shell_command,
    )

    if tool_name != "shell.execute":
        return None

    if not input_data:
        return None

    # Extract command
    cmd = input_data.get("command") or input_data.get("cmd") or ""
    if not cmd:
        return None

    # Categorize
    category = categorize_shell_command(cmd)
    if not category:
        return None

    # Get or create tag and apply
    tag_name = f"{SHELL_TAG_PREFIX}{category}"
    tag_id = get_or_create_tag(conn, tag_name)
    apply_tag(conn, "tool_call", tool_call_id, tag_id)

    return category


DERIVATIVE_TAG = "siftd:derivative"


def is_derivative_tool_call(tool_name: str, input_data: dict | None) -> bool:
    """Check if a tool call indicates a derivative conversation.

    Derivative conversations invoke `siftd ask`, `siftd query`, or `siftd search` —
    their content pollutes future searches with repeated search results.

    Detects two patterns:
    - shell.execute with command containing 'siftd ask', 'siftd query', or 'siftd search'
    - skill.invoke with skill='siftd' (the siftd CLI skill)
    """
    if not input_data:
        return False

    if tool_name == "shell.execute":
        cmd = input_data.get("command") or input_data.get("cmd") or ""
        return "siftd ask" in cmd or "siftd query" in cmd or "siftd search" in cmd

    if tool_name == "skill.invoke":
        skill = input_data.get("skill") or ""
        return skill == "siftd"

    return False


def tag_derivative_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
    tool_name: str,
    input_data: dict | None,
) -> bool:
    """Tag a conversation as derivative if a tool call matches.

    Called at ingest time for each tool call. Applies the conversation-level
    'siftd:derivative' tag on the first matching tool call.

    Returns True if the tag was newly applied.
    """
    if not is_derivative_tool_call(tool_name, input_data):
        return False

    tag_id = get_or_create_tag(conn, DERIVATIVE_TAG)
    result = apply_tag(conn, "conversation", conversation_id, tag_id)
    return result is not None
