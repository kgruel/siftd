"""Read/write operations for the polymorphic attributes table (schema v4)."""

import sqlite3

from siftd.ids import ulid as _ulid


def set_attribute(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
    key: str,
    value: str,
    *,
    scope: str | None = None,
    commit: bool = False,
) -> None:
    """Write a key/value attribute for any target entity. Upserts on conflict.

    target_kind: 'conversation' | 'prompt' | 'response' | 'tool_call'
    target_id: ULID of the target entity
    """
    conn.execute(
        """INSERT INTO attributes (id, target_kind, target_id, key, value, scope)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (target_kind, target_id, key, scope) DO UPDATE SET value = excluded.value""",
        (_ulid(), target_kind, target_id, key, value, scope),
    )
    if commit:
        conn.commit()


def get_attributes(
    conn: sqlite3.Connection,
    target_kind: str,
    target_id: str,
) -> list[sqlite3.Row]:
    """Return all attribute rows for a target entity."""
    return conn.execute(
        "SELECT * FROM attributes WHERE target_kind = ? AND target_id = ?",
        (target_kind, target_id),
    ).fetchall()
