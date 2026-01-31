"""Session management API for siftd.

Exposes live session registration and pending tag operations to CLI.
"""

import sqlite3

from siftd.storage.sessions import (
    cleanup_stale_sessions as _cleanup_stale_sessions,
)
from siftd.storage.sessions import (
    is_session_registered as _is_session_registered,
)
from siftd.storage.sessions import (
    queue_tag as _queue_tag,
)
from siftd.storage.sessions import (
    register_session as _register_session,
)

__all__ = [
    "cleanup_stale_sessions",
    "is_session_registered",
    "queue_tag",
    "register_session",
]


def register_session(
    conn: sqlite3.Connection,
    harness_session_id: str,
    adapter_name: str,
    workspace_path: str | None = None,
    *,
    commit: bool = False,
) -> str:
    """Upsert into active_sessions. Returns harness_session_id."""
    return _register_session(conn, harness_session_id, adapter_name, workspace_path, commit=commit)


def queue_tag(
    conn: sqlite3.Connection,
    harness_session_id: str,
    tag_name: str,
    *,
    entity_type: str = "conversation",
    exchange_index: int | None = None,
    commit: bool = False,
) -> str | None:
    """Insert into pending_tags. Returns ULID or None if duplicate."""
    return _queue_tag(
        conn,
        harness_session_id,
        tag_name,
        entity_type=entity_type,
        exchange_index=exchange_index,
        commit=commit,
    )


def is_session_registered(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> bool:
    """Check if session exists in active_sessions."""
    return _is_session_registered(conn, harness_session_id)


def cleanup_stale_sessions(
    conn: sqlite3.Connection,
    max_age_hours: int = 48,
    *,
    commit: bool = False,
) -> tuple[int, int]:
    """Delete sessions and pending tags older than max_age_hours.

    Returns (sessions_deleted, tags_deleted).
    """
    return _cleanup_stale_sessions(conn, max_age_hours, commit=commit)
