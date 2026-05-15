"""FTS5 full-text search operations for siftd storage."""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SanitizedFts5Query:
    """Result of sanitizing a user-supplied FTS5 query."""

    fts_query: str | None
    tokens: list[str]
    raw: bool = False


def sanitize_fts5_query(
    query: str,
    *,
    raw: bool = False,
    operator: Literal["and", "or"] = "and",
) -> SanitizedFts5Query:
    """Tokenize and quote a user query for safe FTS5 MATCH use.

    Default (raw=False): extract word tokens, quote each, join with implicit AND
    or OR. FTS5 control operators (NOT/AND/OR) become quoted terms. Short tokens
    (one or two letters) are preserved.

    Raw mode (raw=True): return the query unchanged. Phase-2 OR fallback is
    the caller's responsibility to skip.

    Returns SanitizedFts5Query with fts_query=None for empty or punctuation-only
    input (no word tokens found).
    """
    import re

    if raw:
        stripped = query.strip()
        return SanitizedFts5Query(
            fts_query=stripped if stripped else None,
            tokens=[],
            raw=True,
        )

    tokens = re.findall(r"\w+", query)
    if not tokens:
        return SanitizedFts5Query(fts_query=None, tokens=[], raw=False)

    quoted = [f'"{t}"' for t in tokens]
    if operator == "or":
        fts_query = " OR ".join(quoted)
    else:
        fts_query = " ".join(quoted)
    return SanitizedFts5Query(fts_query=fts_query, tokens=tokens, raw=False)


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
            event_content_id UNINDEXED,
            event_id         UNINDEXED,
            conversation_id  UNINDEXED,
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


def rebuild_fts_index(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Drop and rebuild the FTS index from all event_content blocks.

    Indexes all event_content rows that have indexable text ($.text IS NOT NULL),
    which includes text and thinking block types. Populates content_fts.
    """
    # event_content may not exist yet when called from ensure_fts_table before migration 4 runs
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='event_content'"
    ).fetchone():
        return
    conn.execute("DELETE FROM content_fts")

    conn.execute("""
        INSERT INTO content_fts (text_content, event_content_id, event_id, conversation_id)
        SELECT
            json_extract(ec.content, '$.text'),
            ec.id,
            ec.event_id,
            e.conversation_id
        FROM event_content ec
        JOIN events e ON e.id = ec.event_id
        WHERE json_valid(ec.content)
          AND json_extract(ec.content, '$.text') IS NOT NULL
    """)

    if commit:
        conn.commit()


def get_fts_sync_status(conn: sqlite3.Connection) -> dict:
    """Detect FTS5 index sync issues with event_content.

    Returns dict with keys: orphaned_count, missing_count.
    All zeros if FTS table doesn't exist.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
    ).fetchone()
    if not row:
        return {"orphaned_count": 0, "missing_count": 0}

    orphaned_count = conn.execute("""
        SELECT COUNT(*) FROM content_fts
        WHERE event_content_id NOT IN (SELECT id FROM event_content)
    """).fetchone()[0]

    missing_count = conn.execute("""
        SELECT COUNT(*) FROM event_content ec
        WHERE json_valid(ec.content)
          AND json_extract(ec.content, '$.text') IS NOT NULL
          AND ec.id NOT IN (SELECT event_content_id FROM content_fts)
    """).fetchone()[0]

    return {
        "orphaned_count": orphaned_count,
        "missing_count": missing_count,
    }


def insert_fts_content(
    conn: sqlite3.Connection,
    event_content_id: str,
    event_id: str,
    conversation_id: str,
    text: str,
) -> None:
    """Insert a single text entry into the FTS index."""
    conn.execute(
        "INSERT INTO content_fts (text_content, event_content_id, event_id, conversation_id) VALUES (?, ?, ?, ?)",
        (text, event_content_id, event_id, conversation_id),
    )


def search_content(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    *,
    raw_fts: bool = False,
) -> list[dict]:
    """Search text content using FTS5 MATCH.

    Returns list of dicts with: conversation_id, event_id, kind, snippet, rank.
    """
    sanitized = sanitize_fts5_query(query, raw=raw_fts, operator="and")
    if sanitized.fts_query is None:
        return []
    cur = conn.execute(
        """
        SELECT
            content_fts.conversation_id,
            content_fts.event_id,
            e.kind,
            snippet(content_fts, 0, '>>>', '<<<', '...', 64) as snippet,
            content_fts.rank
        FROM content_fts
        JOIN events e ON e.id = content_fts.event_id
        WHERE content_fts MATCH ?
        ORDER BY content_fts.rank
        LIMIT ?
        """,
        (sanitized.fts_query, limit),
    )
    return [
        {
            "conversation_id": row["conversation_id"],
            "event_id": row["event_id"],
            "kind": row["kind"],
            "snippet": row["snippet"],
            "rank": row["rank"],
        }
        for row in cur.fetchall()
    ]


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
    conn: sqlite3.Connection,
    query: str,
    limit: int = 80,
    *,
    raw_fts: bool = False,
) -> tuple[set[str], str]:
    """FTS5 recall: try AND semantics first, fall back to OR for broader recall.

    Args:
        conn: Database connection.
        query: Search query string.
        limit: Maximum conversation IDs to return.
        raw_fts: If True, pass query directly to FTS5 without sanitization and
            skip OR fallback.

    Returns:
        Tuple of (conversation_id set, mode string).
        Mode is "and", "or", or "none".
    """
    recall = fts5_recall_details(conn, query, limit=limit, raw_fts=raw_fts)
    return set(recall.conversation_ids), recall.mode


