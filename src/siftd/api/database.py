"""Database lifecycle API for siftd.

Exposes database connection management to CLI without direct storage imports.
"""

import sqlite3
from pathlib import Path

from siftd.errors import SiftdError
from siftd.paths import db_path as _db_path
from siftd.storage.sqlite import (
    SchemaUpgradeRequiredError,
    remove_database,
)
from siftd.storage.sqlite import (
    open_database as _open_database,
)

# Re-exported via siftd.api so CLI and serve can reach these without crossing
# the cli/serve → storage boundary enforced by tests/architecture/test_imports.py.
__all__ = ["SchemaUpgradeRequiredError", "remove_database"]

# Checks run as pre-flight gates before merge/receive.
# db-blob-refcount-drift is intentionally excluded: sync slices copy ref_count
# verbatim from the source full DB, so every valid slice appears drifted.
# merge_database Step 5 recalculates ref_count post-merge regardless.
_PREFLIGHT_CHECKS = ["db-fk-integrity", "db-trigger-presence"]

_MAX_FINDINGS_IN_MSG = 3


class PreflightError(SiftdError):
    """Raised when a source database fails integrity pre-flight checks."""


def audit_db_integrity(path: Path) -> list:
    """Run structural integrity checks on a database file.

    Returns a list of Finding objects.

    Raises:
        FileNotFoundError: If ``path`` does not exist. Propagated from the
            doctor runner, which requires the DB for the structural checks.

    Creates ``-wal``/``-shm`` beside ``path`` when it is a WAL database, and
    cannot remove them: doctor's read connections do change detection, and a
    read-only connection has no way to clean up on close. This function must
    not do it either — it cannot know whether a writer is active, and unlinking
    a live ``-shm`` costs the locking coherence SQLite shares through it. A
    caller destroying an ephemeral payload calls ``remove_database`` instead,
    which owns the file and takes all three.

    Note: embed_db_path defaults to the user's local embed DB, which is
    irrelevant for source preflight. Any future deep check that reads
    embed_db_path would need to be excluded from _PREFLIGHT_CHECKS or receive
    an alternate embed path here.
    """
    from siftd.doctor.runner import run_checks

    return run_checks(
        db_path=path,
        deep=True,
        checks=_PREFLIGHT_CHECKS,
    )


def run_preflight(path: Path, label: str = "source") -> None:
    """Audit a database and raise PreflightError on error-severity findings.

    Findings are embedded in the error message so the diagnosis is self-contained
    even when the source file is ephemeral (e.g. staged inbox payloads). The path
    is always included so inbox errors are traceable.

    Warnings are logged; info findings are ignored.
    Findings describe the instant the file was sampled at.
    """
    import logging

    findings = audit_db_integrity(path)
    errors = [f for f in findings if f.severity == "error"]
    for w in [f for f in findings if f.severity == "warning"]:
        logging.getLogger(__name__).warning("Preflight [%s]: %s", label, w.message)
    if errors:
        msgs = [f.message for f in errors]
        if len(msgs) > _MAX_FINDINGS_IN_MSG:
            summary = (
                "; ".join(msgs[:_MAX_FINDINGS_IN_MSG])
                + f"; and {len(msgs) - _MAX_FINDINGS_IN_MSG} more"
            )
        else:
            summary = "; ".join(msgs)
        raise PreflightError(
            f"{label} DB failed integrity checks: {summary}. Source: {path}"
        )


def open_database(
    db_path: Path | None = None,
    *,
    read_only: bool = False,
    auto_upgrade: bool = True,
) -> sqlite3.Connection:
    """Open a database connection.

    Args:
        db_path: Path to the database file. If None, uses the default path.
        read_only: If True, open in read-only mode.
        auto_upgrade: When read_only=True and the on-disk schema is below
            SCHEMA_VERSION, run the migration in a transient write-mode open
            and reopen the RO connection against the upgraded file. Set False
            for diagnostic commands that must report the on-disk version
            without mutating it (`db schema-version`, slice source pre-check).

    Returns:
        An open sqlite3.Connection with row_factory set.

    Raises:
        FileNotFoundError: If read_only=True and database doesn't exist.
        SchemaUpgradeRequiredError: If read_only=True, schema is stale, and
            the file is not writable for an auto-upgrade.
    """
    path = db_path or _db_path()
    return _open_database(path, read_only=read_only, auto_upgrade=auto_upgrade)


def backup_database(source_path: Path, target_path: Path) -> None:
    """Create a consistent online backup using sqlite3.Connection.backup().

    The source is read through the derived read-only open, so a database on
    read-only media can be backed up.

    Args:
        source_path: Path to the source database.
        target_path: Path to write the backup. Parent directory is created if needed.

    Raises:
        FileNotFoundError: If source database does not exist.
        DriftError: If the source sits on read-only media beside a `-wal` or hot
            `-journal` holding transactions no read-only connection can replay,
            which a backup would otherwise omit without saying so.
    """
    from siftd.storage.sqlite import backup_database as _backup

    _backup(source_path, target_path)


def recreate_blob_triggers(conn: sqlite3.Connection) -> None:
    """Drop and recreate blob ref-count triggers in their current form."""
    from siftd.storage.events import ensure_event_tool_call_triggers

    ensure_event_tool_call_triggers(conn)


def create_database(db_path: Path | None = None) -> sqlite3.Connection:
    """Create or open a database, running migrations.

    Args:
        db_path: Path to the database file. If None, uses the default path.

    Returns:
        An open sqlite3.Connection with schema initialized.
    """
    path = db_path or _db_path()
    return _open_database(path)
