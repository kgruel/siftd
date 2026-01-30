"""SQL helper utilities for query building and result processing."""

import sqlite3
from typing import Any


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