def fts5_recall_details(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 80,
    min_and_hits: int = 10,
    raw_fts: bool = False,
) -> Fts5Recall:
    """FTS5 recall with detail about the chosen query form.

    Default (raw_fts=False): sanitizes user input (quoted tokens, operators
    stripped from control position), then falls back to OR if AND yields fewer
    than min_and_hits results. Short tokens (one/two letters) are preserved.

    Raw mode (raw_fts=True): passes query directly to FTS5 without sanitization
    and skips the OR fallback entirely.

    Args:
        conn: Database connection.
        query: Search query string.
        limit: Maximum conversation IDs to return.
        min_and_hits: Minimum conversations to keep AND mode (default: 10).
        raw_fts: If True, use raw FTS5 syntax; skip sanitization and OR fallback.

    Returns:
        Fts5Recall with ordered conversation IDs, mode string, and the concrete
        FTS5 MATCH expression used (fts_query).
    """
    sanitized = sanitize_fts5_query(query, raw=raw_fts, operator="and")
    if sanitized.fts_query is None:
        return Fts5Recall(conversation_ids=[], mode="none", fts_query=None)

    # Phase 1: AND (or raw query)
    try:
        ids = _fts5_conversation_ids_ordered(conn, sanitized.fts_query, limit)
        if (raw_fts and ids) or (not raw_fts and len(ids) >= min_and_hits):
            return Fts5Recall(conversation_ids=ids, mode="and", fts_query=sanitized.fts_query)
    except sqlite3.OperationalError as e:
        log.warning("fts5 phase 1 failed for query %r: %s", query, e)

    # Phase 2: OR rewrite for broader recall (skipped in raw mode)
    if not raw_fts and sanitized.tokens:
        or_query = " OR ".join(f'"{t}"' for t in sanitized.tokens)
        try:
            ids = _fts5_conversation_ids_ordered(conn, or_query, limit)
            if ids:
                return Fts5Recall(conversation_ids=ids, mode="or", fts_query=or_query)
        except sqlite3.OperationalError as e:
            log.warning("fts5 phase 2 or-rewrite failed: %s", e)

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
            e.kind,
            snippet(content_fts, 0, '>>>', '<<<', '...', 64) as snippet,
            content_fts.rank
        FROM content_fts
        JOIN events e ON e.id = content_fts.event_id
        WHERE content_fts MATCH ?
          AND content_fts.conversation_id = ?
        ORDER BY content_fts.rank
        LIMIT 1
        """,
        (fts_query, conversation_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"kind": row["kind"], "snippet": row["snippet"], "rank": row["rank"]}


def fts5_first_event_in_conversation(
    conn: sqlite3.Connection,
    phrase: str,
    *,
    conversation_id: str,
    limit: int | None = 1,
) -> str | None | list[str]:
    """Return the event_id(s) of FTS5 phrase match(es) in chronological order.

    Sorts by rowid (insertion order) rather than relevance rank so 'first'
    means the earliest occurrence in the conversation timeline.

    Args:
        conn: Database connection.
        phrase: Literal phrase text to match. Internally wrapped as an FTS5
            phrase query with embedded quotes escaped.
        conversation_id: Scope match to this conversation.
        limit: When 1 (default), returns the first event_id as str | None.
            When None, returns all matching event_ids as list[str].

    Returns:
        When limit=1: event_id string, or None if no match.
        When limit=None: list of event_id strings (may be empty).
    """
    escaped = phrase.replace('"', '""')
    phrase_query = f'"{escaped}"'
    sql = """
        SELECT content_fts.event_id
        FROM content_fts
        WHERE content_fts MATCH ?
          AND content_fts.conversation_id = ?
        ORDER BY content_fts.rowid
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql, (phrase_query, conversation_id))
    rows = cur.fetchall()
    if limit == 1:
        return rows[0]["event_id"] if rows else None
    return [row["event_id"] for row in rows]


def fts5_all_events_in_conversation(
    conn: sqlite3.Connection,
    phrase: str,
    *,
    conversation_id: str,
) -> list[str]:
    """Return all event_ids matching phrase in chronological order.

    Thin wrapper around fts5_first_event_in_conversation with limit=None.
    """
    result = fts5_first_event_in_conversation(conn, phrase, conversation_id=conversation_id, limit=None)
    assert isinstance(result, list)
    return result


def fts5_event_turn_index(
    conn: sqlite3.Connection,
    event_id: str,
    conversation_id: str,
) -> int | None:
    """Return the 0-based turn index of the turn containing event_id.

    For prompt events: returns the ordinal of this prompt among all prompts
    in the conversation (ordered by timestamp).
    For response/tool_call events: returns the ordinal of the preceding prompt.

    Returns None if event not found or no preceding prompt exists.
    """
    row = conn.execute(
        "SELECT kind, timestamp FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None

    kind = row["kind"]
    ts = row["timestamp"]

    if kind == "prompt":
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE conversation_id = ? AND kind = 'prompt' AND timestamp < ?",
            (conversation_id, ts),
        ).fetchone()[0]
        return count

    # For response/tool_call: find the preceding prompt's timestamp
    prompt_row = conn.execute(
        "SELECT timestamp FROM events WHERE conversation_id = ? AND kind = 'prompt' AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
        (conversation_id, ts),
    ).fetchone()
    if prompt_row is None:
        return None
    prompt_ts = prompt_row["timestamp"]
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE conversation_id = ? AND kind = 'prompt' AND timestamp < ?",
        (conversation_id, prompt_ts),
    ).fetchone()[0]
    return count
