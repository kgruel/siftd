"""Live session tracking and pending tag storage.

Supports tagging conversations from within active sessions, with tags
applied at ingest time. Also supports exchange-level tagging.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from siftd.ids import ulid as _ulid


@dataclass
class PendingTag:
    """A tag queued for application at ingest time."""

    tag_name: str
    entity_type: str  # 'conversation' or 'exchange'
    exchange_index: int | None  # None for conversation, 0-based for exchange


def ensure_session_tables(conn: sqlite3.Connection) -> None:
    """Create active_sessions and pending_tags tables. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            harness_session_id TEXT PRIMARY KEY,
            adapter_name TEXT NOT NULL,
            workspace_path TEXT,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
    """)

    # Migration: add last_seen_at column if missing (for existing databases)
    cur = conn.execute("PRAGMA table_info(active_sessions)")
    columns = {row[1] for row in cur.fetchall()}
    if "last_seen_at" not in columns:
        conn.execute("ALTER TABLE active_sessions ADD COLUMN last_seen_at TEXT")
        # Initialize last_seen_at from started_at for existing rows
        conn.execute("UPDATE active_sessions SET last_seen_at = started_at WHERE last_seen_at IS NULL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_tags (
            id TEXT PRIMARY KEY,
            harness_session_id TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'conversation',
            exchange_index INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE (harness_session_id, tag_name, entity_type, exchange_index)
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pending_tags_session
        ON pending_tags(harness_session_id)
    """)

    conn.commit()


def ensure_prompt_tags_table(conn: sqlite3.Connection) -> None:
    """Create prompt_tags table for exchange-level tagging. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_tags (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            applied_at TEXT NOT NULL,
            UNIQUE (prompt_id, tag_id)
        )
    """)

    conn.commit()


def register_session(
    conn: sqlite3.Connection,
    harness_session_id: str,
    adapter_name: str,
    workspace_path: str | None = None,
    *,
    commit: bool = False,
) -> str:
    """Upsert into active_sessions. Returns harness_session_id.

    On insert: sets both started_at and last_seen_at to now.
    On update: refreshes last_seen_at (keeps original started_at).
    """
    now = datetime.now().isoformat()

    conn.execute(
        """
        INSERT INTO active_sessions (harness_session_id, adapter_name, workspace_path, started_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (harness_session_id) DO UPDATE SET
            adapter_name = excluded.adapter_name,
            workspace_path = excluded.workspace_path,
            last_seen_at = excluded.last_seen_at
        """,
        (harness_session_id, adapter_name, workspace_path, now, now),
    )

    if commit:
        conn.commit()

    return harness_session_id


