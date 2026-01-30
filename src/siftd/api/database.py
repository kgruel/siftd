"""Database lifecycle API for siftd.

Exposes database connection management to CLI without direct storage imports.
"""

import sqlite3
from pathlib import Path

from siftd.paths import db_path as _db_path
from siftd.storage.sqlite import open_database as _open_database


def open_database(db_path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a database connection.

    Args:
        db_path: Path to the database file. If None, uses the default path.
        read_only: If True, open in read-only mode (no migrations).

    Returns:
        An open sqlite3.Connection with row_factory set.

    Raises:
        FileNotFoundError: If read_only=True and database doesn't exist.
    """
    path = db_path or _db_path()
    return _open_database(path, read_only=read_only)


def create_database(db_path: Path | None = None) -> sqlite3.Connection:
    """Create or open a database, running migrations.

    Args:
        db_path: Path to the database file. If None, uses the default path.

    Returns:
        An open sqlite3.Connection with schema initialized.
    """
    path = db_path or _db_path()
    return _open_database(path)
