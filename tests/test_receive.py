"""Tests for siftd db receive — create-or-merge from a source database."""

import io
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from conftest import make_db as _make_db

from siftd.api.receive import receive_database
from siftd.cli import main


class TestReceiveDatabase:
    def test_first_receive_creates_db(self, tmp_path):
        """Receiving into a nonexistent target creates it from source."""
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}, {"external_id": "conv-B"}],
        )
        target = tmp_path / "target" / "team.db"

        result = receive_database(source, target)

        assert result["status"] == "created"
        assert result["conversations"] == 2
        assert target.exists()

        conn = sqlite3.connect(str(target))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 2

    def test_subsequent_receive_merges(self, tmp_path):
        """Receiving into an existing target merges the source."""
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "target.db"
        _make_db(target, conversations=[{"external_id": "conv-B"}])

        result = receive_database(source, target)

        assert result["status"] == "merged"
        assert result["conversations"] == 1  # 1 new conversation merged

        conn = sqlite3.connect(str(target))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 2

    def test_invalid_sqlite_raises(self, tmp_path):
        """Non-SQLite source raises ValueError."""
        bad_file = tmp_path / "not-sqlite.db"
        bad_file.write_bytes(b"this is not a database")
        target = tmp_path / "target.db"

        with pytest.raises(ValueError, match="Not a valid SQLite database"):
            receive_database(bad_file, target)

    def test_empty_source_raises(self, tmp_path):
        """Empty file raises ValueError."""
        empty_file = tmp_path / "empty.db"
        empty_file.write_bytes(b"")
        target = tmp_path / "target.db"

        with pytest.raises(ValueError, match="Not a valid SQLite database"):
            receive_database(empty_file, target)

    def test_missing_source_raises(self, tmp_path):
        """Nonexistent source raises FileNotFoundError."""
        target = tmp_path / "target.db"

        with pytest.raises(FileNotFoundError, match="Source not found"):
            receive_database(tmp_path / "nope.db", target)

    def test_creates_parent_directories(self, tmp_path):
        """Receive creates nested parent directories for the target."""
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "deep" / "nested" / "path" / "team.db"

        result = receive_database(source, target)

        assert result["status"] == "created"
        assert target.exists()

    def test_merge_idempotent(self, tmp_path):
        """Receiving the same data twice doesn't create duplicates."""
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}, {"external_id": "conv-B"}],
        )
        target = tmp_path / "target.db"

        # First receive creates
        result1 = receive_database(source, target)
        assert result1["status"] == "created"
        assert result1["conversations"] == 2

        # Second receive merges (idempotent)
        result2 = receive_database(source, target)
        assert result2["status"] == "merged"
        assert result2["conversations"] == 0  # no new conversations

        conn = sqlite3.connect(str(target))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 2

    def test_fk_integrity_after_create(self, tmp_path):
        """Created target passes FK check."""
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "target.db"

        receive_database(source, target)

        conn = sqlite3.connect(str(target))
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert violations == []

    def test_fk_integrity_after_merge(self, tmp_path):
        """Merged target passes FK check."""
        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "target.db"
        _make_db(target, conversations=[{"external_id": "conv-B"}])

        receive_database(source, target)

        conn = sqlite3.connect(str(target))
        conn.execute("PRAGMA foreign_keys = ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert violations == []


class TestReceiveCLIErrors:
    def test_operational_error_returns_json(self, tmp_path, monkeypatch, capsys):
        """OperationalError from receive surfaces as structured JSON error."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "target.db"

        with open(source, "rb") as f:
            data = f.read()
        mock_stdin = io.BytesIO(data)
        mock_stdin_wrapper = MagicMock()
        mock_stdin_wrapper.buffer = mock_stdin
        mock_stdin_wrapper.isatty = MagicMock(return_value=False)

        with (
            patch("siftd.cli_db.sys.stdin", mock_stdin_wrapper),
            patch(
                "siftd.api.receive.receive_database",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
        ):
            rc = main(["--db", str(target), "db", "receive"])

        assert rc == 1
        err = capsys.readouterr().err
        error = json.loads(err)
        assert error["error"] == "database is locked"
        assert error["error_type"] == "database_locked"

    def test_operational_error_generic(self, tmp_path, monkeypatch, capsys):
        """Non-locked OperationalError gets sqlite_error type."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

        source = _make_db(
            tmp_path / "source.db",
            conversations=[{"external_id": "conv-A"}],
        )
        target = tmp_path / "target.db"

        with open(source, "rb") as f:
            data = f.read()
        mock_stdin = io.BytesIO(data)
        mock_stdin_wrapper = MagicMock()
        mock_stdin_wrapper.buffer = mock_stdin
        mock_stdin_wrapper.isatty = MagicMock(return_value=False)

        with (
            patch("siftd.cli_db.sys.stdin", mock_stdin_wrapper),
            patch(
                "siftd.api.receive.receive_database",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ),
        ):
            rc = main(["--db", str(target), "db", "receive"])

        assert rc == 1
        err = capsys.readouterr().err
        error = json.loads(err)
        assert error["error"] == "disk I/O error"
        assert error["error_type"] == "sqlite_error"
