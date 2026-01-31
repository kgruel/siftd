"""SQL helper utilities for query building and result processing."""

import sqlite3
from collections.abc import Iterable
from typing import Any

# SQLite default limit is SQLITE_MAX_VARIABLE_NUMBER = 999
# Use a safe batch size below this limit
SQLITE_MAX_VARIABLES = 999
DEFAULT_BATCH_SIZE = 500


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
