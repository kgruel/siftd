"""Tests for siftd peek command (cmd_peek)."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from siftd.cli import main
from siftd.cli.peek import cmd_peek
from siftd.domain.peek import PeekExchange, PeekNarrativeBlock, PeekToolCall, SessionInfo
from siftd.output.common import fmt_timestamp
from siftd.peek.follow import FollowEvent


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


def _peek_args(**kwargs):
    defaults = {
        "session_id": None,
        "workspace": None,
        "branch": None,
        "all": False,
        "limit": 10,
        "exchanges": None,
        "brief": False,
        "full": False,
        "chars": None,
        "thinking": False,
        "tools": False,
        "follow": False,
        "tail": False,
        "tail_lines": 20,
        "json": False,
        "main_only": False,
        "children": None,
        "last_response": False,
        "last_prompt": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _detail(*, include_thinking: bool = False, response_text: str = "Doing it."):
    narrative = [PeekNarrativeBlock(block_type="text", content=response_text)]
    if include_thinking:
        narrative.insert(0, PeekNarrativeBlock(block_type="thinking", content="Plan it."))
    narrative.append(
        PeekNarrativeBlock(
            block_type="tool_calls",
            tool_calls=[PeekToolCall(tool_name="shell.execute", input="git status")],
        )
    )
    return type("Detail", (), {
        "info": _session("abc123"),
        "started_at": "2025-01-20T10:00:00Z",
        "exchanges": [
            PeekExchange(
                timestamp="2025-01-20T10:01:00Z",
                prompt_text="show me",
                narrative=narrative,
                input_tokens=10,
                output_tokens=20,
            )
        ],
    })()


def _follow_event(*, include_thinking: bool = False, response_text: str = "Still working.") -> FollowEvent:
    narrative = [PeekNarrativeBlock(block_type="text", content=response_text)]
    if include_thinking:
        narrative.insert(0, PeekNarrativeBlock(block_type="thinking", content="Check config."))
    narrative.append(
        PeekNarrativeBlock(
            block_type="tool_calls",
            tool_calls=[PeekToolCall(tool_name="file.read", input="src/config.py")],
        )
    )
    return FollowEvent(
        timestamp="2025-01-20T10:02:00Z",
        text=response_text,
        narrative=narrative,
        tool_calls=[("file.read", 1, ["src/config.py"])],
        input_tokens=5,
        output_tokens=6,
        is_user=False,
    )


class TestPeekValidation:
    """Test flag validation without hitting the filesystem."""

    def test_last_response_and_last_prompt_exclusive(self, capsys):
        rc = main(["peek", "--last-response", "--last-prompt"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().err

    def test_follow_with_tail_exclusive(self, capsys):
        rc = main(["peek", "--follow", "--tail", "abc"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().err

    def test_follow_with_last_response_exclusive(self, capsys):
        rc = main(["peek", "--follow", "--last-response"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().err

    def test_follow_with_last_prompt_exclusive(self, capsys):
        rc = main(["peek", "--follow", "--last-prompt"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().err

    def test_last_response_with_json_exclusive(self, capsys):
        rc = main(["peek", "--last-response", "--json"])
        assert rc == 1
        assert "mutually exclusive" in capsys.readouterr().err

    def test_limit_zero_rejected(self, capsys):
        rc = main(["peek", "-n", "0"])
        assert rc == 1
        assert "--limit must be at least 1" in capsys.readouterr().err

    def test_exchanges_zero_rejected(self, capsys):
        rc = main(["peek", "--exchanges", "0"])
        assert rc == 1
        assert "--exchanges must be at least 1" in capsys.readouterr().err


class TestPeekDetailMode:
    @patch("siftd.api.find_session_file")
    @patch("siftd.api.read_session_detail")
    def test_default_detail_does_not_truncate_text(self, mock_read_detail, mock_find, capsys):
        long_text = "Doing it. " * 30
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.return_value = _detail(response_text=long_text)

        rc = main(["peek", "abc123"])
        assert rc == 0
        out = capsys.readouterr().out

        assert out.count("Doing it.") >= 20
        assert "..." not in out

    @patch("siftd.api.find_session_file")
    @patch("siftd.api.read_session_detail")
    def test_brief_alias_truncates_text(self, mock_read_detail, mock_find, capsys):
        long_text = "Doing it. " * 30
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.return_value = _detail(response_text=long_text)

        rc = main(["peek", "abc123", "-b"])
        assert rc == 0
        out = capsys.readouterr().out

        assert "Doing it." in out
        assert "..." in out

    @patch("siftd.api.find_session_file")
    @patch("siftd.api.read_session_detail")
    def test_tools_renders_painted_detail(self, mock_read_detail, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.return_value = _detail()

        rc = main(["peek", "abc123", "--tools"])
        assert rc == 0
        out = capsys.readouterr().out
        expected_started = fmt_timestamp("2025-01-20T10:00:00Z")
        expected_turn = fmt_timestamp("2025-01-20T10:01:00Z", time_only=True)

        assert "Session: abc123" in out
        assert f"Started: {expected_started}" in out
        assert f"[user] {expected_turn}" in out
        assert f"[assistant] {expected_turn} (30 tok)" in out
        assert "Doing it." in out
        assert "shell.execute" in out
        assert "$ git status" in out
        assert "thinking" not in out
        mock_read_detail.assert_called_once_with(Path("/tmp/fake-session.jsonl"), last_n=5, include_thinking=False)

    @patch("siftd.api.find_session_file")
    @patch("siftd.api.read_session_detail")
    def test_thinking_renders_inline_when_requested(self, mock_read_detail, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.side_effect = lambda path, last_n, include_thinking: _detail(include_thinking=include_thinking)

        rc = main(["peek", "abc123", "--thinking"])
        assert rc == 0
        out = capsys.readouterr().out

        assert "thinking" in out and "Plan it." in out
        assert "Doing it." in out
        assert "→ shell.execute" in out
        assert "$ git status" not in out
        mock_read_detail.assert_called_once_with(Path("/tmp/fake-session.jsonl"), last_n=5, include_thinking=True)

    @patch("siftd.api.find_session_file")
    @patch("siftd.api.read_session_detail")
    def test_full_alias_shows_thinking_and_tool_payloads(self, mock_read_detail, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.side_effect = lambda path, last_n, include_thinking: _detail(include_thinking=include_thinking)

        rc = main(["peek", "abc123", "-F"])
        assert rc == 0
        out = capsys.readouterr().out

        assert "thinking" in out and "Plan it." in out
        assert "$ git status" in out
        mock_read_detail.assert_called_once_with(Path("/tmp/fake-session.jsonl"), last_n=5, include_thinking=True)


class TestPeekFollowMode:
    @patch("siftd.api.find_session_file")
    @patch("siftd.peek.read_session_detail")
    @patch("siftd.peek.follow_session")
    def test_follow_default_does_not_truncate_text(self, mock_follow, mock_read_detail, mock_find, capsys):
        long_initial = "Doing it. " * 30
        long_live = "Still working. " * 30
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.return_value = _detail(response_text=long_initial)

        def _emit(path, *, json_mode, render, include_thinking, **kwargs):
            assert not json_mode
            render(_follow_event(response_text=long_live))

        mock_follow.side_effect = _emit

        rc = main(["peek", "abc123", "--follow"])
        assert rc == 0
        out = capsys.readouterr().out

        assert out.count("Doing it.") >= 20
        assert out.count("Still working.") >= 20
        assert "..." not in out

    @patch("siftd.api.find_session_file")
    @patch("siftd.peek.read_session_detail")
    @patch("siftd.peek.follow_session")
    def test_follow_renders_painted_initial_context_and_live_events(self, mock_follow, mock_read_detail, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.return_value = _detail()

        def _emit(path, *, json_mode, render, include_thinking, **kwargs):
            assert path == Path("/tmp/fake-session.jsonl")
            assert not json_mode
            assert include_thinking is False
            render(_follow_event())

        mock_follow.side_effect = _emit

        rc = main(["peek", "abc123", "--follow"])
        assert rc == 0
        captured = capsys.readouterr()
        out = captured.out
        err = captured.err
        expected_started = fmt_timestamp("2025-01-20T10:00:00Z")
        expected_initial_turn = fmt_timestamp("2025-01-20T10:01:00Z", time_only=True)
        expected_live_turn = fmt_timestamp("2025-01-20T10:02:00Z", time_only=True)

        assert "Session: abc123" in out
        assert f"Started: {expected_started}" in out
        assert f"[user] {expected_initial_turn}" in out
        assert f"[assistant] {expected_initial_turn} (30 tok)" in out
        assert f"[assistant] {expected_live_turn} (11 tok)" in out
        assert "Doing it." in out
        assert "Still working." in out
        assert "→ shell.execute" in out
        assert "→ file.read" in out
        assert "input: git status" not in out
        assert "input: src/config.py" not in out
        assert "--- following ---" in err
        mock_read_detail.assert_called_once_with(Path("/tmp/fake-session.jsonl"), last_n=3, include_thinking=False)

    @patch("siftd.api.find_session_file")
    @patch("siftd.peek.read_session_detail")
    @patch("siftd.peek.follow_session")
    def test_follow_tools_reveals_tool_payloads(self, mock_follow, mock_read_detail, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.return_value = _detail()

        def _emit(path, *, json_mode, render, include_thinking, **kwargs):
            assert not json_mode
            assert include_thinking is False
            render(_follow_event())

        mock_follow.side_effect = _emit

        rc = main(["peek", "abc123", "--follow", "--tools"])
        assert rc == 0
        out = capsys.readouterr().out

        assert "$ git status" in out
        assert "src/config.py" in out

    @patch("siftd.api.find_session_file")
    @patch("siftd.peek.read_session_detail")
    @patch("siftd.peek.follow_session")
    def test_follow_thinking_renders_inline_when_requested(self, mock_follow, mock_read_detail, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read_detail.side_effect = lambda path, last_n, include_thinking: _detail(include_thinking=include_thinking)

        def _emit(path, *, json_mode, render, include_thinking, **kwargs):
            assert not json_mode
            assert include_thinking is True
            render(_follow_event(include_thinking=True))

        mock_follow.side_effect = _emit

        rc = main(["peek", "abc123", "--follow", "--thinking"])
        assert rc == 0
        out = capsys.readouterr().out

        assert "thinking" in out and "Plan it." in out
        assert "Check config." in out
        assert "input: git status" not in out
        assert "input: src/config.py" not in out
        mock_read_detail.assert_called_once_with(Path("/tmp/fake-session.jsonl"), last_n=3, include_thinking=True)


class TestPeekListMode:
    """Test list mode with mocked session discovery."""

    @patch("siftd.api.list_active_sessions")
    def test_no_sessions_found(self, mock_list, capsys):
        mock_list.return_value = []
        rc = main(["peek"])
        assert rc == 0
        assert "No active sessions" in capsys.readouterr().err

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


class TestPeekEdgeBranches:
    @patch("siftd.api.find_session_file")
    def test_last_response_ambiguous_and_not_found(self, mock_find, capsys):
        from siftd.peek import AmbiguousSessionError

        mock_find.side_effect = AmbiguousSessionError("abc", [Path("/tmp/a"), Path("/tmp/b")])
        assert main(["peek", "abc", "--last-response"]) == 1

        mock_find.side_effect = None
        mock_find.return_value = None
        assert main(["peek", "abc", "--last-response"]) == 1
        assert "Session not found" in capsys.readouterr().err

    @patch("siftd.api.list_active_sessions")
    def test_last_response_default_session_missing(self, mock_list, capsys):
        mock_list.return_value = []
        assert main(["peek", "--last-response"]) == 1
        err = capsys.readouterr().err
        assert "No active sessions found" in err and "siftd query" in err

    @patch("siftd.api.list_active_sessions")
    @patch("siftd.api.read_session_detail")
    def test_last_response_and_prompt_error_branches(self, mock_read, mock_list, capsys):
        mock_list.return_value = [_session("abc")]
        mock_read.return_value = None
        assert main(["peek", "--last-response"]) == 1

        empty_detail = type("Detail", (), {"exchanges": []})()
        mock_read.return_value = empty_detail
        assert main(["peek", "--last-response"]) == 1

        detail_no_resp = type("Detail", (), {"exchanges": [PeekExchange(timestamp="t", prompt_text="p", narrative=[], input_tokens=0, output_tokens=0)]})()
        mock_read.return_value = detail_no_resp
        assert main(["peek", "--last-response"]) == 1

        detail_no_prompt = type("Detail", (), {"exchanges": [PeekExchange(timestamp="t", prompt_text="", narrative=[PeekNarrativeBlock(block_type="text", content="r")], input_tokens=0, output_tokens=0)]})()
        mock_read.return_value = detail_no_prompt
        assert main(["peek", "--last-prompt"]) == 1

    @patch("siftd.api.list_active_sessions")
    @patch("siftd.api.read_session_detail")
    def test_last_response_and_prompt_success(self, mock_read, mock_list, capsys):
        mock_list.return_value = [_session("abc")]
        ex = SimpleNamespace(prompt_text="ask", response_text="resp")
        mock_read.return_value = type("Detail", (), {"exchanges": [ex]})()
        assert main(["peek", "--last-response"]) == 0
        assert "resp" in capsys.readouterr().out

        assert main(["peek", "--last-prompt"]) == 0
        assert "ask" in capsys.readouterr().out

    @patch("siftd.api.list_active_sessions")
    def test_follow_default_no_sessions(self, mock_list, capsys):
        mock_list.return_value = []
        assert main(["peek", "--follow"]) == 1
        assert "No active sessions found" in capsys.readouterr().err

    @patch("siftd.api.find_session_file")
    def test_follow_id_ambiguous_and_not_found(self, mock_find, capsys):
        from siftd.peek import AmbiguousSessionError

        mock_find.side_effect = AmbiguousSessionError("abc", [Path("/tmp/a"), Path("/tmp/b")])
        assert main(["peek", "abc", "--follow"]) == 1

        mock_find.side_effect = None
        mock_find.return_value = None
        assert main(["peek", "abc", "--follow"]) == 1
        assert "Session not found" in capsys.readouterr().err

    @patch("siftd.api.list_active_sessions")
    @patch("siftd.peek.read_session_detail")
    @patch("siftd.peek.follow_session")
    def test_follow_default_uses_most_recent_session(self, mock_follow, mock_read, mock_list):
        mock_list.return_value = [_session("abc", file_path=Path("/tmp/default.jsonl"))]
        mock_read.return_value = None
        assert main(["peek", "--follow"]) == 0
        mock_follow.assert_called_once()
        assert mock_follow.call_args.args[0] == Path("/tmp/default.jsonl")

    @patch("siftd.api.find_session_file")
    @patch("siftd.peek.read_session_detail")
    @patch("siftd.peek.follow_session")
    def test_follow_json_mode_ndjson(self, mock_follow, mock_read, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read.return_value = _detail()
        assert main(["peek", "abc", "--follow", "--json"]) == 0
        out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(out_lines) >= 2
        parsed = [json.loads(line) for line in out_lines]
        assert all(isinstance(p, dict) for p in parsed)
        mock_follow.assert_called_once()
        assert mock_follow.call_args.kwargs["json_mode"] is True

    @patch("siftd.api.find_session_file")
    @patch("siftd.api.tail_session")
    def test_detail_tail_text_and_json(self, mock_tail, mock_find, capsys):
        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_tail.return_value = ['{"a":1}', 'not-json']
        assert main(["peek", "abc", "--tail"]) == 0
        out = capsys.readouterr().out
        assert '{"a":1}' in out and "not-json" in out

        assert main(["peek", "abc", "--tail", "--json"]) == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed[0]["a"] == 1 and parsed[1] == "not-json"

    @patch("siftd.api.find_session_file")
    @patch("siftd.api.read_session_detail")
    def test_detail_none_and_json_output(self, mock_read, mock_find, capsys):
        from siftd.peek import AmbiguousSessionError

        mock_find.side_effect = AmbiguousSessionError("abc", [Path("/tmp/a"), Path("/tmp/b")])
        assert main(["peek", "abc"]) == 1

        mock_find.side_effect = None
        mock_find.return_value = None
        assert main(["peek", "abc"]) == 1

        mock_find.return_value = Path("/tmp/fake-session.jsonl")
        mock_read.return_value = None
        assert main(["peek", "abc"]) == 1
        capsys.readouterr()

        d = _detail()
        d.info.parent_session_id = "p1"
        mock_read.return_value = d
        assert main(["peek", "abc", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["parent_session_id"] == "p1"

    @patch("siftd.api.list_active_sessions")
    def test_list_mode_ignored_flags_combo(self, mock_list, capsys):
        mock_list.return_value = []
        assert main(["peek", "--tail-lines", "5", "--exchanges", "2", "--tools"]) == 0
        err = capsys.readouterr().err
        assert "--tail-lines" in err and "--exchanges" in err and "--tools" in err

    def test_follow_id_error_branches_direct(self, monkeypatch, capsys):
        from siftd.peek import AmbiguousSessionError

        monkeypatch.setattr(
            "siftd.api.find_session_file",
            lambda _sid: (_ for _ in ()).throw(AmbiguousSessionError("abc", [Path("/tmp/a"), Path("/tmp/b")])),
        )
        assert cmd_peek(_peek_args(session_id="abc", follow=True)) == 1

        monkeypatch.setattr("siftd.api.find_session_file", lambda _sid: None)
        assert cmd_peek(_peek_args(session_id="abc", follow=True)) == 1
        assert "Session not found" in capsys.readouterr().err
