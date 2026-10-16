"""Tests for peek follow mode: parsing, rendering, and hint extraction."""

import json
import threading
import time
from collections.abc import Callable

import pytest
from conftest import FIXTURES_DIR

from siftd.adapters import claude_code
from siftd.adapters.claude_code import TOOL_ALIASES, TOOL_HINT_KEYS
from siftd.adapters.contract import adapt
from siftd.adapters.sdk import extract_tool_hint
from siftd.peek.follow import (
    FollowEvent,
    account_record,
    charge_usage,
    event_to_json,
    follow_session,
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

    def test_assistant_record_includes_thinking_when_requested(self):
        record = {
            "type": "assistant",
            "timestamp": "2025-01-20T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I should inspect the config first."},
                    {"type": "text", "text": "I'll inspect the config."},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        }
        event = parse_record(record, include_thinking=True)
        assert event is not None
        assert event.text == "[thinking] I should inspect the config first.\nI'll inspect the config."

    def test_assistant_record_skips_empty_thinking_placeholder(self):
        record = {
            "type": "assistant",
            "timestamp": "2025-01-20T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "   "},
                    {"type": "text", "text": "I'll inspect the config."},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        }
        event = parse_record(record, include_thinking=True)
        assert event is not None
        assert event.text == "I'll inspect the config."
        assert [b.block_type for b in event.narrative] == ["text"]

    def test_assistant_record_with_only_empty_thinking_returns_none(self):
        record = {
            "type": "assistant",
            "timestamp": "2025-01-20T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": ""},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        }
        assert parse_record(record, include_thinking=True) is None

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

    def test_narrative_serialized_when_present(self):
        from siftd.domain.peek import PeekNarrativeBlock, PeekToolCall

        event = FollowEvent(
            timestamp="2025-01-20T10:01:00Z",
            text="Thinking then acting.",
            narrative=[
                PeekNarrativeBlock(block_type="thinking", content="Let me check."),
                PeekNarrativeBlock(block_type="text", content="Thinking then acting."),
                PeekNarrativeBlock(
                    block_type="tool_calls",
                    tool_calls=[PeekToolCall(tool_name="file.read", input="a.py")],
                ),
            ],
            tool_calls=[("file.read", 1, ["a.py"])],
            input_tokens=100,
            output_tokens=50,
        )
        d = event_to_json(event)
        assert "narrative" in d
        assert len(d["narrative"]) == 3
        assert d["narrative"][0] == {"block_type": "thinking", "content": "Let me check."}
        assert d["narrative"][1] == {"block_type": "text", "content": "Thinking then acting."}
        assert d["narrative"][2] == {
            "block_type": "tool_calls",
            "tool_calls": [{"tool_name": "file.read", "count": 1, "input": "a.py"}],
        }
        # Must be JSON-serializable
        json.dumps(d)

    def test_narrative_omitted_when_empty(self):
        event = FollowEvent(
            timestamp="2025-01-20T10:01:00Z",
            text="No narrative.",
            tool_calls=[],
            input_tokens=100,
            output_tokens=50,
        )
        d = event_to_json(event)
        assert "narrative" not in d

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


def _wait_for_events(events: list, count: int, timeout: float = 1.0) -> bool:
    """Poll until *events* has at least *count* items, or *timeout* expires."""
    start = time.time()
    while time.time() - start < timeout:
        if len(events) >= count:
            return True
        time.sleep(0.01)
    return False


def _follow_in_thread(
    path,
    *,
    on_turn: Callable[[FollowEvent], None] | None = None,
    json_mode: bool = False,
) -> threading.Thread:
    """Start ``follow_session`` in a daemon thread and wait until it has
    opened the file and seeked to end (i.e. is ready to observe new writes).

    Uses a threading.Event set by follow_session itself — no sleep heuristic.
    """
    ready = threading.Event()
    thread = threading.Thread(
        target=follow_session,
        args=(path,),
        kwargs={
            "poll_interval": 0.01,
            "on_turn": on_turn,
            "json_mode": json_mode,
            "ready_event": ready,
        },
        daemon=True,
    )
    thread.start()
    ready.wait(timeout=5.0)
    return thread


def _stop_follow(path, thread: threading.Thread, *, timeout: float = 1.0) -> None:
    """Delete the session file and join the follow thread."""
    path.unlink()
    thread.join(timeout=timeout)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# follow_session integration tests
# ---------------------------------------------------------------------------


