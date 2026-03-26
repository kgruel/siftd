"""Tests for OpenCode adapter."""

import json
import sqlite3
from pathlib import Path

import pytest

from siftd.adapters import opencode
from siftd.adapters.sdk import AdapterParseError
from siftd.domain.source import Source

S = Source
_SCHEMA = [
    "CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT, version INTEGER, time_created INTEGER, time_updated INTEGER)",
    "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)",
    "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)",
]
_TS = 1710079200000


def _make_db(path, sessions=None):
    """Create an OpenCode SQLite test database. Returns Source."""
    conn = sqlite3.connect(str(path))
    for ddl in _SCHEMA:
        conn.execute(ddl)
    for s in (sessions or []):
        sid = s["session"][0]
        conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)", s["session"])
        for m in s.get("messages", []):
            conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", (m["id"], sid, m["ts"], m["ts"], json.dumps(m["data"])))
            for p in m.get("parts", []):
                conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (p["id"], m["id"], sid, p.get("ts", m["ts"]), p.get("ts", m["ts"]), json.dumps(p["data"])))
    conn.commit()
    conn.close()
    return S(kind="sqlite", location=path)


def _partial_db(path, schemas, rows=None):
    """Create a DB with subset of tables. Returns Source."""
    conn = sqlite3.connect(str(path))
    for i in schemas:
        conn.execute(_SCHEMA[i])
    for sql, params in (rows or []):
        conn.execute(sql, params)
    conn.commit()
    conn.close()
    return S(kind="sqlite", location=path)


