"""Content-addressable blob storage for deduplication.

Stores large content (tool_calls.result) with SHA256 hash as key.
Reference counting enables garbage collection when content is no longer needed.
"""

import hashlib
import sqlite3
from datetime import datetime

_sha256 = hashlib.sha256


class BlobCollisionError(Exception):
    """Two distinct content values produced the same SHA256 hash."""


def compute_content_hash(content: str) -> str:
    """Compute SHA256 hash of content string."""
    return _sha256(content.encode("utf-8")).hexdigest()


# Shared timestamp for batch operations — avoids datetime.now() per call
_batch_timestamp: str | None = None


def store_content(
    conn: sqlite3.Connection,
    content: str,
    *,
    commit: bool = False,
) -> str:
    """Store content in blob storage, return hash.

    If content already exists with the same bytes, increments ref_count.
    If content is new, creates blob with ref_count=1.
    Raises BlobCollisionError if a different content maps to the same hash.

    Args:
        conn: Database connection
        content: The content string to store
        commit: Whether to commit the transaction

    Returns:
        SHA256 hash of the content
    """
    global _batch_timestamp
    content_hash = _sha256(content.encode("utf-8")).hexdigest()
    if _batch_timestamp is None:
        _batch_timestamp = datetime.now().isoformat()

    # SAVEPOINT wraps the upsert so a collision rolls back the ref_count increment
    # before raising, preserving the fail-closed invariant from H17.
    conn.execute("SAVEPOINT _store_content")
    row = conn.execute(
        """
        INSERT INTO content_blobs (hash, content, ref_count, created_at)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(hash) DO UPDATE SET ref_count = ref_count + 1
        RETURNING content
        """,
        (content_hash, content, _batch_timestamp),
    ).fetchone()
    if row[0] != content:
        conn.execute("ROLLBACK TO SAVEPOINT _store_content")
        conn.execute("RELEASE SAVEPOINT _store_content")
        raise BlobCollisionError(
            f"SHA256 collision: hash {content_hash!r} already stored with different content"
        )
    conn.execute("RELEASE SAVEPOINT _store_content")

    if commit:
        conn.commit()

    return content_hash


def get_content(conn: sqlite3.Connection, content_hash: str) -> str | None:
    """Retrieve content by hash.

    Args:
        conn: Database connection
        content_hash: SHA256 hash of the content

    Returns:
        The content string, or None if not found
    """
    cur = conn.execute(
        "SELECT content FROM content_blobs WHERE hash = ?",
        (content_hash,),
    )
    row = cur.fetchone()
    return row["content"] if row else None


def release_content(
    conn: sqlite3.Connection,
    content_hash: str,
    *,
    commit: bool = False,
) -> None:
    """Decrement ref_count for content. Deletes blob if ref_count reaches 0.

    Clamps to 0 to guard against corrupted negative ref_counts.

    Args:
        conn: Database connection
        content_hash: SHA256 hash of the content to release
        commit: Whether to commit the transaction
    """
    conn.execute(
        "UPDATE content_blobs SET ref_count = MAX(ref_count - 1, 0) WHERE hash = ?",
        (content_hash,),
    )
    conn.execute(
        "DELETE FROM content_blobs WHERE hash = ? AND ref_count <= 0",
        (content_hash,),
    )

    if commit:
        conn.commit()


def get_ref_count(conn: sqlite3.Connection, content_hash: str) -> int:
    """Get current ref_count for a blob.

    Args:
        conn: Database connection
        content_hash: SHA256 hash of the content

    Returns:
        Current ref_count, or 0 if blob doesn't exist
    """
    cur = conn.execute(
        "SELECT ref_count FROM content_blobs WHERE hash = ?",
        (content_hash,),
    )
    row = cur.fetchone()
    return row["ref_count"] if row else 0
