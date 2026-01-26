"""Tests for active session exclusion from search results."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from strata.search import get_active_conversation_ids
from strata.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    record_ingested_file,
)


@pytest.fixture
def test_db(tmp_path):
    """Create a test database with conversations linked to ingested files."""
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "claude_code", source="local", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")

    # Conversation from an active session file
    active_conv_id = insert_conversation(
        conn,
        external_id="active-conv",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )
    insert_prompt(conn, active_conv_id, "p1", "2024-01-15T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/abc/session-active.jsonl",
        "hash_active",
        active_conv_id,
    )

    # Conversation from an inactive (old) session file
    inactive_conv_id = insert_conversation(
        conn,
        external_id="inactive-conv",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-14T10:00:00Z",
    )
    insert_prompt(conn, inactive_conv_id, "p2", "2024-01-14T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/abc/session-old.jsonl",
        "hash_old",
        inactive_conv_id,
    )

    # Conversation from another active session file
    active2_conv_id = insert_conversation(
        conn,
        external_id="active-conv-2",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-16T10:00:00Z",
    )
    insert_prompt(conn, active2_conv_id, "p3", "2024-01-16T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/xyz/session-active2.jsonl",
        "hash_active2",
        active2_conv_id,
    )

    conn.commit()
    conn.close()

    return {
        "db_path": db_path,
        "active_conv_id": active_conv_id,
        "inactive_conv_id": inactive_conv_id,
        "active2_conv_id": active2_conv_id,
    }


def _make_session_info(file_path: str, session_id: str = "test"):
    """Create a minimal SessionInfo-like object."""
    from strata.peek.scanner import SessionInfo

    return SessionInfo(
        session_id=session_id,
        file_path=Path(file_path),
        last_activity=0.0,
        exchange_count=1,
    )


class TestGetActiveConversationIds:
    """Tests for get_active_conversation_ids."""

    def test_returns_conv_ids_for_active_files(self, test_db):
        """Active session file paths should map to their conversation IDs."""
        active_sessions = [
            _make_session_info("/home/user/.claude/projects/abc/session-active.jsonl", "s1"),
            _make_session_info("/home/user/.claude/projects/xyz/session-active2.jsonl", "s2"),
        ]

        with patch("strata.peek.scanner.list_active_sessions", return_value=active_sessions):
            result = get_active_conversation_ids(test_db["db_path"])

        assert result == {test_db["active_conv_id"], test_db["active2_conv_id"]}

    def test_excludes_inactive_files(self, test_db):
        """Files not in active sessions should not appear in results."""
        active_sessions = [
            _make_session_info("/home/user/.claude/projects/abc/session-active.jsonl", "s1"),
        ]

        with patch("strata.peek.scanner.list_active_sessions", return_value=active_sessions):
            result = get_active_conversation_ids(test_db["db_path"])

        assert test_db["inactive_conv_id"] not in result
        assert result == {test_db["active_conv_id"]}

    def test_returns_empty_when_no_active_sessions(self, test_db):
        """No active sessions means nothing to exclude."""
        with patch("strata.peek.scanner.list_active_sessions", return_value=[]):
            result = get_active_conversation_ids(test_db["db_path"])

        assert result == set()

    def test_returns_empty_when_active_files_not_ingested(self, test_db):
        """Active files that haven't been ingested shouldn't match anything."""
        active_sessions = [
            _make_session_info("/home/user/.claude/projects/unknown/no-match.jsonl", "s1"),
        ]

        with patch("strata.peek.scanner.list_active_sessions", return_value=active_sessions):
            result = get_active_conversation_ids(test_db["db_path"])

        assert result == set()

    def test_handles_scanner_exception_gracefully(self, test_db):
        """If list_active_sessions raises, return empty set instead of propagating."""
        with patch("strata.peek.scanner.list_active_sessions", side_effect=OSError("disk error")):
            result = get_active_conversation_ids(test_db["db_path"])

        assert result == set()

    def test_handles_import_error_gracefully(self, test_db):
        """If peek module can't be imported, return empty set."""
        with patch("strata.peek.scanner.list_active_sessions", side_effect=ImportError("no module")):
            # The function catches ImportError internally during import,
            # but since we patch at module level, this tests a different path.
            # The actual ImportError guard is in the function body.
            result = get_active_conversation_ids(test_db["db_path"])

        # With the mock raising ImportError on call, the except Exception catches it
        assert result == set()
