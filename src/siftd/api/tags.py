"""Tag management API for siftd.

Exposes tag CRUD operations to CLI without direct storage imports.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
    get_tags_for as _get_tags_for,
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
from siftd.storage.tags import (
    tag_used_by_other_owners as _tag_used_by_other_owners,
)

__all__ = [
    "DERIVATIVE_TAG",
    "ApplyTagOutcome",
    "ApplyResult",
    "DeleteResult",
    "RenameResult",
    "TagInfo",
    "TagMutationResult",
    "apply_tags",
    "apply_tag",
    "delete_tag_safe",
    "delete_tag",
    "get_tag_id",
    "get_or_create_tag",
    "get_tags_for",
    "list_tags",
    "rename_tag_safe",
    "remove_tag",
    "rename_tag",
    "tag_info_from_dict",
    "tag_info_list_from_dict",
]

_GRANULAR_KINDS = frozenset({"prompt", "response", "tool_call", "exchange"})
_ALL_ENTITY_TYPES = frozenset({"conversation", "workspace"}) | _GRANULAR_KINDS


@dataclass
class TagInfo:
    """Tag with usage counts."""

    name: str
    description: str | None
    created_at: str
    conversation_count: int
    workspace_count: int
    tool_call_count: int
    exchange_count: int


TagMutationResult = Literal["applied", "removed", "not_found", "already_applied", "not_applied"]


@dataclass
class ApplyTagOutcome:
    """Per-tag mutation outcome."""

    tag: str
    status: TagMutationResult
    count: int


@dataclass
class ApplyResult:
    """Batch apply/remove result with enough context for CLI messaging."""

    action: Literal["apply", "remove"]
    results: list[ApplyTagOutcome]
    target_count: int
    entity_type: str
    resolved_entity_id: str | None = None


@dataclass
class RenameResult:
    """Safe rename result payload."""

    status: str
    old_name: str
    new_name: str


@dataclass
class DeleteResult:
    """Safe delete result payload."""

    status: str
    tag_name: str


def tag_info_from_dict(data: dict[str, Any]) -> TagInfo:
    """Deserialize a JSON dict into TagInfo."""
    return TagInfo(**data)


def tag_info_list_from_dict(rows: list[dict[str, Any]]) -> list[TagInfo]:
    """Deserialize a list of JSON dicts into TagInfo objects."""
    return [tag_info_from_dict(row) for row in rows]


def list_tags(
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    since: str | None = None,
    before: str | None = None,
    owner: str | None = None,
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
        rows = _list_tags(conn, since=since, before=before, owner=owner)
        return [
            TagInfo(
                name=r["name"],
                description=r["description"],
                created_at=r["created_at"],
                conversation_count=r["conversation_count"],
                workspace_count=r["workspace_count"],
                tool_call_count=r["tool_call_count"],
                exchange_count=r["exchange_count"],
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


def get_tags_for(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
) -> list:
    """Return tag rows for a given target.

    Args:
        conn: Database connection.
        target_kind: One of 'conversation', 'workspace', 'prompt', 'response', 'tool_call', 'exchange'.
        target_id: The target entity's ULID.

    Returns:
        List of Row objects with name, description, applied_at.
    """
    return _get_tags_for(conn, target_kind, target_id)


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
        entity_type: One of 'conversation', 'workspace', 'prompt', 'response', 'tool_call', 'exchange'.
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


def apply_tags(
    *,
    db_path: Path,
    tags: list[str],
    entity_type: str = "conversation",
    entity_id: str | None = None,
    last: int | None = None,
    owner: str | None = None,
    remove: bool = False,
) -> ApplyResult:
    """Apply or remove tags with shared orchestration.

    This function owns DB lifecycle and transaction boundaries.
    """
    from siftd.api.conversations import get_recent_conversation_ids, resolve_entity_id

    if entity_type not in _ALL_ENTITY_TYPES:
        raise ValueError(f"Unsupported entity_type: {entity_type!r}. Valid: {sorted(_ALL_ENTITY_TYPES)}")

    if owner and entity_type != "conversation":
        raise PermissionError("tag mutation is only supported for conversations when auth is enabled")

    if last is not None and entity_type in _GRANULAR_KINDS:
        raise ValueError("--last is only supported for conversation and workspace entity types")

    conn = _open_database(db_path)
    try:
        if last is not None:
            try:
                last_n_int = int(last)
            except (TypeError, ValueError) as e:
                raise ValueError("last must be an integer") from e
            ids = get_recent_conversation_ids(conn, last_n_int, owner=owner) if last_n_int > 0 else []
            resolved_entity_id = None
        elif entity_id:
            resolved = resolve_entity_id(conn, entity_type, entity_id, owner=owner)
            ids = [resolved] if resolved else []
            resolved_entity_id = resolved
        else:
            raise ValueError("entity_id or last required")

        if not ids:
            raise FileNotFoundError("no matching entities found")

        outcomes: list[ApplyTagOutcome] = []
        for tag_name in tags:
            if remove:
                tag_id = _get_tag_id(conn, tag_name)
                if not tag_id:
                    outcomes.append(ApplyTagOutcome(tag=tag_name, status="not_found", count=0))
                    continue

                removed_count = sum(1 for eid in ids if _remove_tag(conn, entity_type, eid, tag_id, commit=False))
                status: TagMutationResult = "removed" if removed_count else "not_applied"
                outcomes.append(ApplyTagOutcome(tag=tag_name, status=status, count=removed_count))
            else:
                tag_id = _get_or_create_tag(conn, tag_name)
                applied_count = sum(1 for eid in ids if _apply_tag(conn, entity_type, eid, tag_id, commit=False))
                status = "applied" if applied_count else "already_applied"
                outcomes.append(ApplyTagOutcome(tag=tag_name, status=status, count=applied_count))

        conn.commit()
        return ApplyResult(
            action="remove" if remove else "apply",
            results=outcomes,
            target_count=len(ids),
            entity_type=entity_type,
            resolved_entity_id=resolved_entity_id,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rename_tag_safe(
    *,
    db_path: Path,
    old_name: str,
    new_name: str,
    owner: str | None = None,
) -> RenameResult:
    """Rename a tag with owner-scope protections."""
    if not old_name or not new_name:
        raise ValueError("rename requires old_name and new_name")

    conn = _open_database(db_path)
    try:
        tag_id = _get_tag_id(conn, old_name) if owner else None
        if tag_id and _tag_used_by_other_owners(conn, tag_id, owner):
            raise PermissionError("tag is in use by another owner")

        renamed = _rename_tag(conn, old_name, new_name, commit=True)
        if not renamed:
            raise FileNotFoundError(f"Tag not found: {old_name}")

        return RenameResult(status="renamed", old_name=old_name, new_name=new_name)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_tag_safe(
    *,
    db_path: Path,
    tag_name: str,
    owner: str | None = None,
) -> DeleteResult:
    """Delete a tag with owner-scope protections."""
    if not tag_name:
        raise ValueError("delete requires tag_name")

    conn = _open_database(db_path)
    try:
        tag_id = _get_tag_id(conn, tag_name) if owner else None
        if tag_id and _tag_used_by_other_owners(conn, tag_id, owner):
            raise PermissionError("tag is in use by another owner")

        removed = _delete_tag(conn, tag_name, commit=True)
        if removed < 0:
            raise FileNotFoundError(f"Tag not found: {tag_name}")

        return DeleteResult(status="deleted", tag_name=tag_name)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def modify_conversation_tag(
    conversation_id: str,
    tag_name: str,
    *,
    action: str = "apply",
    db_path: Path | None = None,
    owner: str | None = None,
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
        resolved = resolve_entity_id(conn, "conversation", conversation_id, owner=owner)
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
