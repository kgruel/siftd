"""Tests for the peek module."""

import json
import time
from pathlib import Path

import pytest

from siftd.peek.reader import (
    PeekExchange,
    SessionDetail,
    find_session_file,
    read_session_detail,
    tail_session,
)
from siftd.peek.scanner import (
    SessionInfo,
    _scan_session_file,
    list_active_sessions,
)


def _write_session(path: Path, records: list[dict]) -> None:
    """Write a list of records as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _make_user_record(text: str, *, cwd: str = "/test/project", session_id: str = "test-session", timestamp: str = "2025-01-20T10:00:00Z") -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _make_assistant_record(text: str, *, model: str = "claude-opus-4-5-20251101", timestamp: str = "2025-01-20T10:01:00Z", input_tokens: int = 100, output_tokens: int = 50, tool_uses: list[dict] | None = None) -> dict:
    content = [{"type": "text", "text": text}]
    if tool_uses:
        content.extend(tool_uses)
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    }


def _make_tool_result_record(tool_use_id: str, content: str = "ok", *, timestamp: str = "2025-01-20T10:01:30Z") -> dict:
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content},
            ],
        },
    }


@pytest.fixture
def session_dir(tmp_path):
    """Create a temporary directory mimicking Claude Code project structure."""
    project_dir = tmp_path / "projects" / "-test-project"
    project_dir.mkdir(parents=True)
    return project_dir


@pytest.fixture
def sample_session(session_dir):
    """Create a sample session file with multiple exchanges."""
    session_file = session_dir / "abc12345-6789-4def-abcd-ef0123456789.jsonl"
    records = [
        _make_user_record("Hello", timestamp="2025-01-20T10:00:00Z"),
        _make_assistant_record("Hi there!", timestamp="2025-01-20T10:00:05Z"),
        _make_user_record("Add a feature", timestamp="2025-01-20T10:01:00Z", cwd="/test/project"),
        _make_assistant_record(
            "I'll add that feature.",
            timestamp="2025-01-20T10:01:10Z",
            input_tokens=500,
            output_tokens=200,
            tool_uses=[
                {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"path": "/test/file.py"}},
                {"type": "tool_use", "id": "tu2", "name": "Write", "input": {"path": "/test/file.py"}},
            ],
        ),
        _make_tool_result_record("tu1", timestamp="2025-01-20T10:01:20Z"),
        _make_tool_result_record("tu2", timestamp="2025-01-20T10:01:25Z"),
        _make_user_record("Run tests", timestamp="2025-01-20T10:02:00Z"),
        _make_assistant_record(
            "Running tests...",
            timestamp="2025-01-20T10:02:10Z",
            input_tokens=300,
            output_tokens=100,
            tool_uses=[
                {"type": "tool_use", "id": "tu3", "name": "Bash", "input": {"command": "pytest"}},
            ],
        ),
        _make_tool_result_record("tu3", "All tests passed", timestamp="2025-01-20T10:02:30Z"),
    ]
    _write_session(session_file, records)
    return session_file


class TestScanSessionFile:
    """Tests for _scan_session_file."""

    def test_extracts_metadata(self, sample_session):
        info = _scan_session_file(sample_session, sample_session.stat().st_mtime)
        assert info is not None
        assert info.session_id == "test-session"
        assert info.workspace_path == "/test/project"
        assert info.workspace_name == "project"
        assert info.model == "claude-opus-4-5-20251101"
        assert info.exchange_count == 3

    def test_empty_file_returns_none(self, session_dir):
        empty = session_dir / "empty.jsonl"
        empty.write_text("")
        assert _scan_session_file(empty, time.time()) is None

    def test_no_user_records_returns_none(self, session_dir):
        path = session_dir / "no-users.jsonl"
        records = [_make_assistant_record("Solo response")]
        _write_session(path, records)
        assert _scan_session_file(path, time.time()) is None

    def test_handles_malformed_json(self, session_dir):
        path = session_dir / "bad.jsonl"
        path.write_text('{"type":"user","cwd":"/test","sessionId":"s1","message":{"role":"user","content":"hi"}}\n{broken json\n')
        info = _scan_session_file(path, time.time())
        assert info is not None
        assert info.exchange_count == 1

    def test_session_id_from_filename_when_no_record_id(self, session_dir):
        path = session_dir / "file-uuid-1234.jsonl"
        record = {
            "type": "user",
            "cwd": "/test",
            "message": {"role": "user", "content": "hi"},
        }
        _write_session(path, [record])
        info = _scan_session_file(path, time.time())
        assert info is not None
        assert info.session_id == "file-uuid-1234"


class TestReadSessionDetail:
    """Tests for read_session_detail."""

    def test_returns_detail_with_exchanges(self, sample_session):
        detail = read_session_detail(sample_session, last_n=10)
        assert detail is not None
        assert isinstance(detail, SessionDetail)
        assert len(detail.exchanges) == 3
        assert detail.started_at == "2025-01-20T10:00:00Z"

    def test_last_n_limits_exchanges(self, sample_session):
        detail = read_session_detail(sample_session, last_n=2)
        assert detail is not None
        assert len(detail.exchanges) == 2
        # Should be the last 2 exchanges
        assert detail.exchanges[0].prompt_text == "Add a feature"
        assert detail.exchanges[1].prompt_text == "Run tests"

    def test_exchange_has_prompt_and_response(self, sample_session):
        detail = read_session_detail(sample_session, last_n=10)
        first = detail.exchanges[0]
        assert first.prompt_text == "Hello"
        assert first.response_text == "Hi there!"

    def test_exchange_tracks_tokens(self, sample_session):
        detail = read_session_detail(sample_session, last_n=10)
        second = detail.exchanges[1]
        assert second.input_tokens == 500
        assert second.output_tokens == 200

    def test_exchange_tracks_tool_calls(self, sample_session):
        detail = read_session_detail(sample_session, last_n=10)
        second = detail.exchanges[1]
        # Should have Read x1, Write x1
        tool_names = {name for name, _ in second.tool_calls}
        assert "Read" in tool_names
        assert "Write" in tool_names

    def test_tool_result_not_counted_as_exchange(self, sample_session):
        """Tool result messages should not create new exchanges."""
        detail = read_session_detail(sample_session, last_n=10)
        # 3 user prompts, not 6 (3 prompts + 3 tool results)
        assert len(detail.exchanges) == 3

    def test_nonexistent_file(self, session_dir):
        path = session_dir / "nonexistent.jsonl"
        with pytest.raises(FileNotFoundError):
            read_session_detail(path)

    def test_info_populated(self, sample_session):
        detail = read_session_detail(sample_session, last_n=10)
        assert detail.info.session_id == "test-session"
        assert detail.info.workspace_name == "project"
        assert detail.info.exchange_count == 3


class TestTailSession:
    """Tests for tail_session."""

    def test_returns_formatted_lines(self, sample_session):
        lines = tail_session(sample_session, lines=3)
        assert len(lines) == 3
        # Each line should be valid JSON (pretty-printed)
        for line in lines:
            json.loads(line)

    def test_limits_to_requested_count(self, sample_session):
        lines = tail_session(sample_session, lines=2)
        assert len(lines) == 2

    def test_all_lines_when_file_small(self, session_dir):
        path = session_dir / "small.jsonl"
        _write_session(path, [_make_user_record("Hello")])
        lines = tail_session(path, lines=100)
        assert len(lines) == 1

    def test_handles_empty_file(self, session_dir):
        path = session_dir / "empty.jsonl"
        path.write_text("")
        lines = tail_session(path, lines=5)
        assert lines == []


class TestFindSessionFile:
    """Tests for find_session_file."""

    def test_finds_by_prefix(self, sample_session, monkeypatch):
        """find_session_file locates a file by session ID prefix."""
        # Mock load_all_adapters to return a fake adapter pointing to our tmp dir
        class FakeAdapter:
            DEFAULT_LOCATIONS = [str(sample_session.parent.parent)]

        monkeypatch.setattr(
            "siftd.adapters.registry.load_all_adapters",
            lambda: [FakeAdapter()],
        )
        result = find_session_file("abc12345")
        assert result is not None
        assert result.name == sample_session.name

    def test_returns_none_for_no_match(self, session_dir, monkeypatch):
        class FakeAdapter:
            DEFAULT_LOCATIONS = [str(session_dir.parent)]

        monkeypatch.setattr(
            "siftd.adapters.registry.load_all_adapters",
            lambda: [FakeAdapter()],
        )
        result = find_session_file("zzzznotfound")
        assert result is None


class TestListActiveSessions:
    """Tests for list_active_sessions."""

    def test_lists_recent_sessions(self, sample_session, monkeypatch):
        """list_active_sessions finds recently modified session files."""
        class FakeAdapter:
            NAME = "test"
            DEFAULT_LOCATIONS = [str(sample_session.parent.parent)]

        monkeypatch.setattr(
            "siftd.peek.scanner.load_all_adapters",
            lambda: [FakeAdapter()],
        )
        sessions = list_active_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "test-session"

    def test_filters_by_workspace(self, sample_session, monkeypatch):
        class FakeAdapter:
            NAME = "test"
            DEFAULT_LOCATIONS = [str(sample_session.parent.parent)]

        monkeypatch.setattr(
            "siftd.peek.scanner.load_all_adapters",
            lambda: [FakeAdapter()],
        )
        # Should match
        sessions = list_active_sessions(workspace="project")
        assert len(sessions) == 1

        # Should not match
        sessions = list_active_sessions(workspace="nonexistent")
        assert len(sessions) == 0

    def test_excludes_old_by_default(self, session_dir, monkeypatch):
        """Sessions older than threshold are excluded by default."""
        import os

        path = session_dir / "old-session.jsonl"
        _write_session(path, [_make_user_record("old")])
        # Set mtime to 3 hours ago
        old_time = time.time() - 10800
        os.utime(path, (old_time, old_time))

        class FakeAdapter:
            NAME = "test"
            DEFAULT_LOCATIONS = [str(session_dir.parent)]

        monkeypatch.setattr(
            "siftd.peek.scanner.load_all_adapters",
            lambda: [FakeAdapter()],
        )
        sessions = list_active_sessions()
        assert len(sessions) == 0

    def test_include_inactive_shows_old(self, session_dir, monkeypatch):
        """--all flag includes old sessions."""
        import os

        path = session_dir / "old-session.jsonl"
        _write_session(path, [_make_user_record("old")])
        old_time = time.time() - 10800
        os.utime(path, (old_time, old_time))

        class FakeAdapter:
            NAME = "test"
            DEFAULT_LOCATIONS = [str(session_dir.parent)]

        monkeypatch.setattr(
            "siftd.peek.scanner.load_all_adapters",
            lambda: [FakeAdapter()],
        )
        sessions = list_active_sessions(include_inactive=True)
        assert len(sessions) == 1

    def test_sorted_by_most_recent(self, session_dir, monkeypatch):
        """Sessions are sorted most-recent first."""
        import os

        path1 = session_dir / "older.jsonl"
        path2 = session_dir / "newer.jsonl"
        _write_session(path1, [_make_user_record("older")])
        _write_session(path2, [_make_user_record("newer")])

        now = time.time()
        os.utime(path1, (now - 600, now - 600))  # 10 min ago
        os.utime(path2, (now - 60, now - 60))  # 1 min ago

        class FakeAdapter:
            NAME = "test"
            DEFAULT_LOCATIONS = [str(session_dir.parent)]

        monkeypatch.setattr(
            "siftd.peek.scanner.load_all_adapters",
            lambda: [FakeAdapter()],
        )
        sessions = list_active_sessions()
        assert len(sessions) == 2
        assert sessions[0].file_path.name == "newer.jsonl"
        assert sessions[1].file_path.name == "older.jsonl"


