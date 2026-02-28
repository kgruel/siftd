"""Tests for siftd peek command (cmd_peek)."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from siftd.cli import main
from siftd.domain.peek import SessionInfo


def _session(session_id="abc123", **kwargs):
    """Create a SessionInfo with sensible defaults."""
    defaults = {
        "file_path": Path("/tmp/fake-session.jsonl"),
        "workspace_path": "/test/project",
        "workspace_name": "project",
        "model": "claude-3",
        "last_activity": time.time(),
        "exchange_count": 5,
        "adapter_name": "claude_code",
        "parent_session_id": None,
    }
    defaults.update(kwargs)
    return SessionInfo(session_id=session_id, **defaults)


class TestPeekValidation:
    """Test flag validation without hitting the filesystem."""

    def test_last_response_and_last_prompt_exclusive(self, capsys):
        rc = main(["peek", "--last-response", "--last-prompt"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().out

    def test_follow_with_tail_exclusive(self, capsys):
        rc = main(["peek", "--follow", "--tail", "abc"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().out

    def test_follow_with_last_response_exclusive(self, capsys):
        rc = main(["peek", "--follow", "--last-response"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().out

    def test_last_response_with_json_exclusive(self, capsys):
        rc = main(["peek", "--last-response", "--json"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().out

    def test_limit_zero_rejected(self, capsys):
        rc = main(["peek", "-n", "0"])
        assert rc == 1
        assert "--limit must be at least 1" in capsys.readouterr().out

    def test_exchanges_zero_rejected(self, capsys):
        rc = main(["peek", "--exchanges", "0"])
        assert rc == 1
        assert "--exchanges must be at least 1" in capsys.readouterr().out


class TestPeekListMode:
    """Test list mode with mocked session discovery."""

    @patch("siftd.api.list_active_sessions")
    def test_no_sessions_found(self, mock_list, capsys):
        mock_list.return_value = []
        rc = main(["peek"])
        assert rc == 0
        assert "No active sessions" in capsys.readouterr().out

    @patch("siftd.api.list_active_sessions")
    def test_no_sessions_json(self, mock_list, capsys):
        mock_list.return_value = []
        rc = main(["peek", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data == []

    @patch("siftd.api.list_active_sessions")
    def test_list_sessions_text(self, mock_list, capsys):
        mock_list.return_value = [_session("abc123"), _session("def456")]
        rc = main(["peek"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "abc123" in out
        assert "def456" in out

    @patch("siftd.api.list_active_sessions")
    def test_list_sessions_json(self, mock_list, capsys):
        mock_list.return_value = [_session("abc123")]
        rc = main(["peek", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["session_id"] == "abc123"
        assert "workspace_path" in data[0]
        assert "exchange_count" in data[0]

    @patch("siftd.api.list_active_sessions")
    def test_main_only_filter(self, mock_list, capsys):
        mock_list.return_value = [
            _session("parent1"),
            _session("child1", parent_session_id="parent1"),
        ]
        rc = main(["peek", "--main-only"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "parent1" in out
        assert "child1" not in out

    @patch("siftd.api.list_active_sessions")
    def test_children_filter(self, mock_list, capsys):
        mock_list.return_value = [
            _session("parent1"),
            _session("child1", parent_session_id="parent1"),
            _session("child2", parent_session_id="parent1"),
        ]
        rc = main(["peek", "--children", "parent1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "child1" in out
        assert "child2" in out
        assert "parent1" not in out or out.count("parent1") <= 2  # Only in child lines

    @patch("siftd.api.list_active_sessions")
    def test_workspace_filter_passed_through(self, mock_list, capsys):
        mock_list.return_value = []
        main(["peek", "-w", "myproj"])
        mock_list.assert_called_once()
        call_kwargs = mock_list.call_args[1]
        assert call_kwargs["workspace"] == "myproj"

    @patch("siftd.api.list_active_sessions")
    def test_limit_passed_through(self, mock_list, capsys):
        mock_list.return_value = []
        main(["peek", "-n", "5"])
        mock_list.assert_called_once()
        call_kwargs = mock_list.call_args[1]
        assert call_kwargs["limit"] == 5

    @patch("siftd.api.list_active_sessions")
    def test_ignored_flags_warning(self, mock_list, capsys):
        mock_list.return_value = []
        main(["peek", "--tail"])
        err = capsys.readouterr().err
        assert "--tail" in err and "ignored" in err