class TestOpenCodeAdapter:
    @pytest.fixture
    def src(self, tmp_path):
        return _make_db(tmp_path / "opencode.db", sessions=[{
            "session": ("ses_001", "proj_001", "/test/workspace", "Test", 1, _TS, _TS + 60000),
            "messages": [
                {"id": "m1", "ts": _TS + 10000, "data": {"role": "user", "summary": {"title": "Run the tests"}},
                 "parts": [{"id": "p1", "data": {"type": "text", "text": "Run the tests please"}}]},
                {"id": "m2", "ts": _TS + 20000, "data": {"role": "assistant", "modelID": "claude-3-opus-20240229",
                    "providerID": "anthropic", "cost": 0.025,
                    "tokens": {"total": 680, "input": 500, "output": 120, "reasoning": 60, "cache": {"read": 50, "write": 10}},
                    "finish": "tool-calls"},
                 "parts": [{"id": "p2", "data": {"type": "text", "text": "I'll run the tests for you."}},
                    {"id": "p3", "ts": _TS + 25000, "data": {"type": "tool", "callID": "c1", "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "pytest"}, "output": "5 passed, 0 failed",
                            "time": {"start": _TS + 25000, "end": _TS + 28000}}}}]}]}])

    def test_can_handle(self):
        assert opencode.can_handle(S(kind="sqlite", location=Path("/mock/opencode.db")))
        assert not opencode.can_handle(S(kind="sqlite", location=Path("/mock/other.db")))
        assert not opencode.can_handle(S(kind="file", location=Path("/mock/opencode.db")))

    def test_parse_full(self, src):
        conv = list(opencode.parse(src))[0]
        assert conv.external_id == "opencode::ses_001" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "opencode" and "2024-03-10" in conv.started_at
        assert "Run the tests" in conv.prompts[0].content[0].content["text"]
        resp = conv.prompts[0].responses[0]
        assert resp.model == "claude-3-opus-20240229" and resp.usage.input_tokens == 500
        assert resp.attributes["cost"] == "0.025" and resp.attributes["cache_read_input_tokens"] == "50"
        assert resp.tool_calls[0].tool_name == "bash" and resp.tool_calls[0].status == "success"

    def test_parse_edge_cases(self, tmp_path):
        assert list(opencode.parse(_make_db(tmp_path / "empty.db"))) == []
        with pytest.raises(AdapterParseError, match="does not exist"):
            list(opencode.parse(S(kind="sqlite", location=tmp_path / "nope.db")))
        src = _make_db(tmp_path / "edge.db", sessions=[{
            "session": ("s2", "p2", "/ws", "Test", 1, _TS, _TS + 60000),
            "messages": [
                {"id": "m1", "ts": _TS + 10000, "data": {"role": "user", "summary": {"title": "Do something"}},
                 "parts": [{"id": "p0", "data": {"type": "text", "text": "hi"}}]},
                {"id": "m2", "ts": _TS + 20000, "data": {"role": "assistant", "modelID": "gpt-4", "tokens": {"input": 10, "output": 5}},
                 "parts": [{"id": "p1", "data": {"type": "reasoning", "text": "thinking..."}},
                    {"id": "p2", "ts": _TS + 21000, "data": {"type": "step-start"}},
                    {"id": "p3", "ts": _TS + 22000, "data": {"type": "tool", "callID": "c1", "tool": "bash",
                        "state": {"status": "error", "input": "raw-string", "output": "fail",
                            "time": {"start": _TS + 22000, "end": _TS + 23000}}}},
                    {"id": "p4", "data": {"type": "unknown-type"}},
                    {"id": "p5", "data": {"type": "tool", "tool": "x", "state": "not-a-dict"}},
                    {"id": "p6", "data": {"type": "tool", "tool": "x", "state": {"status": "running"}}}]},
                {"id": "m3", "ts": _TS + 30000, "data": "not-json-dict"}]}])
        # Inject bad part data into user message (L134) and assistant parts (L187)
        conn = sqlite3.connect(str(src.location))
        conn.execute("INSERT INTO part VALUES ('bp','m1','s2',?,?,'not-json')", (_TS, _TS))
        conn.execute("INSERT INTO part VALUES ('bp2','m2','s2',?,?,'')", (_TS, _TS))
        conn.commit()
        conn.close()
        conv = list(opencode.parse(src))[0]
        assert conv.prompts[0].content[0].content["text"] in ("hi", "Do something")
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "thinking"]
        tcs = conv.prompts[0].responses[0].tool_calls
        assert any(tc.status == "error" for tc in tcs) and any(tc.status == "pending" for tc in tcs)

    def test_parse_missing_tables(self, tmp_path):
        (tmp_path / "isdir.db").mkdir()
        with pytest.raises(AdapterParseError, match="is not a file"):
            list(opencode.parse(S(kind="sqlite", location=tmp_path / "isdir.db")))
        with pytest.raises(AdapterParseError, match="missing the session table"):
            list(opencode.parse(_partial_db(tmp_path / "notable.db", [])))
        ses_row = ("INSERT INTO session VALUES ('s','p','/w','T',1,?,?)", (_TS, _TS + 60000))
        with pytest.raises(AdapterParseError, match="missing the message table"):
            list(opencode.parse(_partial_db(tmp_path / "nomsg.db", [0], [ses_row])))
        msg_row = ("INSERT INTO message VALUES ('m','s',?,?,?)", (_TS, _TS, json.dumps({"role": "assistant", "modelID": "m"})))
        usr_row = ("INSERT INTO message VALUES ('u','s',?,?,?)", (_TS, _TS, json.dumps({"role": "user", "summary": {"title": "Hi"}})))
        assert list(opencode.parse(_partial_db(tmp_path / "nopart.db", [0, 1], [ses_row, usr_row, msg_row])))

    def test_parse_bad_json(self, tmp_path):
        src = _make_db(tmp_path / "badjson.db", sessions=[{
            "session": ("s1", "p1", "/ws", "T", 1, _TS, _TS + 60000),
            "messages": [
                {"id": "m1", "ts": _TS + 10000, "data": {"role": "user", "summary": {"title": "Hi"}},
                 "parts": [{"id": "p1", "data": {"type": "text", "text": "hi"}}]},
                {"id": "m2", "ts": _TS + 20000, "data": {"role": "assistant", "modelID": "m"},
                 "parts": [{"id": "p2", "data": {"type": "text", "text": "ok"}}]}]}])
        conn = sqlite3.connect(str(src.location))
        conn.execute("INSERT INTO message VALUES ('bad','s1',?,?,'not-valid-json')", (_TS, _TS))
        conn.execute("INSERT INTO message VALUES ('bad2','s1',?,?,?)", (_TS, _TS, '""'))
        conn.commit()
        conn.close()
        assert len(list(opencode.parse(src))) == 1

    def test_discover(self, tmp_path):
        d = tmp_path / "opencode"
        d.mkdir()
        sqlite3.connect(str(d / "opencode.db")).close()
        assert list(opencode.discover(locations=[str(d)])) and list(opencode.discover(locations=[str(tmp_path / "nope")])) == []
