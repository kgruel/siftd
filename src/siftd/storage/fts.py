"""FTS5 full-text search operations for siftd storage."""

import sqlite3
from dataclasses import dataclass


def ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Ensure the main content FTS5 table exists with the expected tokenizer.

    Notes:
      - SQLite does not support altering an existing FTS5 virtual table's tokenizer.
      - If an existing DB has a non-Porter tokenizer, we drop and rebuild the FTS
        index to apply stemming. This happens on upgrade during the next open in
        write mode (e.g., `siftd ingest`).
    """
    expected = """
        CREATE VIRTUAL TABLE content_fts USING fts5(
            text_content,
            content_id UNINDEXED,
            side UNINDEXED,
            conversation_id UNINDEXED,
            tokenize='porter unicode61 remove_diacritics 1'
        )
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='content_fts'"
    ).fetchone()
    if row is None:
        conn.execute(expected)
        return

    existing_sql = (row[0] or "").lower()
    has_porter = "tokenize" in existing_sql and "porter" in existing_sql
    if has_porter:
        return

    conn.execute("DROP TABLE IF EXISTS content_fts")
    conn.execute(expected)
    rebuild_fts_index(conn)


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
          AND json_valid(pc.content)
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
          AND json_valid(rc.content)
          AND json_extract(rc.content, '$.text') IS NOT NULL
    """)

    conn.commit()


def get_fts_sync_status(conn: sqlite3.Connection) -> dict:
    """Detect FTS5 index sync issues with content tables.

    Returns dict with keys: orphaned_count, missing_prompt_count,
    missing_response_count. All zeros if FTS table doesn't exist.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
    ).fetchone()
    if not row:
        return {"orphaned_count": 0, "missing_prompt_count": 0, "missing_response_count": 0}

    orphaned_count = conn.execute("""
        SELECT COUNT(*) FROM content_fts
        WHERE content_id NOT IN (SELECT id FROM prompt_content)
          AND content_id NOT IN (SELECT id FROM response_content)
    """).fetchone()[0]

    missing_prompt_count = conn.execute("""
        SELECT COUNT(*) FROM prompt_content pc
        WHERE pc.block_type = 'text'
          AND pc.id NOT IN (SELECT content_id FROM content_fts WHERE side = 'prompt')
    """).fetchone()[0]

    missing_response_count = conn.execute("""
        SELECT COUNT(*) FROM response_content rc
        WHERE rc.block_type = 'text'
          AND rc.id NOT IN (SELECT content_id FROM content_fts WHERE side = 'response')
    """).fetchone()[0]

    return {
        "orphaned_count": orphaned_count,
        "missing_prompt_count": missing_prompt_count,
        "missing_response_count": missing_response_count,
    }


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


def _fts5_conversation_ids_ordered(
    conn: sqlite3.Connection, fts_query: str, limit: int
) -> list[str]:
    """Run FTS5 MATCH and return distinct conversation IDs (best-first)."""
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
    return [row["conversation_id"] for row in cur.fetchall()]


@dataclass(frozen=True)
class Fts5Recall:
    """FTS5 recall decision result for a query."""

    conversation_ids: list[str]
    mode: str
    fts_query: str | None


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
    recall = fts5_recall_details(conn, query, limit=limit)
    return set(recall.conversation_ids), recall.mode


def fts5_recall_details(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 80,
    min_and_hits: int = 10,
) -> Fts5Recall:
    """FTS5 recall with detail about the chosen query form.

    Uses implicit AND (raw query) when it yields enough conversations, otherwise
    rewrites the query into an OR expression for broader recall.

    Args:
        conn: Database connection.
        query: Search query string (FTS5 MATCH syntax).
        limit: Maximum conversation IDs to return.
        min_and_hits: Minimum number of conversations required to keep AND mode.
            If fewer matches are found, OR rewrite is attempted. Set to 1 for
            strict AND-first behavior; set to >=limit to strongly prefer OR.

    Returns:
        Fts5Recall with ordered conversation IDs, mode string, and the concrete
        FTS5 MATCH expression used (fts_query).
    """
    # Phase 1: implicit AND (raw query)
    try:
        ids = _fts5_conversation_ids_ordered(conn, query, limit)
        if len(ids) >= min_and_hits:
            return Fts5Recall(conversation_ids=ids, mode="and", fts_query=query)
    except Exception:  # pragma: no cover — malformed FTS query
        pass

    # Phase 2: OR rewrite for broader recall
    or_query = _fts5_or_rewrite(query)
    if or_query:
        try:
            ids = _fts5_conversation_ids_ordered(conn, or_query, limit)
            if ids:
                return Fts5Recall(conversation_ids=ids, mode="or", fts_query=or_query)
        except Exception:  # pragma: no cover — OR queries are self-constructed
            pass

    return Fts5Recall(conversation_ids=[], mode="none", fts_query=None)


def fts5_best_hit_for_conversation(
    conn: sqlite3.Connection,
    fts_query: str,
    *,
    conversation_id: str,
) -> dict | None:
    """Return the best (lowest-rank) hit for a conversation, including snippet."""
    cur = conn.execute(
        """
        SELECT
            side,
            snippet(content_fts, 0, '>>>', '<<<', '...', 64) as snippet,
            rank
        FROM content_fts
        WHERE content_fts MATCH ?
          AND conversation_id = ?
        ORDER BY rank
        LIMIT 1
        """,
        (fts_query, conversation_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"side": row["side"], "snippet": row["snippet"], "rank": row["rank"]}
