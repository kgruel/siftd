"""Migration API wrappers for workspace identity maintenance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path


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


def workspace_duplicate_count(db_path: Path | None = None) -> tuple[int, int]:
    """Count legacy duplicate-workspace groups (workspaces sharing a git remote).

    Returns ``(groups, extra_rows)`` — the same shape the ambient
    ``workspace-duplicates`` caveat reports — so a read surface can advertise the
    condition and the ``siftd migrate --merge-workspaces`` remediation without
    re-implementing the detection. Count-only by design: it leaks no path or
    remote, so it is safe to surface regardless of tenant. ``(0, 0)`` when clean
    or the DB is absent.
    """
    from siftd.paths import db_path as default_db_path
    from siftd.storage.migrate_workspaces import find_duplicate_workspaces
    from siftd.storage.sqlite import open_database

    path = db_path or default_db_path()
    if not Path(path).exists():
        return (0, 0)
    conn = open_database(path, read_only=True)
    try:
        groups = find_duplicate_workspaces(conn)
    finally:
        conn.close()
    extras = sum(len(g["workspace_ids"]) - 1 for g in groups)
    return (len(groups), extras)


def get_schema_version_info(current_version: int) -> dict:
    """Return schema version triage info for the given current DB version.

    Returns a dict with keys: current_version, target_version, applied,
    pending, all_migrations.  ``pending`` is non-empty when the DB needs
    migrations; ``current_version > target_version`` indicates the DB was
    created by a newer version of siftd.
    """
    from siftd.storage.sqlite import MIGRATIONS, SCHEMA_VERSION

    all_migrations = sorted(MIGRATIONS.keys())
    applied = [v for v in all_migrations if v <= current_version]
    pending = [v for v in all_migrations if v > current_version]
    return {
        "current_version": current_version,
        "target_version": SCHEMA_VERSION,
        "applied": applied,
        "pending": pending,
        "all_migrations": all_migrations,
    }
