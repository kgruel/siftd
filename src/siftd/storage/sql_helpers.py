"""SQL helper utilities for query building and result processing."""

import sqlite3
from collections.abc import Iterable
from typing import Any

# SQLite default limit is SQLITE_MAX_VARIABLE_NUMBER = 999
# Use a safe batch size below this limit
SQLITE_MAX_VARIABLES = 999
DEFAULT_BATCH_SIZE = 500


def has_conversation_owners_table(conn: sqlite3.Connection) -> bool:
    """Return True if the conversation_owners table exists.

    This is used to keep owner scoping safe on pre-migration databases.
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_owners'"
        ).fetchone()
        is not None
    )


def owner_predicate(conversation_id_expr: str) -> str:
    """Return a composable owner-scoping predicate for a conversation id expression."""
    return (
        f"{conversation_id_expr} IN "
        "(SELECT conversation_id FROM conversation_owners WHERE user_id = ?)"
    )


def owner_exists(conversation_id_expr: str) -> str:
    """Return an EXISTS form of the owner-scoping predicate."""
    return (
        "EXISTS (SELECT 1 FROM conversation_owners co "
        f"WHERE co.conversation_id = {conversation_id_expr} AND co.user_id = ?)"
    )


def placeholders(n: int) -> str:
    """Generate placeholder string for IN clause.

    Args:
        n: Number of placeholders needed.

    Returns:
        String like "?, ?, ?" for n=3.
    """
    return ", ".join("?" * n)


def in_clause(values: list[Any]) -> tuple[str, list[Any]]:
    """Generate IN clause placeholder string and values tuple.

    Args:
        values: List of values for IN clause.

    Returns:
        Tuple of (placeholder_string, values_list).
        Example: ("?, ?, ?", [1, 2, 3])
    """
    return placeholders(len(values)), list(values)


def fetchall_dicts(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple | list = (),
) -> list[dict]:
    """Execute query and return results as list of dicts.

    Args:
        conn: Database connection (row_factory is temporarily set).
        sql: SQL query string.
        params: Query parameters.

    Returns:
        List of dict rows.
    """
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.row_factory = old_factory


def batched_in_query(
    conn: sqlite3.Connection,
    sql_template: str,
    ids: Iterable[Any],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    prefix_params: tuple | list = (),
    suffix_params: tuple | list = (),
) -> list[sqlite3.Row]:
    """Execute a query with IN() clause in batches to avoid SQLite variable limits.

    Args:
        conn: Database connection.
        sql_template: SQL with {placeholders} where the IN clause values go.
            Example: "SELECT * FROM foo WHERE x = ? AND id IN ({placeholders})"
        ids: Iterable of IDs for the IN clause.
        batch_size: Max IDs per batch (default 500, must be < 999).
        prefix_params: Params that appear before the IN clause values.
        suffix_params: Params that appear after the IN clause values.

    Returns:
        Aggregated list of rows from all batches.
    """
    id_list = list(ids)
    if not id_list:
        return []

    results: list[sqlite3.Row] = []
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i : i + batch_size]
        ph = placeholders(len(batch))
        sql = sql_template.format(placeholders=ph)
        params = list(prefix_params) + list(batch) + list(suffix_params)
        results.extend(conn.execute(sql, params).fetchall())

    return results


def uncached_input_sql(response_alias: str = "r") -> str:
    """SQL fragment: per-response UNCACHED input tokens, normalized across the
    provider input-token conventions.

    Anthropic reports ``input_tokens`` EXCLUSIVE of cache — ``cache_read`` /
    ``cache_creation`` are separate, additive fields. OpenAI reports it INCLUSIVE
    — ``cache_read`` is a subset already counted in ``input_tokens``. Verified on
    the live corpus (claude: input ≪ cache_read; codex: input ≥ cache_read). Since
    that convention is a hard API-contract property of the provider, it's keyed on
    the harness ``source``; an ambiguous source (e.g. ``multi`` — a Claude-backed
    multiplexer) falls back to the per-row signature, which is unambiguous exactly
    where it matters: a ``cache_read`` exceeding ``input_tokens`` can only be the
    exclusive convention.

    The caller's flatten MUST expose ``{r}.input_tokens``, ``{r}.cache_read`` and
    ``{r}.source`` as columns. Returns a non-negative scalar expression.
    """
    r = response_alias
    it = f"COALESCE({r}.input_tokens, 0)"
    cr = f"COALESCE({r}.cache_read, 0)"
    return (
        f"CASE"
        f" WHEN {r}.source = 'anthropic' THEN {it}"  # exclusive: input is already uncached
        f" WHEN {it} < {cr} THEN {it}"               # exclusive signature (claude-backed 'multi')
        f" ELSE {it} - {cr}"                          # inclusive (openai + default): strip the subset
        f" END"
    )


def cost_expr_sql(
    response_alias: str = "r",
    pricing_alias: str = "pr",
    *,
    coalesce_pricing: bool = False,
) -> str:
    """SQL fragment: per-response cost — cache-aware, four billed components.

    Computes (before the caller's ``/ 1e6``)::

        uncached_input * input_rate
      + cache_creation * cache_creation_rate
      + cache_read     * cache_read_rate
      + output         * output_rate

    where ``uncached_input`` is :func:`uncached_input_sql` (so the token total and
    the cost can never disagree about what's cached), and the three input-side
    components are DISJOINT — no double-charging. This supersedes the old
    "subtract cache_read, bill nothing" form, which billed cache_read at zero and
    collapsed Anthropic cost to output-only (input_tokens being uncached there,
    ``input − cache_read`` clamped to 0).

    Cache rates are OVERRIDE-ONLY pricing columns: when
    ``cache_read_per_mtok`` / ``cache_creation_per_mtok`` are NULL they default to
    the standard Anthropic multiples of the input rate (read ×0.1, creation ×1.25)
    — exact for Anthropic, an approximation for any provider whose cache pricing
    isn't a standard multiple (set the column to override). When
    ``coalesce_pricing`` is False (default) a NULL ``input_per_mtok`` propagates to
    NULL cost — the "unpriced ≠ free" / em-dash invariant.

    REQUIRES the flatten to expose ``{r}.cache_read``, ``{r}.cache_creation``,
    ``{r}.source``, ``{r}.input_tokens``, ``{r}.output_tokens`` — this no longer
    runs its own ``attributes`` subquery (the rebuild joins the cache sums once).

    Returns an expression for use inside ``SUM()``; wrap with
    ``ROUND(SUM(...) / 1000000.0, 4)``.
    """
    r = response_alias
    p = pricing_alias
    input_rate = f"COALESCE({p}.input_per_mtok, 0)" if coalesce_pricing else f"{p}.input_per_mtok"
    output_rate = f"COALESCE({p}.output_per_mtok, 0)" if coalesce_pricing else f"{p}.output_per_mtok"
    # Override-only cache rates; default to the standard multiple of the input rate.
    # When input_rate is NULL (unpriced) these resolve to NULL too, so cost stays NULL.
    cache_read_rate = f"COALESCE({p}.cache_read_per_mtok, {input_rate} * 0.1)"
    cache_creation_rate = f"COALESCE({p}.cache_creation_per_mtok, {input_rate} * 1.25)"

    uncached = uncached_input_sql(r)
    cr = f"COALESCE({r}.cache_read, 0)"
    cc = f"COALESCE({r}.cache_creation, 0)"
    out = f"COALESCE({r}.output_tokens, 0)"
    return (
        f"({uncached}) * {input_rate}"
        f" + {cc} * {cache_creation_rate}"
        f" + {cr} * {cache_read_rate}"
        f" + {out} * {output_rate}"
    )


def batched_execute(
    conn: sqlite3.Connection,
    sql_template: str,
    ids: Iterable[Any],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Execute a modifying query (DELETE, UPDATE) with IN() clause in batches.

    Args:
        conn: Database connection.
        sql_template: SQL with {placeholders} where the IN clause values go.
            Example: "DELETE FROM foo WHERE id IN ({placeholders})"
        ids: Iterable of IDs for the IN clause.
        batch_size: Max IDs per batch (default 500, must be < 999).

    Returns:
        Total number of affected rows across all batches.
    """
    id_list = list(ids)
    if not id_list:
        return 0

    total_affected = 0
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i : i + batch_size]
        ph = placeholders(len(batch))
        sql = sql_template.format(placeholders=ph)
        cur = conn.execute(sql, batch)
        total_affected += cur.rowcount

    return total_affected
