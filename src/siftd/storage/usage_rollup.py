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

from siftd.storage.sql_helpers import cost_expr_sql, uncached_input_sql

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
    cost                  REAL,
    -- v10 cache columns. Listed LAST to match ALTER ADD COLUMN's append order, so
    -- a fresh DB and a v9→v10-migrated DB have identical column order (the slice
    -- copies by position; divergence would corrupt it).
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0
    , PRIMARY KEY (conversation_id, model_id, provider_id)
)
"""
# input_tokens here is the TRUE TOTAL input (uncached + cache_read + cache_creation),
# NOT raw event_response.input_tokens (provider-native: uncached-only for Anthropic,
# cache-inclusive for OpenAI). cache_read_tokens / cache_creation_tokens are the disjoint
# cache components, broken out so cost bills each at its own rate. Normalization to this
# common representation happens once, in rebuild_usage_by_conv_model (the only place that
# knows the per-provider input convention) — see sql_helpers.uncached_input_sql.

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
    flatten ``r`` spans ``events`` + ``event_response`` + the harness ``source``
    (the per-provider input-token convention key) + per-response cache sums from
    the polymorphic ``attributes`` table (keyed on ``e.id``, the response event id).

    Token normalization happens HERE (the only place that knows the provider
    convention): ``input_tokens`` is stored as the TRUE TOTAL input —
    ``uncached + cache_read + cache_creation`` — via :func:`uncached_input_sql`,
    and the two cache components are also stored disjointly so cost can bill each
    at its rate (:func:`cost_expr_sql`). The token total and the cost share the
    same ``uncached`` definition, so they cannot disagree about what's cached.

    All joins are 1:1 — conversations/harnesses by PK, the two cache subqueries
    GROUP BY ``target_id`` (one row per response), ``providers.name`` UNIQUE,
    ``pricing`` UNIQUE(model_id, provider_id) — so no row fans out before the
    aggregate (the precondition that keeps the 290x fan-out class structurally out
    of reach).  ``cost`` is stored UNROUNDED (dollars); the per-conversation round
    happens once, when ``conversation_stats`` sums these rows.

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
    # Cache-awareness is gated on the v10 schema. The discriminator is the PRICING
    # cache-rate columns: only the v10 migration adds them, whereas the rollup table
    # gets cache columns from the fresh DDL the moment v9 CREATEs it (so the rollup's
    # own columns can't distinguish v9-mid-migration from v10). Gating on pricing
    # means the v9 rebuild step runs the FROZEN legacy path — reproducing v9-era
    # cost/tokens so v9's cost-parity assertion still holds — and only a true v10 DB
    # (fresh schema or post-ALTER) folds cache in, with the pricing cache-rate columns
    # cost_expr_sql reads guaranteed present. With no pricing table at all (synthetic
    # DBs) cost is NULL regardless, so the rollup's own cache columns gate the tokens.
    def _has_col(table: str, col: str) -> bool:
        return any(row[1] == col for row in conn.execute(f"PRAGMA table_info({table})"))

    cache_aware = (
        _has_col("pricing", "cache_read_per_mtok")
        if has_pricing
        else _has_col(_TABLE, "cache_read_tokens")
    )

    uncached = uncached_input_sql("r")

    if cache_aware:
        insert_cols = (
            "(conversation_id, model_id, provider_id, input_tokens, output_tokens, "
            "response_count, responses_with_tokens, cost, "
            "cache_read_tokens, cache_creation_tokens)"
        )
        # input_tokens stores the TRUE total; cache components stored disjointly.
        metric_cols = (
            f"COALESCE(SUM(({uncached}) + r.cache_read + r.cache_creation), 0),\n"
            "            COALESCE(SUM(r.output_tokens), 0),"
        )
        cache_cols = (
            "            COALESCE(SUM(r.cache_read), 0),\n"
            "            COALESCE(SUM(r.cache_creation), 0)"
        )
        per_response_cost = cost_expr_sql("r", "pr")
    else:
        insert_cols = (
            "(conversation_id, model_id, provider_id, input_tokens, output_tokens, "
            "response_count, responses_with_tokens, cost)"
        )
        metric_cols = (
            "COALESCE(SUM(r.input_tokens), 0),\n"
            "            COALESCE(SUM(r.output_tokens), 0),"
        )
        cache_cols = None
        # FROZEN v9-era per-response cost: subtract cache_read from input, bill the
        # remainder at the input rate, bill nothing for cache. Uses r.cache_read from
        # the flatten (same value the old attributes subquery produced).
        per_response_cost = (
            "(CASE WHEN COALESCE(r.input_tokens, 0) - r.cache_read < 0 THEN 0 "
            "ELSE COALESCE(r.input_tokens, 0) - r.cache_read END) * pr.input_per_mtok "
            "+ COALESCE(r.output_tokens, 0) * pr.output_per_mtok"
        )

    cost_expr = "NULL"
    cost_join = ""
    if has_pricing:
        # Unrounded per-group dollars.  NULL propagates when unpriced (no COALESCE
        # on the per-mtok rates), preserving the cost>0 vs IS NULL distinction.
        # Route pricing through the harness source when provider_id is NULL.
        cost_expr = f"SUM({per_response_cost}) / 1000000.0"
        cost_join = (
            "LEFT JOIN providers p_fallback ON p_fallback.name = r.source "
            "LEFT JOIN pricing pr "
            "ON pr.model_id = r.model_id "
            "AND pr.provider_id = COALESCE(r.provider_id, p_fallback.id)"
        )

    # Trailing cache columns (when cache_aware) keep the SELECT aligned with the
    # column list, which ends with cost then the two cache columns.
    tail = f",\n            {cost_expr}" + (f",\n{cache_cols}" if cache_cols else "")

    conn.execute(f"""
        INSERT INTO {_TABLE} {insert_cols}
        SELECT
            r.conversation_id,
            r.model_id,
            r.provider_id,
            {metric_cols}
            COUNT(*),
            SUM(CASE WHEN r.input_tokens IS NOT NULL OR r.output_tokens IS NOT NULL
                     THEN 1 ELSE 0 END){tail}
        FROM (
            SELECT e.id, e.conversation_id, er.input_tokens, er.output_tokens,
                   er.model_id, er.provider_id,
                   h.source AS source,
                   COALESCE(cr.v, 0) AS cache_read,
                   COALESCE(cc.v, 0) AS cache_creation
            FROM events e
            JOIN event_response er ON er.event_id = e.id
            LEFT JOIN conversations c ON c.id = e.conversation_id
            LEFT JOIN harnesses h ON h.id = c.harness_id
            LEFT JOIN (
                SELECT target_id, MAX(CAST(value AS INTEGER)) AS v
                FROM attributes
                WHERE target_kind = 'response' AND key = 'cache_read_input_tokens'
                GROUP BY target_id
            ) cr ON cr.target_id = e.id
            LEFT JOIN (
                SELECT target_id, MAX(CAST(value AS INTEGER)) AS v
                FROM attributes
                WHERE target_kind = 'response' AND key = 'cache_creation_input_tokens'
                GROUP BY target_id
            ) cc ON cc.target_id = e.id
            WHERE e.kind = 'response'
        ) r
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
