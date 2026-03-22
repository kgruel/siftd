"""Receive a database file and create-or-merge into the target.

Thin wrapper around merge_database() — validates the source, handles the
first-receive case (no target yet), and delegates merging to the existing
merge infrastructure.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

_SQLITE_MAGIC = b"SQLite format 3\x00"


def receive_database(
    source_path: Path,
    target_db: Path,
    *,
    rebuild_fts: bool = False,
    user_id: str | None = None,
    push_id: str | None = None,
) -> dict:
    """Create or merge a source database into the target.

    Args:
        source_path: Path to the incoming database (e.g. a slice).
        target_db: Path to the target siftd database.
        rebuild_fts: Whether to rebuild the FTS5 index after merge.
        user_id: Authenticated user identity to stamp as conversation owner.
        push_id: Push log ID for provenance linking.

    Returns:
        Dict with ``status`` ("created" or "merged") and merge stats.

    Raises:
        ValueError: If source is not a valid SQLite database.
        FileNotFoundError: If source does not exist.
    """
    if not source_path.exists():
        raise FileNotFoundError(f"Source not found: {source_path}")

    _validate_sqlite(source_path)

    if not target_db.exists():
        result = _create_from_source(source_path, target_db)
        if user_id:
            conv_ids = _all_conversation_ids(target_db)
            _stamp_ownership(target_db, conv_ids, user_id, push_id)
            result["owned"] = len(conv_ids)
        return result

    from siftd.api.merge import merge_database

    result = merge_database(target_db, source_path, rebuild_fts=rebuild_fts)
    result["status"] = "merged"

    if user_id:
        # new_conversation_ids is a superset of replaced_conversation_ids
        # (replacements delete the stale target before the snapshot, so the
        # source rows appear as new in the diff).  No need to concatenate.
        owned_ids = result.get("new_conversation_ids", [])
        _stamp_ownership(target_db, owned_ids, user_id, push_id)
        result["owned"] = len(owned_ids)

    return result


def _validate_sqlite(path: Path) -> None:
    """Check that a file starts with the SQLite magic bytes."""
    with open(path, "rb") as f:
        header = f.read(16)
    if len(header) < 16 or not header.startswith(_SQLITE_MAGIC):
        raise ValueError(f"Not a valid SQLite database: {path}")


def _create_from_source(source_path: Path, target_db: Path) -> dict:
    """Move source into place as the new target database."""
    target_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_db)

    from siftd.storage.sqlite import open_database

    conn = open_database(target_db, read_only=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    finally:
        conn.close()

    return {"status": "created", "conversations": count}


def _all_conversation_ids(db_path: Path) -> list[str]:
    """Return all conversation IDs from a database."""
    from siftd.storage.sqlite import open_database

    conn = open_database(db_path, read_only=True)
    try:
        rows = conn.execute("SELECT id FROM conversations").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _stamp_ownership(
    db_path: Path,
    conversation_ids: list[str],
    user_id: str,
    push_id: str | None = None,
) -> None:
    """Insert or replace ownership for given conversation IDs."""
    if not conversation_ids:
        return

    from siftd.storage.sqlite import open_database

    conn = open_database(db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.executemany(
            "INSERT OR REPLACE INTO conversation_owners "
            "(conversation_id, user_id, push_id, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            [(cid, user_id, push_id, now) for cid in conversation_ids],
        )
        conn.commit()
    finally:
        conn.close()
