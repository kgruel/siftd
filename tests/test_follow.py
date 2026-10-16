"""Tests for siftd.peek.follow — live session following utilities."""

import json
import os
import signal
import threading
import time
from types import ModuleType

from conftest import FIXTURES_DIR

from siftd.peek.follow import (
    FollowEvent,
    _resolve_adapter_config,
    event_to_json,
    follow_session,
    parse_record,
    render_tool_line,
)
from siftd.plugin_discovery import PluginInfo

_USER_REC = json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}})
_ASST_REC = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})


def _mod(name="m", **kw):
    m = ModuleType(name)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def _user(content="hello"):
    return {"type": "user", "message": {"content": content}}


def _asst(content=None, usage=None):
    d = {"type": "assistant", "message": {"content": content or []}}
    if usage:
        d["message"]["usage"] = usage
    return d


class TestParseRecord:
    def test_non_message(self):
        assert parse_record({"type": "system"}) is None

    def test_user_text(self):
        r = parse_record(_user([{"type": "text", "text": "hi"}]))
        assert r and r.is_user and r.text == "hi"

    def test_user_string_content(self):
        assert parse_record(_user("hello")).text == "hello"

    def test_user_none_content(self):
        assert parse_record(_user(None)) is not None

    def test_user_non_list(self):
        assert parse_record(_user(42)) is not None

    def test_user_string_block(self):
        assert parse_record(_user(["raw"])).text == "raw"

    def test_user_tool_result_skipped(self):
        assert parse_record(_user([{"type": "tool_result"}])) is None

    def test_assistant_text(self):
        r = parse_record(_asst([{"type": "text", "text": "reply"}],
                               {"input_tokens": 10, "output_tokens": 20}))
        assert r and r.text == "reply" and r.input_tokens == 10

    def test_assistant_string_block(self):
        assert parse_record(_asst(["raw"])).text == "raw"

    def test_assistant_tool_use(self):
        r = parse_record(_asst([{"type": "tool_use", "name": "f.r", "input": {}}]))
        assert r and r.tool_calls[0][0] == "f.r"

    def test_thinking(self):
        r = parse_record(_asst([{"type": "thinking", "thinking": "hmm"}]),
                         include_thinking=True)
        assert r and "[thinking]" in r.text

    def test_empty_assistant(self):
        assert parse_record(_asst([])) is None


class TestRenderToolLine:
    def test_basic(self):
        assert "f.r" in render_tool_line("f.r", 1, [])

    def test_with_hints(self):
        assert "3" in render_tool_line("sh", 3, ["ls", "pwd"])


class TestEventToJson:
    def test_user(self):
        assert event_to_json(FollowEvent(text="hi", is_user=True))["role"] == "user"

    def test_assistant(self):
        d = event_to_json(FollowEvent(text="r", is_user=False, input_tokens=10))
        assert d["role"] == "assistant" and d["input_tokens"] == 10


def _follow_with_writer(f, lines, poll=0.02, delay=0.08, **kw):
    """Run follow_session in main thread, append lines from writer thread, then SIGINT."""
    def writer():
        time.sleep(delay)
        with f.open("a") as fh:
            for line in lines:
                fh.write(line + "\n")
            fh.flush()
        time.sleep(delay)
        os.kill(os.getpid(), signal.SIGINT)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    follow_session(f, poll_interval=poll, **kw)
    t.join(timeout=2)


class TestFollowSession:
    def test_on_turn_with_invalid(self, tmp_path, monkeypatch):
        """on_turn fires; invalid JSON + blank lines are skipped."""
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        f = tmp_path / "session.jsonl"
        f.write_text("")
        events = []
        _follow_with_writer(f, ["not json", "", _USER_REC],
                            on_turn=lambda e: events.append(e))
        assert len(events) >= 1 and events[0].is_user

    def test_json_mode(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        f = tmp_path / "session.jsonl"
        f.write_text("")
        _follow_with_writer(f, [_ASST_REC], json_mode=True)
        assert "ok" in capsys.readouterr().out

    def test_streamed_message_counted_once(self, tmp_path, monkeypatch):
        """Issue #97: a streamed message's records repeat its usage. The events of the
        followed stream sum to the message's final counts, not one copy per record."""
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        f = tmp_path / "session.jsonl"
        f.write_text("")
        lines = (FIXTURES_DIR / "adapters" / "claude_code" / "streamed_message" / "input.jsonl").read_text().splitlines()
        events = []
        _follow_with_writer(f, lines, on_turn=lambda e: events.append(e))
        assistant = [e for e in events if not e.is_user]
        assert sum(e.output_tokens for e in assistant) == 176
        assert {e.message_id for e in assistant} == {"msg_011Cea2TnjxEfrXbjGNnGHD1"}

    def test_thinking_only_final_record_keeps_the_message_total(self, tmp_path, monkeypatch):
        """The stream's final record shows nothing (thinking only), so its usage must still
        reach the stream: the message's 176 tokens, not the 2 of its earlier record."""
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        f = tmp_path / "session.jsonl"
        f.write_text("")
        lines = (FIXTURES_DIR / "adapters" / "claude_code" / "streamed_message" / "input.jsonl").read_text().splitlines()
        final = json.loads(lines[3])
        final["message"]["content"] = [{"type": "thinking", "thinking": "done"}]
        events = []
        _follow_with_writer(f, [lines[2], json.dumps(final)], on_turn=lambda e: events.append(e))
        assert sum(e.output_tokens for e in events if not e.is_user) == 176
        assert [e.shows_output for e in events if not e.is_user] == [True, False]

    def test_truncation_restarts_the_usage_count(self, tmp_path, monkeypatch):
        """A truncated file is read again from its start, so its messages are counted
        again: the charge state for the old content must not absorb the new copy."""
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        f = tmp_path / "session.jsonl"
        f.write_text("")
        lines = (FIXTURES_DIR / "adapters" / "claude_code" / "streamed_message" / "input.jsonl").read_text().splitlines()

        def writer():
            time.sleep(0.08)
            with f.open("a") as fh:
                fh.write("\n".join(lines) + "\n")
            time.sleep(0.15)
            f.write_text(lines[3] + "\n")  # the final 176-token record alone; smaller than before
            time.sleep(0.15)
            os.kill(os.getpid(), signal.SIGINT)

        events = []
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        follow_session(f, poll_interval=0.02, on_turn=lambda e: events.append(e))
        t.join(timeout=2)
        assert sum(e.output_tokens for e in events if not e.is_user) == 176 + 176

    def test_render_callback(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        f = tmp_path / "session.jsonl"
        f.write_text("")
        rendered = []
        _follow_with_writer(f, [_USER_REC], render=lambda e: rendered.append(e))
        assert len(rendered) >= 1


class TestResolveAdapterConfig:
    def test_no_adapter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.peek.reader.load_all_adapters", lambda: [])
        assert _resolve_adapter_config(tmp_path / "x.jsonl") == (None, None)

    def test_with_config(self, tmp_path, monkeypatch):
        loc = tmp_path / "sessions"
        loc.mkdir()
        (loc / "t.jsonl").write_text("{}")
        aliases, hints = {"R": "f.r"}, {"f.r": ["path"]}
        m = _mod("c", peek_scan=lambda p: None, DEFAULT_LOCATIONS=[str(loc)],
                 TOOL_ALIASES=aliases, TOOL_HINT_KEYS=hints)
        monkeypatch.setattr(
            "siftd.peek.reader.load_all_adapters",
            lambda: [PluginInfo(name="c", origin="builtin", module=m)],
        )
        assert _resolve_adapter_config(loc / "t.jsonl") == (aliases, hints)
