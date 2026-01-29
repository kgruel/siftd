"""Tool usage statistics API."""

from dataclasses import dataclass
from pathlib import Path

from siftd.paths import db_path as default_db_path
from siftd.storage.sqlite import open_database


@dataclass
class TagUsage:
    """Tag with usage count."""

    name: str
    count: int


@dataclass
class WorkspaceTagUsage:
    """Per-workspace breakdown of tool tag usage."""

    workspace: str
    tags: list[TagUsage]
    total: int


def get_tool_tag_summary(
    *,
    db_path: Path | None = None,
    prefix: str = "shell:",
) -> list[TagUsage]:
    """Get summary of tool call tags by category.

    Args:
        db_path: Path to database. Uses default if not specified.
        prefix: Tag prefix to filter by (default: "shell:").

    Returns:
        List of TagUsage sorted by count descending.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)

    rows = conn.execute(
        """
        SELECT t.name, COUNT(tct.id) as count
        FROM tags t
        JOIN tool_call_tags tct ON tct.tag_id = t.id
        WHERE t.name LIKE ?
        GROUP BY t.id
        ORDER BY count DESC
        """,
        (f"{prefix}%",),
    ).fetchall()

    conn.close()

    return [TagUsage(name=row["name"], count=row["count"]) for row in rows]


def get_tool_tags_by_workspace(
    *,
    db_path: Path | None = None,
    prefix: str = "shell:",
    limit: int = 20,
) -> list[WorkspaceTagUsage]:
    """Get tool tag usage broken down by workspace.

    Args:
        db_path: Path to database. Uses default if not specified.
        prefix: Tag prefix to filter by (default: "shell:").
        limit: Maximum workspaces to return.

    Returns:
        List of WorkspaceTagUsage sorted by total count descending.

    Raises:
        FileNotFoundError: If database does not exist.
    """
    db = db_path or default_db_path()

    if not db.exists():
        raise FileNotFoundError(f"Database not found: {db}")

    conn = open_database(db, read_only=True)

    # Get per-workspace, per-tag counts
    rows = conn.execute(
        """
        SELECT
            COALESCE(w.path, '(no workspace)') as workspace,
            t.name as tag,
            COUNT(tct.id) as count
        FROM tool_call_tags tct
        JOIN tags t ON t.id = tct.tag_id
        JOIN tool_calls tc ON tc.id = tct.tool_call_id
        JOIN conversations c ON c.id = tc.conversation_id
        LEFT JOIN workspaces w ON w.id = c.workspace_id
        WHERE t.name LIKE ?
        GROUP BY w.id, t.id
        ORDER BY workspace, count DESC
        """,
        (f"{prefix}%",),
    ).fetchall()

    conn.close()

    # Aggregate by workspace
    by_workspace: dict[str, list[TagUsage]] = {}
    for row in rows:
        ws = row["workspace"]
        if ws not in by_workspace:
            by_workspace[ws] = []
        by_workspace[ws].append(TagUsage(name=row["tag"], count=row["count"]))

    # Build result sorted by total
    results = []
    for ws, tags in by_workspace.items():
        total = sum(t.count for t in tags)
        results.append(WorkspaceTagUsage(workspace=ws, tags=tags, total=total))

    results.sort(key=lambda x: x.total, reverse=True)
    return results[:limit]
