"""Receive a database file and create-or-merge into the target.

Thin wrapper around merge_database() — validates the source, handles the
first-receive case (no target yet), and delegates merging to the existing
merge infrastructure.
"""

from __future__ import annotations

import shutil
from pathlib import Path

_SQLITE_MAGIC = b"SQLite format 3\x00"


def receive_database(
    source_path: Path,
    target_db: Path,
    *,
    rebuild_fts: bool = False,
) -> dict:
    """Create or merge a source database into the target.

    Args:
        source_path: Path to the incoming database (e.g. a slice).
        target_db: Path to the target siftd database.
        rebuild_fts: Whether to rebuild the FTS5 index after merge.

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
        return _create_from_source(source_path, target_db, rebuild_fts=rebuild_fts)

    from siftd.api.merge import merge_database

    result = merge_database(target_db, source_path, rebuild_fts=rebuild_fts)
    result["status"] = "merged"
    return result


def _validate_sqlite(path: Path) -> None:
    """Check that a file starts with the SQLite magic bytes."""
    with open(path, "rb") as f:
        header = f.read(16)
    if len(header) < 16 or not header.startswith(_SQLITE_MAGIC):
        raise ValueError(f"Not a valid SQLite database: {path}")


def _create_from_source(
    source_path: Path, target_db: Path, *, rebuild_fts: bool = False,
) -> dict:
    """Copy source into place as the new target database."""
    target_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_db)

    from siftd.storage.sqlite import open_database

    conn = open_database(target_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        if rebuild_fts and count > 0:
            from siftd.storage.fts import rebuild_fts_index

            rebuild_fts_index(conn)
    finally:
        conn.close()

    return {"status": "created", "conversations": count}
