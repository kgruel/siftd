"""Event detail API.

Exposes a single event (prompt, response, or tool_call) by its ULID,
with content blocks, tags, conversation context, and kind-specific data.

Phase 4 of the event-ergonomics plan: lets `siftd query <event_id>` work
as a peer to `siftd query <conversation_id>`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from siftd.paths import db_path as default_db_path
from siftd.storage.sqlite import open_database
from siftd.storage.tags import get_tags_for

_EVENT_KINDS = ("prompt", "response", "tool_call")


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


def _resolve_event_id(conn: sqlite3.Connection, event_id: str) -> tuple[str, str] | None:
    """Resolve a possibly-prefix event ID to (id, kind), or None.

    Tries exact match first, then prefix. Ambiguous prefixes resolve to the
    first match by ULID order — callers should pass enough characters to
    disambiguate (12+ recommended).
    """
    row = conn.execute(
        "SELECT id, kind FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row:
        return (row["id"], row["kind"])
    row = conn.execute(
        "SELECT id, kind FROM events WHERE id LIKE ? ORDER BY id LIMIT 1",
        (f"{event_id}%",),
    ).fetchone()
    if row:
        return (row["id"], row["kind"])
    return None


def _fetch_event_row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, kind, conversation_id, parent_id, external_id, timestamp"
        " FROM events WHERE id = ?",
        (event_id,),
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
    row = conn.execute(
        "SELECT c.id, c.started_at, w.path AS workspace_path"
        " FROM conversations c"
        " LEFT JOIN workspaces w ON w.id = c.workspace_id"
        " WHERE c.id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    # Pick a model — same heuristic as get_conversation: most-frequent response model.
    model_row = conn.execute(
        "SELECT m.name AS name, COUNT(*) AS n"
        " FROM events e"
        " JOIN event_response er ON er.event_id = e.id"
        " LEFT JOIN models m ON m.id = er.model_id"
        " WHERE e.conversation_id = ? AND e.kind = 'response' AND m.name IS NOT NULL"
        " GROUP BY m.name ORDER BY n DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "workspace": row["workspace_path"],
        "model": model_row["name"] if model_row else None,
    }


def _fetch_event_tags(conn: sqlite3.Connection, event_id: str, kind: str) -> list[str]:
    """Tags assigned to this event, across both event-kind and exchange (prompt anchor)."""
    rows = get_tags_for(conn, kind, event_id)
    names = [row["name"] for row in rows]
    # Prompts also surface 'exchange'-kind tags (same target_id, different kind)
    if kind == "prompt":
        ex_rows = get_tags_for(conn, "exchange", event_id)
        names.extend(row["name"] for row in ex_rows if row["name"] not in names)
    return names


def _fetch_neighbors(
    conn: sqlite3.Connection, event_id: str, kind: str, conversation_id: str,
) -> dict[str, str | None]:
    """Return prev/next event of the same kind in the conversation by timestamp+id."""
    base = (
        "SELECT timestamp, id FROM events"
        " WHERE conversation_id = ? AND kind = ? AND id = ?"
    )
    cur = conn.execute(base, (conversation_id, kind, event_id)).fetchone()
    if cur is None:
        return {"prev_event_id": None, "next_event_id": None}
    ts, eid = cur["timestamp"], cur["id"]

    prev_row = conn.execute(
        "SELECT id FROM events"
        " WHERE conversation_id = ? AND kind = ?"
        "   AND (timestamp < ? OR (timestamp = ? AND id < ?))"
        " ORDER BY timestamp DESC, id DESC LIMIT 1",
        (conversation_id, kind, ts, ts, eid),
    ).fetchone()
    next_row = conn.execute(
        "SELECT id FROM events"
        " WHERE conversation_id = ? AND kind = ?"
        "   AND (timestamp > ? OR (timestamp = ? AND id > ?))"
        " ORDER BY timestamp ASC, id ASC LIMIT 1",
        (conversation_id, kind, ts, ts, eid),
    ).fetchone()
    return {
        "prev_event_id": prev_row["id"] if prev_row else None,
        "next_event_id": next_row["id"] if next_row else None,
    }


def get_event(
    id: str,
    *,
    db_path: Path | None = None,
    include_content: bool = True,
    include_neighbors: bool = False,
) -> EventDetail | None:
    """Get a single event by ID (full or prefix).

    Args:
        id: Event ULID, or a prefix of one.
        db_path: Path to database. Uses default if not specified.
        include_content: Include `content_blocks`. Default True.
        include_neighbors: Include `neighbors` (opt-in for cost). Default False.

    Returns:
        EventDetail or None if no event matches.

    Raises:
        FileNotFoundError: If the database does not exist.
    """
    db = db_path or default_db_path()
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)
    try:
        resolved = _resolve_event_id(conn, id)
        if resolved is None:
            return None
        event_id, kind = resolved
        if kind not in _EVENT_KINDS:
            return None

        row = _fetch_event_row(conn, event_id)
        if row is None:
            return None

        content_blocks = _fetch_content_blocks(conn, event_id) if include_content else []
        tags = _fetch_event_tags(conn, event_id, kind)
        kind_specific = _kind_specific_for(conn, event_id, kind)
        conv_summary = _fetch_conversation_summary(conn, row["conversation_id"])
        neighbors = (
            _fetch_neighbors(conn, event_id, kind, row["conversation_id"])
            if include_neighbors
            else None
        )

        return EventDetail(
            id=row["id"],
            kind=row["kind"],
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
        conn.close()


def _kind_specific_for(conn: sqlite3.Connection, event_id: str, kind: str) -> dict[str, Any]:
    if kind == "response":
        ks = _fetch_response_kind_specific(conn, event_id)
        ks["tool_calls"] = _fetch_response_child_tool_calls(conn, event_id)
        return ks
    if kind == "tool_call":
        return _fetch_tool_call_kind_specific(conn, event_id)
    return {}


def get_event_neighbors(
    id: str, *, db_path: Path | None = None,
) -> dict[str, str | None] | None:
    """Standalone neighbors lookup. Returns None if the event is not found."""
    db = db_path or default_db_path()
    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")
    conn = open_database(db, read_only=True)
    try:
        resolved = _resolve_event_id(conn, id)
        if resolved is None:
            return None
        event_id, kind = resolved
        row = _fetch_event_row(conn, event_id)
        if row is None:
            return None
        return _fetch_neighbors(conn, event_id, kind, row["conversation_id"])
    finally:
        conn.close()
