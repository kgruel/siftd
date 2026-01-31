"""FTS5 full-text search operations for siftd storage."""

import sqlite3


def ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Create the FTS5 virtual table if it doesn't exist. Idempotent."""
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
            text_content,
            content_id UNINDEXED,
            side UNINDEXED,
            conversation_id UNINDEXED
        )
    """)


def rebuild_fts_index(conn: sqlite3.Connection) -> None:
    """Drop and rebuild the FTS index from all text content blocks.

    Reads prompt_content and response_content where block_type='text',
    extracts the text from JSON content, and populates content_fts.
    """
    conn.execute("DELETE FROM content_fts")

    # Index prompt text blocks
    conn.execute("""
        INSERT INTO content_fts (text_content, content_id, side, conversation_id)
        SELECT
            json_extract(pc.content, '$.text'),
            pc.id,
            'prompt',
            p.conversation_id
        FROM prompt_content pc
        JOIN prompts p ON p.id = pc.prompt_id
        WHERE pc.block_type = 'text'
          AND json_extract(pc.content, '$.text') IS NOT NULL
    """)

    # Index response text blocks
    conn.execute("""
        INSERT INTO content_fts (text_content, content_id, side, conversation_id)
        SELECT
            json_extract(rc.content, '$.text'),
            rc.id,
            'response',
            r.conversation_id
        FROM response_content rc
        JOIN responses r ON r.id = rc.response_id
        WHERE rc.block_type = 'text'
          AND json_extract(rc.content, '$.text') IS NOT NULL
    """)

    conn.commit()


def insert_fts_content(
    conn: sqlite3.Connection,
    content_id: str,
    side: str,
    conversation_id: str,
    text: str,
) -> None:
    """Insert a single text entry into the FTS index."""
    conn.execute(
        "INSERT INTO content_fts (text_content, content_id, side, conversation_id) VALUES (?, ?, ?, ?)",
        (text, content_id, side, conversation_id),
    )


def search_content(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Search text content using FTS5 MATCH.

    Returns list of dicts with: conversation_id, side, snippet, rank.
    """
    cur = conn.execute(
        """
        SELECT
            conversation_id,
            side,
            snippet(content_fts, 0, '>>>', '<<<', '...', 64) as snippet,
            rank
        FROM content_fts
        WHERE content_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    )
    return [
        {
            "conversation_id": row["conversation_id"],
            "side": row["side"],
            "snippet": row["snippet"],
            "rank": row["rank"],
        }
        for row in cur.fetchall()
    ]


def _fts5_or_rewrite(query: str) -> str | None:
    """Split query into tokens, filter short ones, join with OR for broad recall."""
    import re
    tokens = re.findall(r"\w+", query)
    tokens = [t for t in tokens if len(t) >= 3]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def _fts5_conversation_ids(
    conn: sqlite3.Connection, fts_query: str, limit: int
) -> set[str]:
    """Run FTS5 MATCH and return distinct conversation IDs."""
    cur = conn.execute(
        """
        SELECT conversation_id FROM content_fts
        WHERE content_fts MATCH ?
        GROUP BY conversation_id
        ORDER BY MIN(rank)
        LIMIT ?
        """,
        (fts_query, limit),
    )
    return {row["conversation_id"] for row in cur.fetchall()}


def fts5_recall_conversations(
    conn: sqlite3.Connection, query: str, limit: int = 80
) -> tuple[set[str], str]:
    """FTS5 recall: try AND semantics first, fall back to OR for broader recall.

    Args:
        conn: Database connection.
        query: Search query string.
        limit: Maximum conversation IDs to return.

    Returns:
        Tuple of (conversation_id set, mode string).
        Mode is "and", "or", or "none".
    """
    # Phase 1: implicit AND (raw query)
    try:
        ids = _fts5_conversation_ids(conn, query, limit)
        if len(ids) >= 10:
            return ids, "and"
    except Exception:
        pass  # malformed FTS query, fall through to OR rewrite

    # Phase 2: OR rewrite for broader recall
    or_query = _fts5_or_rewrite(query)
    if or_query:
        try:
            ids = _fts5_conversation_ids(conn, or_query, limit)
            if ids:
                return ids, "or"
        except Exception:
            pass

    return set(), "none"
