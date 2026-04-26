"""Migration script for existing tool_calls.result data to content_blobs.

Migrates inline result content to deduplicated blob storage in batches.
Designed to be run offline or as a background task.

Uses trigger-based ref_count management: blobs are inserted with ref_count=0,
and the AFTER UPDATE trigger on tool_calls.result_hash increments ref_count
when the hash is set.  This is safe under concurrent execution — if two
migrators process the same row, only the first UPDATE changes result_hash
from NULL, so the trigger fires exactly once.

Usage:
    from siftd.storage.migrate_blobs import migrate_existing_results
    migrate_existing_results(conn, batch_size=1000, on_progress=print)
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from siftd.storage.blobs import BlobCollisionError, compute_content_hash


def count_pending_migrations(conn: sqlite3.Connection) -> dict:
    """Count rows that need migration.

    Returns:
        dict with:
            - total: number of tool_calls with inline result
            - unique: estimated unique content (based on hash)
            - size_bytes: total bytes of result content
    """
    cur = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(LENGTH(result)) as size_bytes
        FROM tool_calls
        WHERE result IS NOT NULL AND result_hash IS NULL
    """)
    row = cur.fetchone()
    total = row[0] or 0
    size_bytes = row[1] or 0

    # Count unique values (sample-based for large tables)
    if total > 10000:
        # For large tables, sample to estimate uniqueness
        cur = conn.execute("""
            SELECT COUNT(DISTINCT result) as unique_count
            FROM (SELECT result FROM tool_calls WHERE result IS NOT NULL LIMIT 10000)
        """)
        unique = cur.fetchone()[0]
        # Extrapolate (rough estimate)
        unique = int(unique * (total / 10000))
    else:
        cur = conn.execute("""
            SELECT COUNT(DISTINCT result) as unique_count
            FROM tool_calls
            WHERE result IS NOT NULL AND result_hash IS NULL
        """)
        unique = cur.fetchone()[0] or 0

    return {
        "total": total,
        "unique": unique,
        "size_bytes": size_bytes,
    }


def migrate_existing_results(
    conn: sqlite3.Connection,
    *,
    batch_size: int = 1000,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Migrate existing tool_calls.result to content_blobs.

    Blobs are inserted with ref_count=0. The AFTER UPDATE trigger on
    tool_calls.result_hash handles ref_count bookkeeping, ensuring
    exactly-once increment even under concurrent execution.

    Raises BlobCollisionError if two distinct content values share a hash.

    Args:
        conn: Database connection
        batch_size: Number of rows to process per batch
        on_progress: Optional callback(processed, total) for progress updates

    Returns:
        dict with:
            - migrated: number of tool_calls migrated
            - blobs_created: number of unique blobs created
            - blobs_reused: number of times existing blobs were reused
            - bytes_before: total bytes of inline results
            - bytes_after: total bytes in blob storage
    """
    stats = {
        "migrated": 0,
        "blobs_created": 0,
        "blobs_reused": 0,
        "bytes_before": 0,
        "bytes_after": 0,
    }

    # Count total for progress reporting
    cur = conn.execute("""
        SELECT COUNT(*) FROM tool_calls
        WHERE result IS NOT NULL AND result_hash IS NULL
    """)
    total = cur.fetchone()[0]

    if total == 0:
        return stats

    now = datetime.now().isoformat()

    while True:
        # Fetch batch
        cur = conn.execute("""
            SELECT id, result FROM tool_calls
            WHERE result IS NOT NULL AND result_hash IS NULL
            LIMIT ?
        """, (batch_size,))
        rows = cur.fetchall()

        if not rows:
            break

        for row in rows:
            tool_call_id = row["id"]
            result = row["result"]

            stats["bytes_before"] += len(result.encode("utf-8"))

            result_hash = compute_content_hash(result)

            # Verify content matches if blob already exists, then insert or reuse.
            existing_row = conn.execute(
                "SELECT content FROM content_blobs WHERE hash = ?",
                (result_hash,),
            ).fetchone()
            if existing_row is not None:
                if existing_row[0] != result:
                    raise BlobCollisionError(
                        f"SHA256 collision during migration: hash {result_hash!r} exists with different content"
                    )
                stats["blobs_reused"] += 1
            else:
                conn.execute(
                    "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
                    "VALUES (?, ?, 0, ?)",
                    (result_hash, result, now),
                )
                stats["blobs_created"] += 1

            # Update tool_call — the AFTER UPDATE trigger on result_hash
            # increments the blob's ref_count exactly once.
            conn.execute(
                "UPDATE tool_calls "
                "SET result_hash = ?, result = NULL "
                "WHERE id = ? AND result_hash IS NULL",
                (result_hash, tool_call_id),
            )

            stats["migrated"] += 1

        conn.commit()

        if on_progress:
            on_progress(stats["migrated"], total)

    # Calculate bytes after (unique blob storage)
    cur = conn.execute("SELECT SUM(LENGTH(content)) FROM content_blobs")
    stats["bytes_after"] = cur.fetchone()[0] or 0

    return stats


def verify_migration(conn: sqlite3.Connection) -> dict:
    """Verify migration was successful.

    Returns:
        dict with:
            - pending: number of rows still needing migration
            - migrated: number of rows using blob storage
            - orphaned_blobs: blobs with ref_count=0 and no tool_call references
            - ref_count_mismatches: blobs where stored ref_count != actual tool_call reference count
            - negative_ref_counts: blobs with ref_count < 0 (legacy corruption; 0 after migration)
    """
    cur = conn.execute("""
        SELECT
            SUM(CASE WHEN result IS NOT NULL AND result_hash IS NULL THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN result_hash IS NOT NULL THEN 1 ELSE 0 END) as migrated
        FROM tool_calls
    """)
    row = cur.fetchone()

    cur = conn.execute("SELECT COUNT(*) FROM content_blobs WHERE ref_count = 0")
    orphaned = cur.fetchone()[0]

    cur = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT cb.hash
            FROM content_blobs cb
            LEFT JOIN tool_calls tc ON tc.result_hash = cb.hash
            GROUP BY cb.hash
            HAVING cb.ref_count != COUNT(tc.result_hash)
        )
    """)
    ref_count_mismatches = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM content_blobs WHERE ref_count < 0")
    negative_ref_counts = cur.fetchone()[0]

    return {
        "pending": row[0] or 0,
        "migrated": row[1] or 0,
        "orphaned_blobs": orphaned,
        "ref_count_mismatches": ref_count_mismatches,
        "negative_ref_counts": negative_ref_counts,
    }
