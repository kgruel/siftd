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
    fetch_response_token_coverage,
    fetch_table_count,
    fetch_token_coverage_by_harness,
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
class TokenCoverageByHarness:
    """Token coverage summary for a harness."""

    name: str
    responses: int
    with_tokens: int
    pct_with_tokens: float


@dataclass
class TokenCoverage:
    """Overall token coverage summary."""

    responses: int
    with_tokens: int
    pct_with_tokens: float
    by_harness: list[TokenCoverageByHarness]


@dataclass
class CostCoverage:
    """Cost coverage across conversations with token data."""

    total_with_tokens: int
    with_positive_cost: int
    with_null_cost: int
    pct_covered: float


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
    token_coverage: TokenCoverage
    activity_window: tuple[str | None, str | None]
    last_ingest_at: str | None


def get_cost_coverage(conn: sqlite3.Connection) -> CostCoverage | None:
    """Get cost coverage statistics from conversation_stats.

    Returns None if the conversation_stats table does not exist.

    Cost coverage is measured as the fraction of token-bearing conversations
    that have a positive computed cost (cost > 0).  Conversations with NULL cost
    have no pricing data available; conversations with cost = 0.0 have tokens
    but were priced at zero (indicates stale stats — run siftd ingest to rebuild).
    """
    has_stats = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='conversation_stats'"
    ).fetchone()[0]
    if not has_stats:
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

    # Token coverage
    total_responses, responses_with_tokens = fetch_response_token_coverage(conn)
    pct_with_tokens = (
        round((responses_with_tokens / total_responses) * 100, 2)
        if total_responses
        else 0.0
    )
    harness_rows = fetch_token_coverage_by_harness(conn)
    token_by_harness = []
    for row in harness_rows:
        responses = row["responses"]
        with_tokens = row["with_tokens"] if row["with_tokens"] is not None else 0
        pct = round((with_tokens / responses) * 100, 2) if responses else 0.0
        token_by_harness.append(
            TokenCoverageByHarness(
                name=row["harness"],
                responses=responses,
                with_tokens=with_tokens,
                pct_with_tokens=pct,
            )
        )

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
        token_coverage=TokenCoverage(
            responses=total_responses,
            with_tokens=responses_with_tokens,
            pct_with_tokens=pct_with_tokens,
            by_harness=token_by_harness,
        ),
        activity_window=activity_window,
        last_ingest_at=last_ingest_at,
    )
