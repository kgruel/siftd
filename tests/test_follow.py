"""Tests for siftd.peek.follow — live session following utilities."""

import json
import os
import signal
import threading
import time
from types import ModuleType

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
