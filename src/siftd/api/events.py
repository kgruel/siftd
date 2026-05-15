"""Event detail API.

Exposes a single event (prompt, response, or tool_call) by its ULID,
with content blocks, tags, conversation context, and kind-specific data.
Lets `siftd query <event_id>` work as a peer to `siftd query <conversation_id>`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from siftd.paths import db_path as default_db_path
from siftd.storage.queries import (
    fetch_conversation_by_id_or_prefix,
    fetch_conversation_model,
)
from siftd.storage.sqlite import open_database
from siftd.storage.tags import get_tags_for

_EVENT_KINDS = ("prompt", "response", "tool_call")
_ULID_LEN = 26


@dataclass
class EventDetail:
    """A single event with content, tags, and kind-specific data."""

    id: str
    kind: str
    conversation_id: str
    parent_id: str | None
    external_id: str | None
    timestamp: str | None
    tags: list[str] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    kind_specific: dict[str, Any] = field(default_factory=dict)
    conversation: dict[str, Any] | None = None
    neighbors: dict[str, str | None] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict shape; canonical agent-facing contract.

        kind_specific is hoisted to a kind-named top-level key:
          - kind == 'response':  "response": {...}, "tool_calls": [...]
          - kind == 'tool_call': "tool_call": {...}
        Other kinds emit nothing extra.
        """
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "conversation_id": self.conversation_id,
            "parent_id": self.parent_id,
            "external_id": self.external_id,
            "timestamp": self.timestamp,
            "tags": list(self.tags),
        }
        if self.conversation is not None:
            out["conversation"] = self.conversation
        if self.neighbors is not None:
            out["neighbors"] = self.neighbors
        if self.content_blocks:
            out["content"] = list(self.content_blocks)

        if self.kind == "response":
            ks = dict(self.kind_specific)
            tool_calls = ks.pop("tool_calls", [])
            out["response"] = ks
            out["tool_calls"] = tool_calls
        elif self.kind == "tool_call":
            out["tool_call"] = dict(self.kind_specific)

        return out


def resolve_event_row(
    conn: sqlite3.Connection, event_id: str,
) -> sqlite3.Row | None:
    """Resolve a possibly-prefix event ID to its full row, or None.

    Skips prefix-LIKE entirely when the input is a full 26-char ULID.

    Known future concern: the prefix path uses fetchone() and silently returns
    the first matching row when multiple events share a prefix. This mirrors
    the conversation-resolver bug killed in the id-resolution refactor. At
    current DB scale event-level 8-char collisions are rare (random ULID
    portion), but rerun a prefix-collision census on the events table when
    the DB exceeds ~50k events and extend AmbiguousPrefix detection here if
    collisions are confirmed.
    """
    if len(event_id) == _ULID_LEN:
        return conn.execute(
            "SELECT id, kind, conversation_id, parent_id, external_id, timestamp"
            " FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    return conn.execute(
        "SELECT id, kind, conversation_id, parent_id, external_id, timestamp"
        " FROM events WHERE id LIKE ? ORDER BY id LIMIT 1",
        (f"{event_id}%",),
    ).fetchone()


def _fetch_content_blocks(conn: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT block_type, content, block_index"
        " FROM event_content WHERE event_id = ?"
        " ORDER BY block_index",
        (event_id,),
    ).fetchall()
    return [
        {
            "block_type": row["block_type"],
            "content": row["content"],
            "block_index": row["block_index"],
        }
        for row in rows
    ]


def _fetch_response_kind_specific(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT er.input_tokens, er.output_tokens,"
        " m.name AS model_name, m.raw_name AS model_raw_name,"
        " p.name AS provider_name"
        " FROM event_response er"
        " LEFT JOIN models m ON m.id = er.model_id"
        " LEFT JOIN providers p ON p.id = er.provider_id"
        " WHERE er.event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "model": row["model_name"] or row["model_raw_name"],
        "provider": row["provider_name"],
    }


def _fetch_response_child_tool_calls(
    conn: sqlite3.Connection, response_id: str,
) -> list[dict[str, Any]]:
    """Return summary tool_call children for a response event."""
    rows = conn.execute(
        "SELECT e.id, e.external_id, e.timestamp, t.name AS tool_name, etc.status"
        " FROM events e"
        " JOIN event_tool_call etc ON etc.event_id = e.id"
        " LEFT JOIN tools t ON t.id = etc.tool_id"
        " WHERE e.parent_id = ? AND e.kind = 'tool_call'"
        " ORDER BY e.timestamp, e.id",
        (response_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "external_id": row["external_id"],
            "timestamp": row["timestamp"],
            "tool_name": row["tool_name"],
            "status": row["status"],
        }
        for row in rows
    ]


def _fetch_tool_call_kind_specific(
    conn: sqlite3.Connection, event_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT etc.input, etc.status, etc.result_hash,"
        " t.name AS tool_name,"
        " cb.content AS result"
        " FROM event_tool_call etc"
        " LEFT JOIN tools t ON t.id = etc.tool_id"
        " LEFT JOIN content_blobs cb ON cb.hash = etc.result_hash"
        " WHERE etc.event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "tool_name": row["tool_name"],
        "status": row["status"],
        "input": row["input"],
        "result": row["result"],
    }


