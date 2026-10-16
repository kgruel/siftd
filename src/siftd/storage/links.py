"""Conversation lineage edges: write parents, resolve parents and children at read time (schema v13).

A conversation has at most one parent per relation: where it ran (``subagent``)
and what it continues (``fork``, ``resume``, ``branch``), so a forked subagent
has two edges, keyed ``(conversation_id, kind)``. The edge stores the parent's ``external_id``, never its conversation
id, and the readers here join it to ``conversations.external_id`` within the
child's harness. Re-ingesting the parent replaces its row under a new ULID, so
a stored id would dangle on every replacement; the external id survives it. A
parent that was never ingested leaves the edge *dangling*: ``parent_id`` is
None, and nothing is an error.
"""

import sqlite3
from collections.abc import Sequence
from typing import NamedTuple

from siftd.domain import Lineage
from siftd.domain.models import CONTAINMENT_KINDS
from siftd.storage.sql_helpers import batched_in_query, has_conversation_owners_table, owner_predicate


class ConversationLink(NamedTuple):
    """One conversation's parent edge, with the parent resolved if it is stored."""

    conversation_id: str
    kind: str
    parent_external_id: str
    anchor: str | None
    parent_id: str | None  # None: the parent was never ingested (a dangling edge)


class ChildLink(NamedTuple):
    """A child conversation and the edge that makes it one."""

    conversation_id: str
    parent_id: str
    kind: str
    anchor: str | None


def insert_conversation_link(
    conn: sqlite3.Connection,
    conversation_id: str,
    lineage: Lineage,
) -> None:
    """Record one parent edge of ``conversation_id``. Caller controls the commit."""
    conn.execute(
        "INSERT INTO conversation_links (conversation_id, kind, parent_external_id, anchor)"
        " VALUES (?, ?, ?, ?)",
        (conversation_id, lineage.kind, lineage.parent, lineage.anchor),
    )


def get_conversation_links(
    conn: sqlite3.Connection,
    conversation_ids: Sequence[str],
) -> dict[str, list[ConversationLink]]:
    """Parent edges for the given conversations, keyed by child id, containment first.

    Conversations with no edge are absent. The parent resolves by joining the
    child's harness and the edge's ``parent_external_id`` against
    ``conversations`` (``UNIQUE (harness_id, external_id)``, so at most one row).
    """
    rows = batched_in_query(
        conn,
        "SELECT l.conversation_id, l.kind, l.parent_external_id, l.anchor, p.id AS parent_id"
        " FROM conversation_links l"
        " JOIN conversations c ON c.id = l.conversation_id"
        " LEFT JOIN conversations p"
        "   ON p.harness_id = c.harness_id AND p.external_id = l.parent_external_id"
        " WHERE l.conversation_id IN ({placeholders})",
        conversation_ids,
    )
    links: dict[str, list[ConversationLink]] = {}
    for row in rows:
        links.setdefault(row["conversation_id"], []).append(ConversationLink(*tuple(row)))
    for edges in links.values():
        edges.sort(key=lambda e: e.kind not in CONTAINMENT_KINDS)
    return links


def get_conversation_parent(
    conn: sqlite3.Connection,
    conversation_id: str,
    kind: str | None = None,
) -> ConversationLink | None:
    """One parent edge of ``conversation_id``: of ``kind`` if given, else where it ran
    (its containment edge) and failing that what it continues. None if it has none."""
    edges = get_conversation_links(conn, [conversation_id]).get(conversation_id, [])
    if kind is not None:
        edges = [e for e in edges if e.kind == kind]
    return edges[0] if edges else None


def subagent_parent_sql(child: str = "c") -> str:
    """SQL for the external_id of ``child``'s sub-agent parent, NULL for a top-level one.

    The edge is authoritative: a conversation with ANY conversation_links row is a
    sub-agent only if that edge is kind ``subagent``, and its parent is the edge's.
    The ``<root>::agent::<id>`` spelling is read only for a row with NO edge: schema
    v13 did not backfill edges, so rows ingested before it still carry only the
    marker. The fallback's removal is LEGACY_AGENT_EDGE_REMOVAL in api/compat.py.
    Every reader of sub-agent parentage derives it from this expression.
    """
    return f"""(CASE
    WHEN EXISTS (SELECT 1 FROM conversation_links l WHERE l.conversation_id = {child}.id)
        THEN (SELECT l.parent_external_id FROM conversation_links l
              WHERE l.conversation_id = {child}.id AND l.kind = 'subagent')
    WHEN instr({child}.external_id, '::agent::') > 0
        THEN substr({child}.external_id, 1, instr({child}.external_id, '::agent::') - 1)
END)"""


def get_subagent_children(
    conn: sqlite3.Connection,
    parent_ids: Sequence[str],
    *,
    owner: str | None = None,
) -> list[str]:
    """Ids of the sub-agents directly below ``parent_ids``, oldest first.

    The sub-agent counterpart of get_conversation_children: it resolves through
    subagent_parent_sql, so a row with no edge is a child exactly when the
    listing's root selection calls it one. ``owner`` scopes the children as in
    get_conversation_children.
    """
    if not parent_ids:
        return []
    if owner and not has_conversation_owners_table(conn):
        return []
    where: list[str] = []
    prefix: list[str] = []
    if owner:
        where.append(owner_predicate("c.id"))
        prefix.append(owner)
    where.append("p.id IN ({placeholders})")
    # CROSS JOIN pins the child as the outer loop, so each child finds its parent
    # through the (harness_id, external_id) index. With a plain JOIN the planner
    # drives from the parent ids and scans every child per parent (~100x slower).
    sql = (
        "SELECT c.id AS conversation_id FROM conversations c"
        " CROSS JOIN conversations p"
        f"   ON p.harness_id = c.harness_id AND p.external_id = {subagent_parent_sql('c')}"
        " WHERE " + " AND ".join(where) + " ORDER BY c.started_at, c.id"
    )
    rows = batched_in_query(conn, sql, parent_ids, prefix_params=tuple(prefix))
    return [row["conversation_id"] for row in rows]


def get_conversation_children(
    conn: sqlite3.Connection,
    parent_ids: Sequence[str],
    *,
    kind: str | None = None,
    owner: str | None = None,
) -> list[ChildLink]:
    """Conversations whose edge resolves to one of ``parent_ids``, oldest child first.

    ``kind`` narrows to one edge kind. ``owner`` scopes the children to that
    user's conversations, as every other owner-scoped read does: a visible
    parent does not grant its children. An owner scope on a database with no
    ownership table yields nothing, matching those reads.
    """
    if owner and not has_conversation_owners_table(conn):
        return []
    where: list[str] = []
    prefix: list[str] = []
    if kind is not None:
        where.append("l.kind = ?")
        prefix.append(kind)
    if owner:
        where.append(owner_predicate("c.id"))
        prefix.append(owner)
    # The id batch is the last predicate, so batched_in_query appends it after
    # the prefix parameters in statement order.
    where.append("p.id IN ({placeholders})")
    sql = (
        "SELECT c.id AS conversation_id, p.id AS parent_id, l.kind, l.anchor"
        " FROM conversation_links l"
        " JOIN conversations c ON c.id = l.conversation_id"
        " JOIN conversations p"
        "   ON p.harness_id = c.harness_id AND p.external_id = l.parent_external_id"
        " WHERE " + " AND ".join(where) + " ORDER BY c.started_at, c.id"
    )
    rows = batched_in_query(conn, sql, parent_ids, prefix_params=tuple(prefix))
    return [ChildLink(*tuple(row)) for row in rows]