def test_follow_session_partial_lines(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text("")
    events: list[FollowEvent] = []

    thread = _follow_in_thread(path, on_turn=events.append)

    record = {
        "type": "user",
        "timestamp": "2025-01-20T10:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
    }
    line = json.dumps(record)
    first_half = line[: len(line) // 2]
    second_half = line[len(line) // 2 :]

    with path.open("a", encoding="utf-8") as f:
        f.write(first_half)
        f.flush()

    time.sleep(0.05)
    assert events == []

    with path.open("a", encoding="utf-8") as f:
        f.write(second_half + "\n")
        f.flush()

    assert _wait_for_events(events, 1)
    assert events[0].is_user

    _stop_follow(path, thread)


def test_follow_session_deletion_stops(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text("")

    thread = _follow_in_thread(path)

    _stop_follow(path, thread)


def test_follow_session_json_output(tmp_path):
    """Verify json_mode produces correct JSON via on_turn + event_to_json.

    Previous version monkeypatched sys.stdout which races under pytest-xdist.
    Instead we collect events via the xdist-safe on_turn callback, then verify
    event_to_json produces the same output that json_mode would print.
    """
    path = tmp_path / "session.jsonl"
    path.write_text("")
    events: list[FollowEvent] = []

    thread = _follow_in_thread(path, on_turn=events.append)

    record = {
        "type": "user",
        "timestamp": "2025-01-20T10:00:00Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()

    assert _wait_for_events(events, 1)

    _stop_follow(path, thread)

    # Verify the event converts to correct JSON (same as json_mode would emit)
    assert len(events) == 1
    payload = event_to_json(events[0])
    assert payload["role"] == "user"
    assert payload["text"] == "Hello"
    # Must be JSON-serializable (json_mode does json.dumps on this)
    serialized = json.dumps(payload, separators=(",", ":"))
    roundtripped = json.loads(serialized)
    assert roundtripped == payload


def test_follow_session_with_timeout(tmp_path):
    """Verify timeout causes follow_session to exit after specified duration."""
    path = tmp_path / "session.jsonl"
    path.write_text("")
    events: list[FollowEvent] = []

    # Start follow with 1 second timeout
    start = time.time()
    follow_session(
        path,
        poll_interval=0.05,
        on_turn=events.append,
        timeout=1.0,
    )
    elapsed = time.time() - start

    # Should exit after ~1 second (with some tolerance for overhead)
    assert elapsed >= 1.0
    assert elapsed < 1.5

    # Clean up the file so _stop_follow can unlink it without errors
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Streamed-message usage (#97)
# ---------------------------------------------------------------------------

_STREAMED_FIXTURE = FIXTURES_DIR / "adapters" / "claude_code" / "streamed_message" / "input.jsonl"


def _streamed_records() -> list[dict]:
    return [json.loads(line) for line in _STREAMED_FIXTURE.read_text().splitlines() if line.strip()]


def _charged_totals(records: list[dict], *, include_thinking: bool = False) -> tuple[int, int]:
    """Replay records through the follow accounting, as follow_session does."""
    charged: dict[str, tuple[int, int]] = {}
    input_total = output_total = 0
    for record in records:
        event = account_record(record, charged=charged, include_thinking=include_thinking)
        if event is None or event.is_user:
            continue
        input_total += event.input_tokens
        output_total += event.output_tokens
    return input_total, output_total


class TestChargeUsage:
    """Follow mode counts a streamed message's usage once, at its final record."""

    @pytest.mark.parametrize("include_thinking", [False, True])
    def test_golden_stream_matches_peek_exchanges(self, include_thinking):
        records = _streamed_records()
        exchanges = adapt(claude_code).peek_exchanges(_STREAMED_FIXTURE, 5)
        peek_totals = (sum(x.input_tokens for x in exchanges), sum(x.output_tokens for x in exchanges))
        assert _charged_totals(records, include_thinking=include_thinking) == peek_totals == (2, 176)

    def test_stream_starting_mid_message_charges_final_counts(self):
        """follow starts at end-of-file, so a message's first records can be unseen. The
        final record still carries the message's counts and is charged them in full."""
        records = _streamed_records()
        assert _charged_totals(records[2:])[1] == 176
        assert _charged_totals(records[1:])[1] == 176

    def test_unkeyed_records_are_charged_as_is(self):
        charged: dict[str, tuple[int, int]] = {}
        for _ in range(2):
            event = charge_usage(FollowEvent(input_tokens=1, output_tokens=5), charged)
            assert (event.input_tokens, event.output_tokens) == (1, 5)
        assert charged == {}

    def test_user_event_is_not_charged(self):
        event = FollowEvent(is_user=True, message_id="m1", input_tokens=9, output_tokens=9)
        assert charge_usage(event, {}) is event

    def test_role_mismatch_record_is_an_assistant_event(self):
        """effective_role classifies a record typed user with role assistant as a response."""
        record = {"type": "user", "timestamp": "T", "message": {
            "role": "assistant", "id": "msg_x", "content": [{"type": "text", "text": "reply"}],
            "usage": {"input_tokens": 3, "output_tokens": 4}}}
        event = parse_record(record)
        assert event is not None and not event.is_user
        assert (event.message_id, event.input_tokens, event.output_tokens) == ("msg_x", 3, 4)

    def test_final_record_without_visible_output_is_still_charged(self):
        """A message whose final record shows nothing (thinking only) keeps that record's
        usage: it is charged as a usage-only event, so the message totals stay whole."""
        charged: dict[str, tuple[int, int]] = {}
        first = {"type": "assistant", "timestamp": "T", "message": {
            "role": "assistant", "id": "msg_t", "content": [
                {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}],
            "usage": {"input_tokens": 1, "output_tokens": 2}}}
        final = {"type": "assistant", "timestamp": "T", "message": {
            "role": "assistant", "id": "msg_t", "content": [{"type": "thinking", "thinking": ""}],
            "usage": {"input_tokens": 1, "output_tokens": 20}}}
        first_event = account_record(first, charged=charged)
        final_event = account_record(final, charged=charged)
        assert first_event is not None and first_event.shows_output
        assert final_event is not None and not final_event.shows_output
        assert (final_event.input_tokens, final_event.output_tokens) == (0, 18)
        assert charged == {"msg_t": (1, 20)}
        assert _charged_totals([first, final]) == (1, 20)


class TestAttachSeedsCharges:
    """follow attaches at end of file. Records already on disk seed the usage charges
    without being emitted, so a message's later record is charged only its new part (#97)."""

    def test_records_already_on_disk_seed_charges_and_are_not_emitted(self, tmp_path):
        records = _streamed_records()
        path = tmp_path / "session.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records[:3]), encoding="utf-8")
        events: list[FollowEvent] = []

        thread = _follow_in_thread(path, on_turn=events.append)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(records[3]) + "\n")
            f.flush()

        assert _wait_for_events(events, 1)
        _stop_follow(path, thread)

        # Records 0-2 were charged on attach (2 input, 2 output), so the final record
        # adds only the difference to 2/176.
        assert len(events) == 1
        assert (events[0].message_id, events[0].input_tokens, events[0].output_tokens) == (
            "msg_011Cea2TnjxEfrXbjGNnGHD1", 0, 174)

    def test_partial_line_on_disk_completes_with_its_charge_intact(self, tmp_path):
        records = _streamed_records()
        path = tmp_path / "session.jsonl"
        final_line = json.dumps(records[3]) + "\n"
        head = "".join(json.dumps(r) + "\n" for r in records[:3])
        path.write_text(head + final_line[: len(final_line) // 2], encoding="utf-8")
        events: list[FollowEvent] = []

        thread = _follow_in_thread(path, on_turn=events.append)
        with path.open("a", encoding="utf-8") as f:
            f.write(final_line[len(final_line) // 2 :])
            f.flush()

        assert _wait_for_events(events, 1)
        _stop_follow(path, thread)

        assert [(e.input_tokens, e.output_tokens) for e in events] == [(0, 174)]

    def test_complete_record_without_its_newline_is_seeded_once(self, tmp_path):
        """A complete record the writer has not yet terminated is part of the initial
        context; when its newline and the rest of the stream arrive, it is not charged
        a second time (it was, at 350 output tokens instead of 176)."""
        records = _streamed_records()
        assert len(records) > 4
        path = tmp_path / "session.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records[:3]) + json.dumps(records[3]), encoding="utf-8")
        seeded = _charged_totals(records[:4])
        events: list[FollowEvent] = []

        thread = _follow_in_thread(path, on_turn=events.append)
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + "".join(json.dumps(r) + "\n" for r in records[4:]))
            f.flush()

        _wait_for_events(events, len(records) - 4)
        _stop_follow(path, thread)

        total_out = seeded[1] + sum(e.output_tokens for e in events if not e.is_user)
        assert total_out == _charged_totals(records)[1]
