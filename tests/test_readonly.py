"""Tests for read-only database mode."""

import os
import stat
from pathlib import Path

import pytest

from siftd.storage.sqlite import open_database


class TestReadOnlyMode:
    """Tests for open_database read_only parameter."""

    def test_read_only_succeeds_on_readonly_file(self, tmp_path):
        """read_only=True opens successfully when file is chmod read-only."""
        db_path = tmp_path / "test.db"

        # Create DB with some data
        conn = open_database(db_path)
        conn.execute("SELECT 1")
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Should succeed with read_only=True
            conn = open_database(db_path, read_only=True)
            result = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
            assert result[0] == 0
            conn.close()
        finally:
            # Restore permissions for cleanup
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    def test_read_only_false_fails_on_readonly_file(self, tmp_path):
        """read_only=False fails when file is chmod read-only."""
        db_path = tmp_path / "test.db"

        # Create DB
        conn = open_database(db_path)
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Should fail with read_only=False (default)
            with pytest.raises(Exception):
                conn = open_database(db_path, read_only=False)
                # Force a write to trigger the error (some systems may delay)
                conn.execute("INSERT INTO harnesses (id, name) VALUES ('x', 'x')")
                conn.commit()
        finally:
            # Restore permissions for cleanup
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    def test_read_only_raises_on_missing_file(self, tmp_path):
        """read_only=True raises FileNotFoundError if DB doesn't exist."""
        db_path = tmp_path / "nonexistent.db"

        with pytest.raises(FileNotFoundError, match="Database not found"):
            open_database(db_path, read_only=True)

    def test_read_only_skips_migrations(self, tmp_path):
        """read_only=True does not attempt migrations."""
        db_path = tmp_path / "test.db"

        # Create a minimal DB without running migrations
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Should open without error (no migration attempts)
            conn = open_database(db_path, read_only=True)
            # Verify we can read
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            assert "conversations" in tables
            conn.close()
        finally:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    def test_read_only_uses_uri_mode(self, tmp_path):
        """read_only=True uses URI mode with immutable flag."""
        db_path = tmp_path / "test.db"

        # Create DB
        conn = open_database(db_path)
        conn.close()

        # Open read-only and verify no WAL/SHM files created
        conn = open_database(db_path, read_only=True)
        conn.execute("SELECT 1")
        conn.close()

        # WAL and SHM files should not exist
        wal_path = tmp_path / "test.db-wal"
        shm_path = tmp_path / "test.db-shm"
        assert not wal_path.exists(), "WAL file should not be created in read-only mode"
        assert not shm_path.exists(), "SHM file should not be created in read-only mode"


class TestSearchReadOnlyMode:
    """Tests for read-only database access in search code paths."""

    def test_filter_conversations_no_wal(self, tmp_path):
        """filter_conversations() should not create WAL/SHM files."""
        from siftd.search import filter_conversations

        db_path = tmp_path / "test.db"

        # Create DB with schema
        conn = open_database(db_path)
        conn.close()

        # Call filter_conversations (triggers read-only open)
        result = filter_conversations(db_path, workspace="test")
        assert result is not None or result == set()

        # WAL and SHM files should not exist
        wal_path = tmp_path / "test.db-wal"
        shm_path = tmp_path / "test.db-shm"
        assert not wal_path.exists(), "WAL file should not be created by filter_conversations"
        assert not shm_path.exists(), "SHM file should not be created by filter_conversations"

    def test_resolve_role_ids_no_wal(self, tmp_path):
        """resolve_role_ids() should not create WAL/SHM files."""
        from siftd.search import resolve_role_ids

        db_path = tmp_path / "test.db"

        # Create DB with schema
        conn = open_database(db_path)
        conn.close()

        # Call resolve_role_ids (triggers read-only open)
        result = resolve_role_ids(db_path, "user", None)

        # WAL and SHM files should not exist
        wal_path = tmp_path / "test.db-wal"
        shm_path = tmp_path / "test.db-shm"
        assert not wal_path.exists(), "WAL file should not be created by resolve_role_ids"
        assert not shm_path.exists(), "SHM file should not be created by resolve_role_ids"

    def test_get_active_conversation_ids_no_wal(self, tmp_path):
        """get_active_conversation_ids() should not create WAL/SHM files."""
        from siftd.search import get_active_conversation_ids

        db_path = tmp_path / "test.db"

        # Create DB with schema
        conn = open_database(db_path)
        conn.close()

        # Call get_active_conversation_ids (triggers read-only open if sessions found)
        result = get_active_conversation_ids(db_path)
        assert isinstance(result, set)

        # WAL and SHM files should not exist
        wal_path = tmp_path / "test.db-wal"
        shm_path = tmp_path / "test.db-shm"
        assert not wal_path.exists(), "WAL file should not be created by get_active_conversation_ids"
        assert not shm_path.exists(), "SHM file should not be created by get_active_conversation_ids"

    def test_filter_conversations_works_on_readonly_file(self, tmp_path):
        """filter_conversations() works when DB file is chmod read-only."""
        from siftd.search import filter_conversations

        db_path = tmp_path / "test.db"

        # Create DB with schema and some data
        conn = open_database(db_path)
        conn.execute(
            "INSERT INTO harnesses (id, name) VALUES (?, ?)",
            ("h1", "test_harness"),
        )
        conn.execute(
            "INSERT INTO workspaces (id, path, discovered_at) VALUES (?, ?, ?)",
            ("ws1", "/path/to/project", "2024-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at) VALUES (?, ?, ?, ?, ?)",
            ("conv1", "ext1", "h1", "ws1", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Should succeed on read-only file
            result = filter_conversations(db_path, workspace="project")
            assert result == {"conv1"}
        finally:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    def test_resolve_role_ids_works_on_readonly_file(self, tmp_path):
        """resolve_role_ids() works when DB file is chmod read-only."""
        from siftd.search import resolve_role_ids

        db_path = tmp_path / "test.db"

        # Create DB with schema and some data
        conn = open_database(db_path)
        conn.execute(
            "INSERT INTO harnesses (id, name) VALUES (?, ?)",
            ("h1", "test_harness"),
        )
        conn.execute(
            "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?, ?, ?, ?)",
            ("conv1", "ext1", "h1", "2024-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO prompts (id, conversation_id, timestamp) VALUES (?, ?, ?)",
            ("prompt1", "conv1", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Should succeed on read-only file
            result = resolve_role_ids(db_path, "user", {"conv1"})
            assert result == {"prompt1"}
        finally:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
