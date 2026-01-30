"""Content-addressable blob storage for deduplication.

Stores large content (tool_calls.result) with SHA256 hash as key.
Reference counting enables garbage collection when content is no longer needed.
"""

import hashlib
import sqlite3
from datetime import datetime


def compute_content_hash(content: str) -> str:
    """Compute SHA256 hash of content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def store_content(
    conn: sqlite3.Connection,
    content: str,
    *,
    commit: bool = False,
) -> str:
    """Store content in blob storage, return hash.

    If content already exists, increments ref_count.
    If content is new, creates blob with ref_count=1.

    Args:
        conn: Database connection
        content: The content string to store
        commit: Whether to commit the transaction

    Returns:
        SHA256 hash of the content
    """
    content_hash = compute_content_hash(content)
    created_at = datetime.now().isoformat()

    conn.execute(
        """
        INSERT INTO content_blobs (hash, content, ref_count, created_at)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(hash) DO UPDATE SET ref_count = ref_count + 1
        """,
        (content_hash, content, created_at),
    )

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

    Args:
        conn: Database connection
        content_hash: SHA256 hash of the content to release
        commit: Whether to commit the transaction
    """
    conn.execute(
        "UPDATE content_blobs SET ref_count = ref_count - 1 WHERE hash = ?",
        (content_hash,),
    )
    conn.execute(
        "DELETE FROM content_blobs WHERE hash = ? AND ref_count = 0",
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
