"""Tag management API for siftd.

Exposes tag CRUD operations to CLI without direct storage imports.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from siftd.paths import db_path as _db_path
from siftd.storage.sqlite import open_database as _open_database
from siftd.storage.tags import DERIVATIVE_TAG
from siftd.storage.tags import (
    apply_tag as _apply_tag,
)
from siftd.storage.tags import (
    delete_tag as _delete_tag,
)
from siftd.storage.tags import (
    get_or_create_tag as _get_or_create_tag,
)
from siftd.storage.tags import (
    get_tag_id as _get_tag_id,
)
from siftd.storage.tags import (
    list_tags as _list_tags,
)
from siftd.storage.tags import (
    remove_tag as _remove_tag,
)
from siftd.storage.tags import (
    rename_tag as _rename_tag,
)

__all__ = [
    "DERIVATIVE_TAG",
    "TagInfo",
    "apply_tag",
    "delete_tag",
    "get_tag_id",
    "get_or_create_tag",
    "list_tags",
    "remove_tag",
    "rename_tag",
]


@dataclass
class TagInfo:
    """Tag with usage counts."""

    name: str
    description: str | None
    created_at: str
    conversation_count: int
    workspace_count: int
    tool_call_count: int
    prompt_count: int


def list_tags(
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    since: str | None = None,
    before: str | None = None,
) -> list[TagInfo]:
    """List all tags with usage counts.

    Args:
        db_path: Path to database. Ignored if conn provided.
        conn: Existing connection to use.
        since: Only count associations where conversation started after this ISO date.
        before: Only count associations where conversation started before this ISO date.

    Returns:
        List of TagInfo objects sorted by name.
    """
    should_close = False
    if conn is None:
        path = db_path or _db_path()
        conn = _open_database(path, read_only=True)
        should_close = True

    try:
        rows = _list_tags(conn, since=since, before=before)
        return [
            TagInfo(
                name=r["name"],
                description=r["description"],
                created_at=r["created_at"],
                conversation_count=r["conversation_count"],
                workspace_count=r["workspace_count"],
                tool_call_count=r["tool_call_count"],
                prompt_count=r["prompt_count"],
            )
            for r in rows
        ]
    finally:
        if should_close:
            conn.close()


def get_or_create_tag(
    conn: sqlite3.Connection,
    name: str,
    description: str | None = None,
) -> str:
    """Get or create a tag by name.

    Args:
        conn: Database connection.
        name: Tag name.
        description: Optional tag description.

    Returns:
        Tag ID (ULID).
    """
    return _get_or_create_tag(conn, name, description)


def get_tag_id(
    conn: sqlite3.Connection,
    name: str,
) -> str | None:
    """Return tag id for name, or None if not found."""
    return _get_tag_id(conn, name)


def apply_tag(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> str | None:
    """Apply a tag to an entity.

    Args:
        conn: Database connection.
        entity_type: One of 'conversation', 'workspace', 'tool_call'.
        entity_id: The entity's ULID.
        tag_id: The tag's ULID.
        commit: Whether to commit the transaction.

    Returns:
        Assignment ID if newly applied, None if already applied.
    """
    return _apply_tag(conn, entity_type, entity_id, tag_id, commit=commit)


def remove_tag(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    tag_id: str,
    *,
    commit: bool = False,
) -> bool:
    """Remove a tag from an entity.

    Args:
        conn: Database connection.
        entity_type: One of 'conversation', 'workspace', 'tool_call'.
        entity_id: The entity's ULID.
        tag_id: The tag's ULID.
        commit: Whether to commit the transaction.

    Returns:
        True if removed, False if not applied.
    """
    return _remove_tag(conn, entity_type, entity_id, tag_id, commit=commit)


def modify_conversation_tag(
    conversation_id: str,
    tag_name: str,
    *,
    action: str = "apply",
    db_path: Path | None = None,
) -> list[str]:
    """Apply or remove a tag on a conversation, returning the updated tag list.

    Manages its own connection. Resolves the conversation ID (prefix match).

    Args:
        conversation_id: Full or prefix conversation ID.
        tag_name: Tag name to apply or remove.
        action: "apply" or "remove".
        db_path: Database path. Uses default if not provided.

    Returns:
        Updated list of tag names on the conversation.
    """
    from siftd.api.conversations import resolve_entity_id
    from siftd.storage.queries import fetch_conversation_tags

    path = db_path or _db_path()
    conn = _open_database(path)
    try:
        resolved = resolve_entity_id(conn, "conversation", conversation_id)
        if not resolved:
            return []

        if action == "remove":
            tid = _get_tag_id(conn, tag_name)
            if tid:
                _remove_tag(conn, "conversation", resolved, tid)
        else:
            tid = _get_or_create_tag(conn, tag_name)
            _apply_tag(conn, "conversation", resolved, tid)

        conn.commit()
        return fetch_conversation_tags(conn, resolved)
    finally:
        conn.close()


def rename_tag(
    old_name: str = "",
    new_name: str = "",
    *,
    conn: sqlite3.Connection | None = None,
    db_path: Path | None = None,
    commit: bool = False,
) -> bool:
    """Rename a tag.

    Args:
        old_name: Current tag name.
        new_name: New tag name.
        conn: Database connection. Opened from db_path if not provided.
        db_path: Path to database. Ignored if conn provided.
        commit: Whether to commit the transaction.

    Returns:
        True if renamed, False if old_name not found.

    Raises:
        ValueError: If new_name already exists.
    """
    should_close = False
    if conn is None:
        db = db_path or _db_path()
        conn = _open_database(db)
        should_close = True
        commit = True  # auto-commit when we own the connection
    try:
        return _rename_tag(conn, old_name, new_name, commit=commit)
    finally:
        if should_close:
            conn.close()


def delete_tag(
    conn: sqlite3.Connection,
    name: str,
    *,
    commit: bool = False,
) -> int:
    """Delete a tag and all its associations.

    Args:
        conn: Database connection.
        name: Tag name to delete.
        commit: Whether to commit the transaction.

    Returns:
        Count of entity associations removed, or -1 if tag not found.
    """
    return _delete_tag(conn, name, commit=commit)
