"""Tests for OpenCode adapter."""

import json
import sqlite3
from pathlib import Path

import pytest

from siftd.adapters import opencode
from siftd.domain.source import Source


def _make_opencode_db(path, sessions=None):
    """Create an OpenCode SQLite test database. Returns Source."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT, version INTEGER, time_created INTEGER, time_updated INTEGER)")
    conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute("CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    if sessions:
        for s in sessions:
            conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)", s["session"])
            for m in s.get("messages", []):
                conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", (m["id"], s["session"][0], m["ts"], m["ts"], json.dumps(m["data"])))
                for p in m.get("parts", []):
                    conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (p["id"], m["id"], s["session"][0], p.get("ts", m["ts"]), p.get("ts", m["ts"]), json.dumps(p["data"])))
    conn.commit()
    conn.close()
    return Source(kind="sqlite", location=path)


class TestOpenCodeAdapter:
    @pytest.fixture
    def opencode_source(self, tmp_path):
        return _make_opencode_db(tmp_path / "opencode.db", sessions=[{
            "session": ("ses_001", "proj_001", "/test/workspace", "Test", 1, 1710079200000, 1710079260000),
            "messages": [
                {"id": "m1", "ts": 1710079210000, "data": {"role": "user", "summary": {"title": "Run the tests"}},
                 "parts": [{"id": "p1", "data": {"type": "text", "text": "Run the tests please"}}]},
                {"id": "m2", "ts": 1710079220000, "data": {"role": "assistant", "modelID": "claude-3-opus-20240229",
                    "providerID": "anthropic", "cost": 0.025,
                    "tokens": {"total": 680, "input": 500, "output": 120, "reasoning": 60, "cache": {"read": 50, "write": 10}},
                    "finish": "tool-calls"},
                 "parts": [
                    {"id": "p2", "data": {"type": "text", "text": "I'll run the tests for you."}},
                    {"id": "p3", "ts": 1710079225000, "data": {"type": "tool", "callID": "c1", "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "pytest"}, "output": "5 passed, 0 failed",
                            "time": {"start": 1710079225000, "end": 1710079228000}}}}]}]}])

    def test_can_handle(self):
        assert opencode.can_handle(Source(kind="sqlite", location=Path("/mock/opencode.db")))
        assert not opencode.can_handle(Source(kind="sqlite", location=Path("/mock/other.db")))
        assert not opencode.can_handle(Source(kind="file", location=Path("/mock/opencode.db")))

    def test_parse_full(self, opencode_source):
        conv = list(opencode.parse(opencode_source))[0]
        assert conv.external_id == "opencode::ses_001" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "opencode" and "2024-03-10" in conv.started_at
        assert "Run the tests" in conv.prompts[0].content[0].content["text"]
        resp = conv.prompts[0].responses[0]
        assert resp.model == "claude-3-opus-20240229" and resp.usage.input_tokens == 500
        assert resp.attributes["cost"] == "0.025" and resp.attributes["cache_read_input_tokens"] == "50"
        assert resp.tool_calls[0].tool_name == "bash" and resp.tool_calls[0].status == "success"

    def test_parse_empty_and_edge_cases(self, tmp_path):
        assert list(opencode.parse(_make_opencode_db(tmp_path / "empty.db"))) == []
        assert list(opencode.parse(Source(kind="sqlite", location=tmp_path / "nope.db"))) == []
        src = _make_opencode_db(tmp_path / "edge.db", sessions=[{
            "session": ("s2", "p2", "/ws", "Test", 1, 1710079200000, 1710079260000),
            "messages": [
                {"id": "m1", "ts": 1710079210000, "data": {"role": "user", "summary": {"title": "Do something"}}},
                {"id": "m2", "ts": 1710079220000, "data": {"role": "assistant", "modelID": "gpt-4", "tokens": {"input": 10, "output": 5}},
                 "parts": [{"id": "p1", "data": {"type": "reasoning", "text": "thinking..."}},
                    {"id": "p2", "ts": 1710079221000, "data": {"type": "step-start"}},
                    {"id": "p3", "ts": 1710079222000, "data": {"type": "tool", "callID": "c1", "tool": "bash",
                        "state": {"status": "error", "input": "raw-string", "output": "fail",
                            "time": {"start": 1710079222000, "end": 1710079223000}}}}]}]}])
        conv = list(opencode.parse(src))[0]
        assert "Do something" in conv.prompts[0].content[0].content["text"]
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "thinking"]
        assert conv.prompts[0].responses[0].tool_calls[0].status == "error"

    def test_discover(self, tmp_path):
        d = tmp_path / "opencode"
        d.mkdir()
        sqlite3.connect(str(d / "opencode.db")).close()
        assert list(opencode.discover(locations=[str(d)])) and list(opencode.discover(locations=[str(tmp_path / "nope")])) == []