def unregister_session(
    conn: sqlite3.Connection,
    harness_session_id: str,
    *,
    commit: bool = False,
) -> bool:
    """Delete from active_sessions. Returns True if existed."""
    cur = conn.execute(
        "DELETE FROM active_sessions WHERE harness_session_id = ?",
        (harness_session_id,),
    )

    if commit:
        conn.commit()

    return cur.rowcount > 0


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
    # Check for duplicate explicitly (SQLite UNIQUE doesn't handle NULL correctly)
    if exchange_index is None:
        cur = conn.execute(
            """
            SELECT 1 FROM pending_tags
            WHERE harness_session_id = ? AND tag_name = ? AND entity_type = ? AND exchange_index IS NULL
            """,
            (harness_session_id, tag_name, entity_type),
        )
    else:
        cur = conn.execute(
            """
            SELECT 1 FROM pending_tags
            WHERE harness_session_id = ? AND tag_name = ? AND entity_type = ? AND exchange_index = ?
            """,
            (harness_session_id, tag_name, entity_type, exchange_index),
        )

    if cur.fetchone():
        return None  # Duplicate

    ulid = _ulid()
    now = datetime.now().isoformat()

    conn.execute(
        """
        INSERT INTO pending_tags (id, harness_session_id, tag_name, entity_type, exchange_index, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ulid, harness_session_id, tag_name, entity_type, exchange_index, now),
    )

    if commit:
        conn.commit()

    return ulid


def get_pending_tags(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> list[PendingTag]:
    """Return list of pending tags for this session."""
    cur = conn.execute(
        """
        SELECT tag_name, entity_type, exchange_index
        FROM pending_tags
        WHERE harness_session_id = ?
        ORDER BY created_at
        """,
        (harness_session_id,),
    )

    return [
        PendingTag(
            tag_name=row["tag_name"],
            entity_type=row["entity_type"],
            exchange_index=row["exchange_index"],
        )
        for row in cur.fetchall()
    ]


def consume_pending_tags(
    conn: sqlite3.Connection,
    harness_session_id: str,
    *,
    commit: bool = False,
) -> list[PendingTag]:
    """Get and delete pending tags. Returns PendingTag list."""
    tags = get_pending_tags(conn, harness_session_id)

    conn.execute(
        "DELETE FROM pending_tags WHERE harness_session_id = ?",
        (harness_session_id,),
    )

    if commit:
        conn.commit()

    return tags


def is_session_registered(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> bool:
    """Check if session exists in active_sessions."""
    cur = conn.execute(
        "SELECT 1 FROM active_sessions WHERE harness_session_id = ?",
        (harness_session_id,),
    )
    return cur.fetchone() is not None


def get_session_info(
    conn: sqlite3.Connection,
    harness_session_id: str,
) -> dict | None:
    """Get session info from active_sessions. Returns dict or None."""
    cur = conn.execute(
        """
        SELECT harness_session_id, adapter_name, workspace_path, started_at, last_seen_at
        FROM active_sessions
        WHERE harness_session_id = ?
        """,
        (harness_session_id,),
    )
    row = cur.fetchone()
    if row:
        return {
            "harness_session_id": row["harness_session_id"],
            "adapter_name": row["adapter_name"],
            "workspace_path": row["workspace_path"],
            "started_at": row["started_at"],
            "last_seen_at": row["last_seen_at"],
        }
    return None


def cleanup_stale_sessions(
    conn: sqlite3.Connection,
    max_age_hours: int = 48,
    *,
    commit: bool = False,
) -> tuple[int, int]:
    """Delete sessions and pending tags older than max_age_hours.

    Uses last_seen_at (not started_at) to determine staleness,
    so sessions that are re-registered stay fresh.

    Returns (sessions_deleted, tags_deleted).
    """
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    sessions_deleted = 0
    tags_deleted = 0

    # Get stale session IDs (use last_seen_at, fallback to started_at for migrated rows)
    cur = conn.execute(
        "SELECT harness_session_id FROM active_sessions WHERE COALESCE(last_seen_at, started_at) < ?",
        (cutoff,),
    )
    stale_session_ids = [row["harness_session_id"] for row in cur.fetchall()]

    if stale_session_ids:
        # Delete pending tags for stale sessions
        placeholders = ",".join("?" * len(stale_session_ids))
        cur = conn.execute(
            f"DELETE FROM pending_tags WHERE harness_session_id IN ({placeholders})",
            stale_session_ids,
        )
        tags_deleted = cur.rowcount

        # Delete stale sessions
        cur = conn.execute(
            f"DELETE FROM active_sessions WHERE harness_session_id IN ({placeholders})",
            stale_session_ids,
        )
        sessions_deleted = cur.rowcount

    # Also delete orphaned pending tags (tags for sessions that were never registered)
    cur = conn.execute(
        """
        DELETE FROM pending_tags
        WHERE created_at < ?
        AND harness_session_id NOT IN (SELECT harness_session_id FROM active_sessions)
        """,
        (cutoff,),
    )
    tags_deleted += cur.rowcount

    if commit:
        conn.commit()

    return (sessions_deleted, tags_deleted)


def get_orphaned_pending_tags_count(conn: sqlite3.Connection) -> int:
    """Count pending tags for sessions not in active_sessions."""
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM pending_tags
        WHERE harness_session_id NOT IN (SELECT harness_session_id FROM active_sessions)
        """
    )
    return cur.fetchone()[0]


def get_stale_sessions_count(
    conn: sqlite3.Connection,
    max_age_hours: int = 48,
) -> int:
    """Count sessions older than max_age_hours.

    Uses last_seen_at (not started_at) to determine staleness.
    """
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    cur = conn.execute(
        "SELECT COUNT(*) FROM active_sessions WHERE COALESCE(last_seen_at, started_at) < ?",
        (cutoff,),
    )
    return cur.fetchone()[0]