def _fetch_conversation_summary(
    conn: sqlite3.Connection, conversation_id: str,
) -> dict[str, Any] | None:
    """Minimal conversation context for an event.

    Reuses fetch_conversation_by_id_or_prefix and fetch_conversation_model
    so the SQL stays in one place and tracks any future schema changes.
    """
    base = fetch_conversation_by_id_or_prefix(conn, conversation_id)
    if base is None:
        return None
    return {
        "id": base["id"],
        "started_at": base["started_at"],
        "workspace": base["workspace"],
        "model": fetch_conversation_model(conn, conversation_id),
    }


def _fetch_event_tags(conn: sqlite3.Connection, event_id: str, kind: str) -> list[str]:
    """Tags assigned to this event.

    Prompts also surface 'exchange'-kind tags (same target_id, different kind);
    fetched in a single query covering both target_kinds.
    """
    if kind == "prompt":
        rows = conn.execute(
            "SELECT DISTINCT t.name FROM tag_assignments ta"
            " JOIN tags t ON t.id = ta.tag_id"
            " WHERE ta.target_id = ? AND ta.target_kind IN ('prompt', 'exchange')"
            " ORDER BY t.name",
            (event_id,),
        ).fetchall()
        return [row["name"] for row in rows]
    return [row["name"] for row in get_tags_for(conn, kind, event_id)]


def _fetch_neighbors(
    conn: sqlite3.Connection, event_id: str, kind: str, conversation_id: str,
    timestamp: str | None,
) -> dict[str, str | None]:
    """Return prev/next event of the same kind by (timestamp, id) order."""
    if timestamp is None:
        return {"prev_event_id": None, "next_event_id": None}

    prev_row = conn.execute(
        "SELECT id FROM events"
        " WHERE conversation_id = ? AND kind = ?"
        "   AND (timestamp < ? OR (timestamp = ? AND id < ?))"
        " ORDER BY timestamp DESC, id DESC LIMIT 1",
        (conversation_id, kind, timestamp, timestamp, event_id),
    ).fetchone()
    next_row = conn.execute(
        "SELECT id FROM events"
        " WHERE conversation_id = ? AND kind = ?"
        "   AND (timestamp > ? OR (timestamp = ? AND id > ?))"
        " ORDER BY timestamp ASC, id ASC LIMIT 1",
        (conversation_id, kind, timestamp, timestamp, event_id),
    ).fetchone()
    return {
        "prev_event_id": prev_row["id"] if prev_row else None,
        "next_event_id": next_row["id"] if next_row else None,
    }


def _kind_specific_for(conn: sqlite3.Connection, event_id: str, kind: str) -> dict[str, Any]:
    if kind == "response":
        ks = _fetch_response_kind_specific(conn, event_id)
        ks["tool_calls"] = _fetch_response_child_tool_calls(conn, event_id)
        return ks
    if kind == "tool_call":
        return _fetch_tool_call_kind_specific(conn, event_id)
    return {}


def get_event(
    id: str,
    *,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    include_content: bool = True,
    include_neighbors: bool = False,
) -> EventDetail | None:
    """Get a single event by ID (full or prefix).

    Args:
        id: Event ULID, or a prefix of one.
        db_path: Path to database. Uses default if not specified.
        conn: Optional existing read-only connection. Caller retains ownership;
            if provided, db_path is ignored. Useful to avoid a second open
            after a smart-route probe.
        include_content: Include `content_blocks`. Default True.
        include_neighbors: Include `neighbors` (opt-in for cost). Default False.

    Returns:
        EventDetail or None if no event matches.

    Raises:
        FileNotFoundError: If the database does not exist.
    """
    if conn is None:
        db = db_path or default_db_path()
        if not db.exists():
            raise FileNotFoundError(f"Database not found: {db}")
        owned_conn = open_database(db, read_only=True)
    else:
        owned_conn = None

    work_conn = conn or owned_conn
    assert work_conn is not None  # mypy: one of the two is set
    try:
        row = resolve_event_row(work_conn, id)
        if row is None or row["kind"] not in _EVENT_KINDS:
            return None

        event_id = row["id"]
        kind = row["kind"]
        content_blocks = _fetch_content_blocks(work_conn, event_id) if include_content else []
        tags = _fetch_event_tags(work_conn, event_id, kind)
        kind_specific = _kind_specific_for(work_conn, event_id, kind)
        conv_summary = _fetch_conversation_summary(work_conn, row["conversation_id"])
        neighbors = (
            _fetch_neighbors(
                work_conn, event_id, kind, row["conversation_id"], row["timestamp"],
            )
            if include_neighbors
            else None
        )

        return EventDetail(
            id=event_id,
            kind=kind,
            conversation_id=row["conversation_id"],
            parent_id=row["parent_id"],
            external_id=row["external_id"],
            timestamp=row["timestamp"],
            tags=tags,
            content_blocks=content_blocks,
            kind_specific=kind_specific,
            conversation=conv_summary,
            neighbors=neighbors,
        )
    finally:
        if owned_conn is not None:
            owned_conn.close()
