"""Materialized conversation stats table.

A lightweight summary table rebuilt at the end of each ingest.
Holds precomputed metrics (prompt_count, response_count, total_tokens,
dominant model, cost) so list_conversations can read a single row per
conversation instead of joining/aggregating the responses table.
"""

import sqlite3

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


def ensure_conversation_stats_table(conn: sqlite3.Connection) -> None:
    """Create the conversation_stats table if it doesn't exist."""
    conn.execute(_CREATE_SQL)
    conn.commit()


def has_conversation_stats_table(conn: sqlite3.Connection) -> bool:
    """Check if the conversation_stats table exists."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    return row[0] > 0


def rebuild_conversation_stats(conn: sqlite3.Connection) -> int:
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
            END * COALESCE(pr.input_per_mtok, 0)
            + COALESCE(r.output_tokens, 0) * COALESCE(pr.output_per_mtok, 0)
        ) / 1000000.0, 4)"""
        cost_join = (
            "LEFT JOIN pricing pr "
            "ON pr.model_id = r.model_id AND pr.provider_id = r.provider_id"
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
    conn.commit()
    return count
