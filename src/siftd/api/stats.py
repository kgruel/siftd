"""Database statistics API."""

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from siftd.paths import cache_dir
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


def list_workspaces(
    conn: sqlite3.Connection | None = None,
    limit: int = 10,
    *,
    db_path: Path | None = None,
) -> list[sqlite3.Row]:
    """List workspaces with conversation counts.

    Args:
        conn: Database connection. Opened from db_path if not provided.
        limit: Maximum workspaces to return.
        db_path: Path to database. Ignored if conn provided.

    Returns:
        Rows with 'path' and 'convs' keys.
    """
    should_close = False
    if conn is None:
        db = db_path or default_db_path()
        conn = open_database(db, read_only=True)
        should_close = True
    try:
        return fetch_top_workspaces(conn, limit=limit)
    finally:
        if should_close:
            conn.close()


def stats_cache_path() -> Path:
    """Return path to the stats cache file."""
    return cache_dir() / "stats.json"


def _stats_to_dict(stats: DatabaseStats) -> dict:
    """Serialize DatabaseStats to a JSON-safe dict.

    Delegates to serialization.stats.serialize_stats — the canonical
    serializer. This wrapper kept for backward compatibility.
    """
    from siftd.serialization.stats import serialize_stats

    return serialize_stats(stats)



def _dict_to_stats(data: dict) -> DatabaseStats:
    """Deserialize a JSON dict back to DatabaseStats."""
    c = data["counts"]
    tc = data["token_coverage"]
    aw = data["activity_window"]

    return DatabaseStats(
        db_path=Path(data["db_path"]),
        db_size_bytes=data["db_size_bytes"],
        counts=TableCounts(**c),
        harnesses=[
            HarnessInfo(name=h["name"], source=h.get("source"), log_format=h.get("log_format"))
            for h in data["harnesses"]
        ],
        harness_counts=[
            HarnessCount(name=hc["name"], conversation_count=hc["conversation_count"])
            for hc in data["harness_counts"]
        ],
        top_workspaces=[
            WorkspaceStats(path=w["path"], conversation_count=w["conversation_count"], last_activity=w.get("last_activity"))
            for w in data["top_workspaces"]
        ],
        models=data["models"],
        top_tools=[ToolStats(name=t["name"], usage_count=t["usage_count"]) for t in data["top_tools"]],
        top_tags=[TagStats(name=t["name"], count=t["count"]) for t in data["top_tags"]],
        token_coverage=TokenCoverage(
            responses=tc["responses"],
            with_tokens=tc["with_tokens"],
            pct_with_tokens=tc["pct_with_tokens"],
            by_harness=[
                TokenCoverageByHarness(name=h["name"], responses=h["responses"], with_tokens=h["with_tokens"], pct_with_tokens=h["pct_with_tokens"])
                for h in tc["by_harness"]
            ],
        ),
        activity_window=(aw[0], aw[1]),
        last_ingest_at=data.get("last_ingest_at"),
    )


def write_stats_cache(stats: DatabaseStats) -> None:
    """Atomically write stats to the cache file.

    Includes db_mtime_ns for staleness detection and computed_at timestamp.
    """
    payload = {
        "_meta": {
            "computed_at": datetime.now(UTC).isoformat(),
            "db_mtime_ns": stats.db_path.stat().st_mtime_ns if stats.db_path.exists() else 0,
        },
        **_stats_to_dict(stats),
    }
    dest = stats_cache_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_stats_cache(*, db_path: Path | None = None) -> DatabaseStats | None:
    """Read cached stats if the cache exists and is fresh.

    Returns None if cache is missing, corrupt, or the db_path doesn't match.
    """
    path = stats_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Verify db_path matches (prevents stale cache from a different DB)
    effective_db = db_path or default_db_path()
    cached_db = Path(data.get("db_path", ""))
    if cached_db.resolve() != effective_db.resolve():
        return None

    return _dict_to_stats(data)


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
