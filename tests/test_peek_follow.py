"""Tests for peek follow mode: parsing, rendering, and hint extraction."""

import json

from siftd.adapters.claude_code import TOOL_ALIASES, TOOL_HINT_KEYS
from siftd.adapters.sdk import extract_tool_hint
from siftd.peek.follow import (
    FollowEvent,
    event_to_json,
    parse_record,
    render_tool_line,
)


# ---------------------------------------------------------------------------
# extract_tool_hint
# ---------------------------------------------------------------------------


class TestExtractToolHint:
    def test_bash_prefers_description(self):
        hint = extract_tool_hint(
            "shell.execute",
            {"description": "Run unit tests", "command": "pytest -v"},
            TOOL_HINT_KEYS,
        )
        assert hint == "Run unit tests"

    def test_bash_falls_back_to_command(self):
        hint = extract_tool_hint(
            "shell.execute",
            {"command": "git status"},
            TOOL_HINT_KEYS,
        )
        assert hint == "git status"

    def test_file_read_truncates_path(self):
        hint = extract_tool_hint(
            "file.read",
            {"file_path": "/Users/kaygee/Code/siftd/src/siftd/config.py"},
            TOOL_HINT_KEYS,
        )
        assert hint == "siftd/config.py"

    def test_short_path_not_truncated(self):
        hint = extract_tool_hint(
            "file.read",
            {"file_path": "config.py"},
            TOOL_HINT_KEYS,
        )
        assert hint == "config.py"

    def test_grep_pattern(self):
        hint = extract_tool_hint(
            "search.grep",
            {"pattern": "def follow_session"},
            TOOL_HINT_KEYS,
        )
        assert hint == "def follow_session"

    def test_glob_pattern(self):
        hint = extract_tool_hint(
            "file.glob",
            {"pattern": "**/*.py"},
            TOOL_HINT_KEYS,
        )
        assert hint == "**/*.py"

    def test_web_search_query(self):
        hint = extract_tool_hint(
            "search.web",
            {"query": "python jsonl streaming"},
            TOOL_HINT_KEYS,
        )
        assert hint == "python jsonl streaming"

    def test_task_description(self):
        hint = extract_tool_hint(
            "task.spawn",
            {"description": "Explore codebase"},
            TOOL_HINT_KEYS,
        )
        assert hint == "Explore codebase"

    def test_unknown_tool_returns_none(self):
        hint = extract_tool_hint(
            "unknown.tool",
            {"stuff": "value"},
            TOOL_HINT_KEYS,
        )
        assert hint is None

    def test_max_len_truncation(self):
        hint = extract_tool_hint(
            "shell.execute",
            {"command": "a" * 100},
            TOOL_HINT_KEYS,
            max_len=20,
        )
        assert hint is not None
        assert len(hint) == 20
        assert hint.endswith("...")

    def test_empty_value_skipped(self):
        hint = extract_tool_hint(
            "shell.execute",
            {"description": "", "command": "ls"},
            TOOL_HINT_KEYS,
        )
        assert hint == "ls"

    def test_non_string_value_skipped(self):
        hint = extract_tool_hint(
            "shell.execute",
            {"description": 42, "command": "ls"},
            TOOL_HINT_KEYS,
        )
        assert hint == "ls"


# ---------------------------------------------------------------------------
# parse_record
# ---------------------------------------------------------------------------


