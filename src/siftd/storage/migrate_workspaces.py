"""Migration script for workspace git remote identity.

Backfills git_remote column for existing workspaces and optionally merges
duplicates that share the same git remote URL.

Usage:
    from siftd.storage.migrate_workspaces import (
        backfill_git_remotes,
        merge_duplicate_workspaces,
    )
    backfill_git_remotes(conn, on_progress=print)
    merge_duplicate_workspaces(conn, on_progress=print)
"""

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from siftd.git import get_git_remote_url
from siftd.ids import ulid as _ulid


def count_workspaces_without_remote(conn: sqlite3.Connection) -> dict:
    """Count workspaces that need git_remote backfill.

    Returns:
        dict with:
            - total: total workspace count
            - without_remote: workspaces with NULL git_remote
            - with_remote: workspaces with git_remote populated
    """
    cur = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN git_remote IS NULL THEN 1 ELSE 0 END) as without_remote,
            SUM(CASE WHEN git_remote IS NOT NULL THEN 1 ELSE 0 END) as with_remote
        FROM workspaces
    """)
    row = cur.fetchone()
    return {
        "total": row[0] or 0,
        "without_remote": row[1] or 0,
        "with_remote": row[2] or 0,
    }


def backfill_git_remotes(
    conn: sqlite3.Connection,
    *,
    on_progress: Callable[[str], None] | None = None,
    dry_run: bool = False,
) -> dict:
    """Backfill git_remote for existing workspaces.

    For each workspace without a git_remote, attempts to resolve it from
    the filesystem path. Paths that no longer exist or aren't git repos
    are skipped.

    Args:
        conn: Database connection
        on_progress: Optional callback for progress messages
        dry_run: If True, report what would happen without making changes

    Returns:
        dict with:
            - checked: number of workspaces checked
            - updated: number of workspaces updated with git_remote
            - skipped_missing: paths that no longer exist
            - skipped_no_git: paths without git remote
    """
    stats = {
        "checked": 0,
        "updated": 0,
        "skipped_missing": 0,
        "skipped_no_git": 0,
    }

    cur = conn.execute(
        "SELECT id, path FROM workspaces WHERE git_remote IS NULL"
    )
    rows = cur.fetchall()

    for row in rows:
        workspace_id = row["id"]
        path = row["path"]
        stats["checked"] += 1

        # Check if path still exists
        if not Path(path).exists():
            stats["skipped_missing"] += 1
            if on_progress:
                on_progress(f"  [skip] {path} (path no longer exists)")
            continue

        # Try to get git remote
        git_remote = get_git_remote_url(path)
        if not git_remote:
            stats["skipped_no_git"] += 1
            if on_progress:
                on_progress(f"  [skip] {path} (no git remote)")
            continue

        if not dry_run:
            # Update workspace with git remote
            conn.execute(
                "UPDATE workspaces SET git_remote = ? WHERE id = ?",
                (git_remote, workspace_id)
            )
        stats["updated"] += 1
        if on_progress:
            on_progress(f"  [updated] {path} -> {git_remote}")

    if not dry_run:
        conn.commit()
    return stats


def find_duplicate_workspaces(conn: sqlite3.Connection) -> list[dict]:
    """Find workspaces that share the same git_remote.

    Returns:
        List of dicts with:
            - git_remote: the shared remote URL
            - workspace_ids: list of workspace IDs (deterministic order)
            - workspace_paths: list of workspace paths (matching order)
    """
    from itertools import groupby

    # First find which remotes have duplicates
    cur = conn.execute("""
        SELECT git_remote
        FROM workspaces
        WHERE git_remote IS NOT NULL
        GROUP BY git_remote
        HAVING COUNT(*) > 1
    """)
    duplicate_remotes = [row["git_remote"] for row in cur.fetchall()]

    if not duplicate_remotes:
        return []

    # Fetch all workspaces for those remotes, ordered for determinism
    placeholders = ",".join("?" * len(duplicate_remotes))
    cur = conn.execute(f"""
        SELECT id, path, git_remote
        FROM workspaces
        WHERE git_remote IN ({placeholders})
        ORDER BY git_remote, id
    """, duplicate_remotes)

    # Group in Python to avoid GROUP_CONCAT ordering issues
    duplicates = []
    for git_remote, group in groupby(cur.fetchall(), key=lambda r: r["git_remote"]):
        rows = list(group)
        duplicates.append({
            "git_remote": git_remote,
            "workspace_ids": [r["id"] for r in rows],
            "workspace_paths": [r["path"] for r in rows],
        })
    return duplicates


def merge_duplicate_workspaces(
    conn: sqlite3.Connection,
    *,
    on_progress: Callable[[str], None] | None = None,
    dry_run: bool = False,
) -> dict:
    """Merge workspaces that share the same git_remote.

    For each group of duplicate workspaces:
    1. Keeps the workspace with the most conversations as the "keeper"
    2. Re-points all conversations from duplicates to the keeper
    3. Deletes the duplicate workspace records

    Args:
        conn: Database connection
        on_progress: Optional callback for progress messages
        dry_run: If True, report what would happen without making changes

    Returns:
        dict with:
            - groups_found: number of duplicate groups
            - workspaces_merged: number of duplicate workspaces removed
            - conversations_moved: number of conversations re-pointed
    """
    stats = {
        "groups_found": 0,
        "workspaces_merged": 0,
        "conversations_moved": 0,
    }

    duplicates = find_duplicate_workspaces(conn)
    stats["groups_found"] = len(duplicates)

    for dup in duplicates:
        ids = dup["workspace_ids"]
        paths = dup["workspace_paths"]
        git_remote = dup["git_remote"]

        # Find keeper (workspace with most conversations)
        counts = conn.execute("""
            SELECT workspace_id, COUNT(*) as cnt
            FROM conversations
            WHERE workspace_id IN ({})
            GROUP BY workspace_id
            ORDER BY cnt DESC
        """.format(",".join("?" * len(ids))), ids).fetchall()

        # Keeper is the one with most conversations, or first ID if no conversations
        if counts:
            keeper_id = counts[0]["workspace_id"]
        else:
            keeper_id = ids[0]

        other_ids = [id for id in ids if id != keeper_id]
        keeper_path = paths[ids.index(keeper_id)]

        if on_progress:
            on_progress(f"\nMerging {len(other_ids) + 1} workspaces for {git_remote}")
            on_progress(f"  Keeper: {keeper_path}")

        for other_id in other_ids:
            other_path = paths[ids.index(other_id)]
            if on_progress:
                on_progress(f"  Merging: {other_path}")

            if not dry_run:
                # Count conversations to move
                cur = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE workspace_id = ?",
                    (other_id,)
                )
                move_count = cur.fetchone()[0]
                stats["conversations_moved"] += move_count

                # Re-point conversations
                conn.execute(
                    "UPDATE conversations SET workspace_id = ? WHERE workspace_id = ?",
                    (keeper_id, other_id)
                )

                # Migrate workspace_tags to keeper (ignore duplicates)
                # Must provide id and applied_at for each row
                now = datetime.now().isoformat()
                cur = conn.execute(
                    "SELECT tag_id FROM workspace_tags WHERE workspace_id = ?",
                    (other_id,)
                )
                for tag_row in cur.fetchall():
                    conn.execute("""
                        INSERT OR IGNORE INTO workspace_tags (id, workspace_id, tag_id, applied_at)
                        VALUES (?, ?, ?, ?)
                    """, (_ulid(), keeper_id, tag_row["tag_id"], now))

                # Delete the duplicate workspace (will cascade-delete its workspace_tags)
                conn.execute("DELETE FROM workspaces WHERE id = ?", (other_id,))

            stats["workspaces_merged"] += 1

    if not dry_run:
        conn.commit()

    return stats


def verify_workspace_identity(conn: sqlite3.Connection) -> dict:
    """Verify workspace identity migration status.

    Returns:
        dict with:
            - total: total workspace count
            - with_remote: workspaces with git_remote set
            - without_remote: workspaces without git_remote
            - duplicate_groups: number of groups sharing same git_remote
            - duplicate_workspaces: total workspaces in duplicate groups
    """
    counts = count_workspaces_without_remote(conn)
    duplicates = find_duplicate_workspaces(conn)

    duplicate_workspace_count = sum(
        len(d["workspace_ids"]) for d in duplicates
    )

    return {
        "total": counts["total"],
        "with_remote": counts["with_remote"],
        "without_remote": counts["without_remote"],
        "duplicate_groups": len(duplicates),
        "duplicate_workspaces": duplicate_workspace_count,
    }
