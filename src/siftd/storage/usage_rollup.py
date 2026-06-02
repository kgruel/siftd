"""Usage rollup — the keystone derived-tier fact table.

``usage_by_conv_model`` at grain ``(conversation_id, model_id, provider_id)`` is
the single usage fact for the corpus.  ``conversation_stats`` is its
model/provider-dropped cache, re-derived from it (see :func:`rebuild_rollups`).
Every per-model / per-workspace / per-harness / per-provider / global token+cost
breakdown is a ``GROUP BY`` over this table — which is why the per-response cost
definition lives *here only* (:func:`rebuild_usage_by_conv_model`) and is never
re-implemented at read time.

Grain note: ``provider_id`` is in the PK even though it is functionally
dependent on ``(conversation_id, model_id)`` in today's corpus (it adds 0 rows).
It is kept so per-provider spend/coverage is a ``GROUP BY`` rather than a
re-descent to raw, and so a future conversation that genuinely spans two
providers for one model splits into correct rows instead of a lossy merge.  A
side effect that keeps cost coherent: each group is single-provider, so its
pricing join resolves to exactly one row (or none) — a group is uniformly
priced or uniformly unpriced, never mixed, so per-group ``cost`` is cleanly a
number-or-NULL.
"""

import sqlite3

from siftd.storage.sql_helpers import cost_expr_sql

_TABLE = "usage_by_conv_model"

_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    conversation_id       TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    model_id              TEXT REFERENCES models(id) ON DELETE SET NULL,
    provider_id           TEXT REFERENCES providers(id) ON DELETE SET NULL,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    response_count        INTEGER NOT NULL DEFAULT 0,
    responses_with_tokens INTEGER NOT NULL DEFAULT 0,
    cost                  REAL
    , PRIMARY KEY (conversation_id, model_id, provider_id)
)
"""

_CREATE_INDEX_SQL = f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_model ON {_TABLE}(model_id)"


def ensure_usage_by_conv_model_table(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Create the usage_by_conv_model table + supporting index if absent."""
    conn.execute(_CREATE_SQL)
    conn.execute(_CREATE_INDEX_SQL)
    if commit:
        conn.commit()


def has_usage_by_conv_model_table(conn: sqlite3.Connection) -> bool:
    """Check whether the usage_by_conv_model table exists."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    return row[0] > 0


def rebuild_usage_by_conv_model(conn: sqlite3.Connection, *, commit: bool = False) -> int:
    """Rebuild the entire usage_by_conv_model rollup from the event tables.

    One set-based ``GROUP BY (conversation_id, model_id, provider_id)``.  The
    flatten ``r`` and the harness-source pricing fallback mirror
    ``rebuild_conversation_stats`` verbatim so cost is defined identically — the
    cache-read term in :func:`cost_expr_sql` needs ``r.id`` (the *event* id, what
    ``attributes.target_id`` keys on), and the response tokens live on
    ``event_response``, so one flattened alias must span both.

    All joins are 1:1 — conversations/harnesses by PK, ``providers.name`` UNIQUE,
    ``pricing`` UNIQUE(model_id, provider_id) — so no row fans out before the
    aggregate (the precondition that keeps the 290x fan-out class structurally
    out of reach).  ``cost`` is stored UNROUNDED (dollars); the per-conversation
    round happens once, when ``conversation_stats`` sums these rows.

    Returns the number of rollup rows written.
    """
    ensure_usage_by_conv_model_table(conn)
    conn.execute(f"DELETE FROM {_TABLE}")

    has_pricing = (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pricing'"
        ).fetchone()[0]
        > 0
    )

    cost_expr = "NULL"
    cost_join = ""
    if has_pricing:
        # Unrounded per-group dollars.  NULL propagates when unpriced (no COALESCE
        # on the per-mtok rates), preserving the cost>0 vs IS NULL distinction.
        # Route pricing through the harness source when provider_id is NULL.
        cost_expr = f"SUM({cost_expr_sql('r', 'pr')}) / 1000000.0"
        cost_join = (
            "LEFT JOIN conversations c2 ON c2.id = r.conversation_id "
            "LEFT JOIN harnesses h2 ON h2.id = c2.harness_id "
            "LEFT JOIN providers p_fallback ON p_fallback.name = h2.source "
            "LEFT JOIN pricing pr "
            "ON pr.model_id = r.model_id "
            "AND pr.provider_id = COALESCE(r.provider_id, p_fallback.id)"
        )

    conn.execute(f"""
        INSERT INTO {_TABLE} (conversation_id, model_id, provider_id,
                              input_tokens, output_tokens, response_count,
                              responses_with_tokens, cost)
        SELECT
            r.conversation_id,
            r.model_id,
            r.provider_id,
            COALESCE(SUM(r.input_tokens), 0),
            COALESCE(SUM(r.output_tokens), 0),
            COUNT(*),
            SUM(CASE WHEN r.input_tokens IS NOT NULL OR r.output_tokens IS NOT NULL
                     THEN 1 ELSE 0 END),
            {cost_expr}
        FROM (SELECT e.id, e.conversation_id, er.input_tokens, er.output_tokens,
                     er.model_id, er.provider_id
              FROM events e JOIN event_response er ON er.event_id = e.id
              WHERE e.kind = 'response') r
        {cost_join}
        GROUP BY r.conversation_id, r.model_id, r.provider_id
    """)
    count = conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
    if commit:
        conn.commit()
    return count


def rebuild_rollups(conn: sqlite3.Connection, *, commit: bool = False) -> int:
    """Rebuild the derived tier in dependency order.

    ``usage_by_conv_model`` (the usage fact) first, then ``conversation_stats``
    (its model/provider-dropped cache) re-derived from it.  This is the entry
    point ingest and the v9 migration call — ``rebuild_conversation_stats``
    alone assumes the rollup is already current.

    Returns the conversation_stats row count.
    """
    from siftd.storage.conversation_stats import rebuild_conversation_stats

    rebuild_usage_by_conv_model(conn)
    count = rebuild_conversation_stats(conn)
    if commit:
        conn.commit()
    return count