class TestParseRecord:
    def test_user_record(self):
        record = {
            "type": "user",
            "timestamp": "2025-01-20T10:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello world"}],
            },
        }
        event = parse_record(record)
        assert event is not None
        assert event.is_user
        assert event.text == "Hello world"
        assert event.timestamp == "2025-01-20T10:00:00Z"

    def test_tool_result_returns_none(self):
        record = {
            "type": "user",
            "timestamp": "2025-01-20T10:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"},
                ],
            },
        }
        assert parse_record(record) is None

    def test_assistant_record_with_tools(self):
        record = {
            "type": "assistant",
            "timestamp": "2025-01-20T10:00:05Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-5-20251101",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "/src/main.py"}},
                ],
                "usage": {"input_tokens": 500, "output_tokens": 200},
            },
        }
        event = parse_record(
            record,
            tool_aliases=TOOL_ALIASES,
            hint_keys=TOOL_HINT_KEYS,
        )
        assert event is not None
        assert not event.is_user
        assert event.text == "Let me read that file."
        assert event.input_tokens == 500
        assert event.output_tokens == 200
        assert len(event.tool_calls) == 1
        name, count, hints = event.tool_calls[0]
        assert name == "file.read"
        assert count == 1
        assert hints == ["src/main.py"]

    def test_assistant_record_no_text(self):
        record = {
            "type": "assistant",
            "timestamp": "2025-01-20T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        }
        event = parse_record(record, tool_aliases=TOOL_ALIASES, hint_keys=TOOL_HINT_KEYS)
        assert event is not None
        assert event.text is None
        assert len(event.tool_calls) == 1

    def test_non_message_record_returns_none(self):
        assert parse_record({"type": "system"}) is None
        assert parse_record({}) is None

    def test_multiple_same_tool(self):
        record = {
            "type": "assistant",
            "timestamp": "2025-01-20T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"file_path": "/a.py"}},
                    {"type": "tool_use", "id": "tu2", "name": "Read", "input": {"file_path": "/b.py"}},
                    {"type": "tool_use", "id": "tu3", "name": "Read", "input": {"file_path": "/c.py"}},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        }
        event = parse_record(record, tool_aliases=TOOL_ALIASES, hint_keys=TOOL_HINT_KEYS)
        assert event is not None
        assert len(event.tool_calls) == 1
        name, count, hints = event.tool_calls[0]
        assert name == "file.read"
        assert count == 3
        assert len(hints) == 3


# ---------------------------------------------------------------------------
# render_tool_line
# ---------------------------------------------------------------------------


class TestRenderToolLine:
    def test_single_with_hint(self):
        result = render_tool_line("file.read", 1, ["src/config.py"])
        assert result == "  \u2192 file.read: src/config.py"

    def test_single_no_hint(self):
        result = render_tool_line("task.spawn", 1, [])
        assert result == "  \u2192 task.spawn"

    def test_multiple_with_hints(self):
        result = render_tool_line("file.read", 3, ["a.py", "b.py", "c.py"])
        assert result == "  \u2192 file.read \u00d73: a.py, b.py, c.py"

    def test_multiple_elide_hints(self):
        result = render_tool_line("file.read", 5, ["a.py", "b.py", "c.py", "d.py", "e.py"])
        assert result == "  \u2192 file.read \u00d75: a.py, b.py, c.py ... +2 more"

    def test_multiple_no_hints(self):
        result = render_tool_line("shell.execute", 4, [])
        assert result == "  \u2192 shell.execute \u00d74"


# ---------------------------------------------------------------------------
# event_to_json
# ---------------------------------------------------------------------------


class TestEventToJson:
    def test_user_event(self):
        event = FollowEvent(
            timestamp="2025-01-20T10:00:00Z",
            text="Hello",
            is_user=True,
        )
        d = event_to_json(event)
        assert d["role"] == "user"
        assert d["text"] == "Hello"
        assert "tool_calls" not in d

    def test_assistant_event(self):
        event = FollowEvent(
            timestamp="2025-01-20T10:01:00Z",
            text="Reading.",
            tool_calls=[("file.read", 2, ["a.py", "b.py"])],
            input_tokens=500,
            output_tokens=200,
        )
        d = event_to_json(event)
        assert d["role"] == "assistant"
        assert d["input_tokens"] == 500
        assert d["output_tokens"] == 200
        assert len(d["tool_calls"]) == 1
        assert d["tool_calls"][0]["name"] == "file.read"
        assert d["tool_calls"][0]["count"] == 2
        assert d["tool_calls"][0]["hints"] == ["a.py", "b.py"]

    def test_json_serializable(self):
        event = FollowEvent(
            timestamp="2025-01-20T10:01:00Z",
            text="test",
            tool_calls=[("file.read", 1, ["a.py"])],
            input_tokens=100,
            output_tokens=50,
        )
        # Should not raise
        json.dumps(event_to_json(event))
