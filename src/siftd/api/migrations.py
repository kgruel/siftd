"""Migration API wrappers for workspace identity maintenance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable


def backfill_git_remotes(
    conn: sqlite3.Connection,
    *,
    on_progress: Callable[[str], None] | None = None,
    dry_run: bool = False,
) -> dict:
    """Backfill git remote URLs for existing workspaces."""
    from siftd.storage.migrate_workspaces import backfill_git_remotes as _backfill_git_remotes

    return _backfill_git_remotes(conn, on_progress=on_progress, dry_run=dry_run)


def merge_duplicate_workspaces(
    conn: sqlite3.Connection,
    *,
    on_progress: Callable[[str], None] | None = None,
    dry_run: bool = False,
) -> dict:
    """Merge workspaces that share the same git remote URL."""
    from siftd.storage.migrate_workspaces import merge_duplicate_workspaces as _merge_duplicate_workspaces

    return _merge_duplicate_workspaces(conn, on_progress=on_progress, dry_run=dry_run)


def verify_workspace_identity(conn: sqlite3.Connection) -> dict:
    """Verify workspace identity migration status."""
    from siftd.storage.migrate_workspaces import verify_workspace_identity as _verify_workspace_identity

    return _verify_workspace_identity(conn)
