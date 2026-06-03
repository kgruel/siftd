"""Materialized conversation stats table.

A lightweight summary table rebuilt at the end of each ingest.
Holds precomputed metrics (prompt_count, response_count, total_tokens,
dominant model, cost) so list_conversations can read a single row per
conversation instead of joining/aggregating the responses table.
"""

import sqlite3
from dataclasses import dataclass

_TABLE = "conversation_stats"
_ROLLUP = "usage_by_conv_model"

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    prompt_count    INTEGER NOT NULL DEFAULT 0,
    response_count  INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    model_name      TEXT,
    cost            REAL
)
"""


def ensure_conversation_stats_table(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Create the conversation_stats table if it doesn't exist."""
    conn.execute(_CREATE_SQL)
    if commit:
        conn.commit()


def has_conversation_stats_table(conn: sqlite3.Connection) -> bool:
    """Check if the conversation_stats table exists."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    return row[0] > 0


def rebuild_conversation_stats(conn: sqlite3.Connection, *, commit: bool = False) -> int:
    """Rebuild conversation_stats as a cache derived from ``usage_by_conv_model``.

    Precondition: the rollup must be current.  Call
    :func:`siftd.storage.usage_rollup.rebuild_rollups` to rebuild both in order;
    this function alone assumes the rollup already reflects the events.

    ``response_count`` / ``total_tokens`` / ``cost`` are SUMs over the rollup
    (the single usage-fact source — cost is *not* recomputed from raw here).
    Because each rollup group is single-provider and thus uniformly priced or
    unpriced, ``SUM(cost)`` ignoring NULLs matches the historical per-response
    behaviour exactly; the round happens once here, at conversation grain.
    ``model_name`` is the family-label argmax (``LEFT JOIN models`` so an
    all-NULL-model conversation still resolves to a NULL label), with a
    deterministic tiebreak: on equal response counts a *named* model beats the
    NULL-model group (``(m.name IS NULL)``), then alphabetical (``m.name``) — a
    NULL "dominant model" is the least useful label.  ``prompt_count`` still
    counts prompt events (not a usage fact).

    Returns the number of rows written.
    """
    ensure_conversation_stats_table(conn)
    conn.execute(f"DELETE FROM {_TABLE}")

    conn.execute(f"""
        INSERT INTO {_TABLE} (conversation_id, prompt_count, response_count,
                              total_tokens, model_name, cost)
        SELECT
            c.id,
            (SELECT COUNT(*) FROM events WHERE kind = 'prompt' AND conversation_id = c.id),
            COALESCE((SELECT SUM(u.response_count) FROM {_ROLLUP} u
                      WHERE u.conversation_id = c.id), 0),
            COALESCE((SELECT SUM(u.input_tokens) + SUM(u.output_tokens) FROM {_ROLLUP} u
                      WHERE u.conversation_id = c.id), 0),
            (SELECT m.name
             FROM {_ROLLUP} u
             LEFT JOIN models m ON m.id = u.model_id
             WHERE u.conversation_id = c.id
             GROUP BY m.name
             ORDER BY SUM(u.response_count) DESC, (m.name IS NULL), m.name LIMIT 1),
            (SELECT ROUND(SUM(u.cost), 4) FROM {_ROLLUP} u
             WHERE u.conversation_id = c.id)
        FROM conversations c
    """)
    count = conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
    if commit:
        conn.commit()
    return count


@dataclass
class CostCoverage:
    """Cost coverage across conversations with token data."""

    total_with_tokens: int
    with_positive_cost: int
    with_null_cost: int
    pct_covered: float


def get_cost_coverage(conn: sqlite3.Connection) -> CostCoverage | None:
    """Get cost coverage statistics from conversation_stats.

    Returns None if the conversation_stats table does not exist.

    Cost coverage is measured as the fraction of token-bearing conversations
    that have a positive computed cost (cost > 0).  Conversations with NULL cost
    have no pricing data available; conversations with cost = 0.0 have tokens
    but were priced at zero (indicates stale stats -- run siftd ingest to rebuild).
    """
    if not has_conversation_stats_table(conn):
        return None

    row = conn.execute("""
        SELECT
            COUNT(*) FILTER (WHERE total_tokens > 0) AS with_tokens,
            COUNT(*) FILTER (WHERE cost > 0) AS with_cost,
            COUNT(*) FILTER (WHERE total_tokens > 0 AND cost IS NULL) AS null_cost
        FROM conversation_stats
    """).fetchone()

    with_tokens = row["with_tokens"] or 0
    with_cost = row["with_cost"] or 0
    null_cost = row["null_cost"] or 0
    pct = round((with_cost / with_tokens) * 100, 2) if with_tokens else 0.0

    return CostCoverage(
        total_with_tokens=with_tokens,
        with_positive_cost=with_cost,
        with_null_cost=null_cost,
        pct_covered=pct,
    )
