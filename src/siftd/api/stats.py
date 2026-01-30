"""Database statistics API."""

from dataclasses import dataclass
from pathlib import Path

from siftd.paths import db_path as default_db_path
from siftd.storage.queries import (
    fetch_harnesses,
    fetch_model_names,
    fetch_table_count,
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


@dataclass
class ToolStats:
    """Tool with usage count."""

    name: str
    usage_count: int


@dataclass
class DatabaseStats:
    """Complete database statistics."""

    db_path: Path
    db_size_bytes: int
    counts: TableCounts
    harnesses: list[HarnessInfo]
    top_workspaces: list[WorkspaceStats]
    models: list[str]
    top_tools: list[ToolStats]


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
        WorkspaceStats(path=row["path"], conversation_count=row["convs"])
        for row in workspace_rows
    ]

    # Models
    models = fetch_model_names(conn)

    # Top tools by usage
    tool_rows = fetch_top_tools(conn, limit=10)
    top_tools = [
        ToolStats(name=row["name"], usage_count=row["uses"]) for row in tool_rows
    ]

    conn.close()

    return DatabaseStats(
        db_path=db,
        db_size_bytes=db.stat().st_size,
        counts=counts,
        harnesses=harnesses,
        top_workspaces=top_workspaces,
        models=models,
        top_tools=top_tools,
    )
