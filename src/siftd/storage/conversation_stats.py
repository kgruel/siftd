"""Materialized conversation stats table.

A lightweight summary table rebuilt at the end of each ingest.
Holds precomputed metrics (prompt_count, response_count, total_tokens,
dominant model, cost) so list_conversations can read a single row per
conversation instead of joining/aggregating the responses table.
"""

import sqlite3
from dataclasses import dataclass

_TABLE = "conversation_stats"

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
    """Rebuild the entire conversation_stats table from source tables.

    Returns the number of rows written.
    """
    ensure_conversation_stats_table(conn)
    conn.execute(f"DELETE FROM {_TABLE}")

    # Check if pricing table exists for cost calculation
    has_pricing = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pricing'"
    ).fetchone()[0] > 0

    cost_expr = "NULL"
    cost_join = ""
    if has_pricing:
        cost_expr = """ROUND(SUM(
            CASE
                WHEN COALESCE(r.input_tokens, 0) - COALESCE(
                    (SELECT MAX(CAST(ra.value AS INTEGER))
                     FROM response_attributes ra
                     WHERE ra.response_id = r.id
                       AND ra.key = 'cache_read_input_tokens'), 0) < 0
                THEN 0
                ELSE COALESCE(r.input_tokens, 0) - COALESCE(
                    (SELECT MAX(CAST(ra.value AS INTEGER))
                     FROM response_attributes ra
                     WHERE ra.response_id = r.id
                       AND ra.key = 'cache_read_input_tokens'), 0)
            END * pr.input_per_mtok
            + COALESCE(r.output_tokens, 0) * pr.output_per_mtok
        ) / 1000000.0, 4)"""
        # Route pricing through harness source when responses.provider_id is NULL.
        # COALESCE(r.provider_id, p_fallback.id) means: use the response's explicit
        # provider if set, otherwise fall back to the harness source's provider.
        # Removing COALESCE on per_mtok values lets NULL propagate when pricing is
        # absent, so missing pricing yields NULL cost instead of 0.0.
        cost_join = (
            "LEFT JOIN conversations c2 ON c2.id = r.conversation_id "
            "LEFT JOIN harnesses h2 ON h2.id = c2.harness_id "
            "LEFT JOIN providers p_fallback ON p_fallback.name = h2.source "
            "LEFT JOIN pricing pr "
            "ON pr.model_id = r.model_id "
            "AND pr.provider_id = COALESCE(r.provider_id, p_fallback.id)"
        )

    conn.execute(f"""
        INSERT INTO {_TABLE} (conversation_id, prompt_count, response_count,
                              total_tokens, model_name, cost)
        SELECT
            c.id,
            (SELECT COUNT(*) FROM prompts WHERE conversation_id = c.id),
            (SELECT COUNT(*) FROM responses WHERE conversation_id = c.id),
            (SELECT COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
             FROM responses WHERE conversation_id = c.id),
            (SELECT m.name FROM responses r2
             LEFT JOIN models m ON m.id = r2.model_id
             WHERE r2.conversation_id = c.id
             GROUP BY m.name ORDER BY COUNT(*) DESC LIMIT 1),
            (SELECT {cost_expr}
             FROM responses r {cost_join}
             WHERE r.conversation_id = c.id)
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
