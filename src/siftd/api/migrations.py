"""Migration API wrappers for workspace identity maintenance."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from siftd.domain.progress import ProgressSink


def _line_shim(
    group: str, on_progress: ProgressSink | None
) -> Callable[[str], None] | None:
    """Adapt the storage layer's per-row ``str`` callback to a ``ProgressSink``.

    The storage functions (``storage.migrate_workspaces``) emit one line of
    sub-step detail per workspace row — a stable, well-tested detail contract.
    This lifts each line into a ``ProgressEvent`` on ``group`` so the API speaks
    the one progress contract (per the dissolution ledger) while leaving storage
    untouched. Returns ``None`` when there's no sink, so storage skips the
    callback entirely (it already guards ``if on_progress``).
    """
    if on_progress is None:
        return None

    from siftd.domain.progress import ProgressEvent

    def emit(line: str) -> None:
        on_progress(ProgressEvent(group=group, message=line, status="progress"))

    return emit


def backfill_git_remotes(
    conn: sqlite3.Connection,
    *,
    on_progress: ProgressSink | None = None,
    group: str = "backfill git remotes",
    dry_run: bool = False,
) -> dict:
    """Backfill git remote URLs for existing workspaces.

    ``on_progress`` receives a ``ProgressEvent`` per workspace row (the storage
    line lifted onto ``group``); ``group`` names the step for the live consumer.
    """
    from siftd.storage.migrate_workspaces import backfill_git_remotes as _backfill_git_remotes

    return _backfill_git_remotes(
        conn, on_progress=_line_shim(group, on_progress), dry_run=dry_run
    )


def merge_duplicate_workspaces(
    conn: sqlite3.Connection,
    *,
    on_progress: ProgressSink | None = None,
    group: str = "merge workspaces",
    dry_run: bool = False,
) -> dict:
    """Merge workspaces that share the same git remote URL.

    ``on_progress`` receives a ``ProgressEvent`` per merge line (lifted onto
    ``group``); ``group`` names the step for the live consumer.
    """
    from siftd.storage.migrate_workspaces import merge_duplicate_workspaces as _merge_duplicate_workspaces

    return _merge_duplicate_workspaces(
        conn, on_progress=_line_shim(group, on_progress), dry_run=dry_run
    )


def verify_workspace_identity(conn: sqlite3.Connection) -> dict:
    """Verify workspace identity migration status."""
    from siftd.storage.migrate_workspaces import verify_workspace_identity as _verify_workspace_identity

    return _verify_workspace_identity(conn)


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
