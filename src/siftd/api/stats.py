"""Database statistics API."""

from dataclasses import dataclass
from pathlib import Path

from siftd.paths import db_path as default_db_path
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
    tables = [
        ("conversations", "conversations"),
        ("prompts", "prompts"),
        ("responses", "responses"),
        ("tool_calls", "tool_calls"),
        ("harnesses", "harnesses"),
        ("workspaces", "workspaces"),
        ("tools", "tools"),
        ("models", "models"),
        ("ingested_files", "ingested_files"),
    ]
    count_values = {}
    for attr_name, table_name in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        count_values[attr_name] = count

    counts = TableCounts(**count_values)

    # Harnesses
    harness_rows = conn.execute(
        "SELECT name, source, log_format FROM harnesses"
    ).fetchall()
    harnesses = [
        HarnessInfo(
            name=row["name"],
            source=row["source"],
            log_format=row["log_format"],
        )
        for row in harness_rows
    ]

    # Top workspaces
    workspace_rows = conn.execute("""
        SELECT w.path, COUNT(c.id) as convs
        FROM workspaces w
        LEFT JOIN conversations c ON c.workspace_id = w.id
        GROUP BY w.id
        ORDER BY convs DESC
        LIMIT 10
    """).fetchall()
    top_workspaces = [
        WorkspaceStats(path=row["path"], conversation_count=row["convs"])
        for row in workspace_rows
    ]

    # Models
    model_rows = conn.execute("SELECT raw_name FROM models").fetchall()
    models = [row["raw_name"] for row in model_rows]

    # Top tools by usage
    tool_rows = conn.execute("""
        SELECT t.name, COUNT(tc.id) as uses
        FROM tools t
        JOIN tool_calls tc ON tc.tool_id = t.id
        GROUP BY t.id
        ORDER BY uses DESC
        LIMIT 10
    """).fetchall()
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
