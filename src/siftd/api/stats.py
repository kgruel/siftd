"""Database statistics API."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from siftd.paths import db_path as default_db_path
from siftd.storage.queries import (
    fetch_conversation_time_window,
    fetch_harness_conversation_counts,
    fetch_harnesses,
    fetch_last_ingest_time,
    fetch_model_names,
    fetch_table_count,
    fetch_top_conversation_tags,
    fetch_top_tools,
    fetch_top_workspaces,
)
from siftd.storage.sqlite import open_database


@dataclass
class TableCounts:
    """Row counts for core tables."""

    conversations: int
    prompts: int
    responses: int
    tool_calls: int
    harnesses: int
    workspaces: int
    tools: int
    models: int
    ingested_files: int


@dataclass
class HarnessInfo:
    """Harness metadata."""

    name: str
    source: str | None
    log_format: str | None


@dataclass
class WorkspaceStats:
    """Workspace with conversation count."""

    path: str
    conversation_count: int
    last_activity: str | None


@dataclass
class ToolStats:
    """Tool with usage count."""

    name: str
    usage_count: int


@dataclass
class HarnessCount:
    """Conversation count by harness."""

    name: str
    conversation_count: int


@dataclass
class TagStats:
    """Tag usage count."""

    name: str
    count: int


@dataclass
class DatabaseStats:
    """Complete database statistics."""

    db_path: Path
    db_size_bytes: int
    counts: TableCounts
    harnesses: list[HarnessInfo]
    harness_counts: list[HarnessCount]
    top_workspaces: list[WorkspaceStats]
    models: list[str]
    top_tools: list[ToolStats]
    top_tags: list[TagStats]
    activity_window: tuple[str | None, str | None]
    last_ingest_at: str | None


def list_workspaces(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """List workspaces with conversation counts.

    Args:
        conn: Database connection.
        limit: Maximum workspaces to return.

    Returns:
        Rows with 'path' and 'convs' keys.
    """
    return fetch_top_workspaces(conn, limit=limit)


def get_stats(*, db_path: Path | None = None) -> DatabaseStats:
    """Get comprehensive database statistics.

    Args:
        db_path: Path to database. Uses default if not specified.

    Returns:
        DatabaseStats with counts, harnesses, workspaces, models, tools.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)

    # Table counts
    table_names = [
        "conversations",
        "prompts",
        "responses",
        "tool_calls",
        "harnesses",
        "workspaces",
        "tools",
        "models",
        "ingested_files",
    ]
    count_values = {name: fetch_table_count(conn, name) for name in table_names}
    counts = TableCounts(**count_values)

    # Harnesses
    harness_rows = fetch_harnesses(conn)
    harnesses = [
        HarnessInfo(
            name=row["name"],
            source=row["source"],
            log_format=row["log_format"],
        )
        for row in harness_rows
    ]

    # Top workspaces
    workspace_rows = fetch_top_workspaces(conn, limit=10)
    top_workspaces = [
        WorkspaceStats(
            path=row["path"],
            conversation_count=row["convs"],
            last_activity=row["last_activity"],
        )
        for row in workspace_rows
    ]

    # Models
    models = fetch_model_names(conn)

    # Top tools by usage
    tool_rows = fetch_top_tools(conn, limit=10)
    top_tools = [
        ToolStats(name=row["name"], usage_count=row["uses"]) for row in tool_rows
    ]

    # Harness conversation counts
    harness_count_rows = fetch_harness_conversation_counts(conn)
    harness_counts = [
        HarnessCount(name=row["name"], conversation_count=row["conversations"])
        for row in harness_count_rows
    ]

    # Top conversation tags
    tag_rows = fetch_top_conversation_tags(conn, limit=5)
    top_tags = [TagStats(name=row["name"], count=row["count"]) for row in tag_rows]

    # Activity window and ingest recency
    activity_window = fetch_conversation_time_window(conn)
    last_ingest_at = fetch_last_ingest_time(conn)

    conn.close()

    return DatabaseStats(
        db_path=db,
        db_size_bytes=db.stat().st_size,
        counts=counts,
        harnesses=harnesses,
        harness_counts=harness_counts,
        top_workspaces=top_workspaces,
        models=models,
        top_tools=top_tools,
        top_tags=top_tags,
        activity_window=activity_window,
        last_ingest_at=last_ingest_at,
    )
