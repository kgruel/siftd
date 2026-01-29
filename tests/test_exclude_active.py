"""Tests for active session exclusion from search results."""

from pathlib import Path
from unittest.mock import patch

import pytest

from siftd.peek.scanner import SessionInfo
from siftd.search import get_active_conversation_ids


def _make_session_info(file_path: str, session_id: str = "test"):
    """Create a minimal SessionInfo object."""
    return SessionInfo(
        session_id=session_id,
        file_path=Path(file_path),
        last_activity=0.0,
        exchange_count=1,
    )


class TestGetActiveConversationIds:
    """Tests for get_active_conversation_ids."""

    def test_returns_conv_ids_for_active_files(self, test_db_with_ingested_files):
        """Active session file paths should map to their conversation IDs."""
        db = test_db_with_ingested_files
        active_sessions = [
            _make_session_info("/home/user/.claude/projects/abc/session-active.jsonl", "s1"),
            _make_session_info("/home/user/.claude/projects/xyz/session-active2.jsonl", "s2"),
        ]

        with patch("siftd.peek.scanner.list_active_sessions", return_value=active_sessions):
            result = get_active_conversation_ids(db["db_path"])

        assert result == {db["active_conv_id"], db["active2_conv_id"]}

    def test_excludes_inactive_files(self, test_db_with_ingested_files):
        """Files not in active sessions should not appear in results."""
        db = test_db_with_ingested_files
        active_sessions = [
            _make_session_info("/home/user/.claude/projects/abc/session-active.jsonl", "s1"),
        ]

        with patch("siftd.peek.scanner.list_active_sessions", return_value=active_sessions):
            result = get_active_conversation_ids(db["db_path"])

        assert db["inactive_conv_id"] not in result
        assert result == {db["active_conv_id"]}

    def test_returns_empty_when_no_active_sessions(self, test_db_with_ingested_files):
        """No active sessions means nothing to exclude."""
        with patch("siftd.peek.scanner.list_active_sessions", return_value=[]):
            result = get_active_conversation_ids(test_db_with_ingested_files["db_path"])

        assert result == set()

    def test_returns_empty_when_active_files_not_ingested(self, test_db_with_ingested_files):
        """Active files that haven't been ingested shouldn't match anything."""
        active_sessions = [
            _make_session_info("/home/user/.claude/projects/unknown/no-match.jsonl", "s1"),
        ]

        with patch("siftd.peek.scanner.list_active_sessions", return_value=active_sessions):
            result = get_active_conversation_ids(test_db_with_ingested_files["db_path"])

        assert result == set()

    def test_handles_scanner_exception_gracefully(self, test_db_with_ingested_files):
        """If list_active_sessions raises, return empty set instead of propagating."""
        with patch("siftd.peek.scanner.list_active_sessions", side_effect=OSError("disk error")):
            result = get_active_conversation_ids(test_db_with_ingested_files["db_path"])

        assert result == set()

    def test_handles_import_error_gracefully(self, test_db_with_ingested_files):
        """If peek module can't be imported, return empty set."""
        with patch("siftd.peek.scanner.list_active_sessions", side_effect=ImportError("no module")):
            result = get_active_conversation_ids(test_db_with_ingested_files["db_path"])

        assert result == set()
