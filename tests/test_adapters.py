"""Tests for conversation log adapters."""

import json
import shutil
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from conftest import FIXTURES_DIR, ClaudeSession, CodexSession, write_jsonl

import siftd.adapters.sdk as sdk
from siftd.adapters import aider, claude_code, codex_cli, copilot_cli, gemini_cli, opencode, pi_agent, vscode
from siftd.adapters.claude_code import normalize_record as claude_norm
from siftd.adapters.codex_cli import normalize_record as codex_norm
from siftd.adapters.registry import load_all_adapters, load_builtin_adapters, load_dropin_adapters, wrap_adapter_paths
from siftd.adapters.validation import ADAPTER_INTERFACE_VERSION, validate_adapter
from siftd.domain import Response
from siftd.domain.source import Source


class TestValidateAdapter:
    def test_validation(self):
        mod = ModuleType("test_adapter")
        for attr, val in [("ADAPTER_INTERFACE_VERSION", ADAPTER_INTERFACE_VERSION), ("NAME", "test"),
                          ("DEFAULT_LOCATIONS", []), ("DEDUP_STRATEGY", "file"), ("HARNESS_SOURCE", "test")]:
            setattr(mod, attr, val)
        mod.discover = lambda locations=None: []
        mod.can_handle = lambda source: False
        mod.parse = lambda source: iter([])
        assert validate_adapter(mod, "test") is None
        # Bad version
        mod.ADAPTER_INTERFACE_VERSION = 999
        assert "incompatible" in validate_adapter(mod, "x")
        mod.ADAPTER_INTERFACE_VERSION = ADAPTER_INTERFACE_VERSION
        # Missing attribute
        mod2 = ModuleType("bad")
        assert "missing required attribute" in validate_adapter(mod2, "x")
        # Wrong type
        mod2.ADAPTER_INTERFACE_VERSION = "not_int"
        assert "must be" in validate_adapter(mod2, "x")
        # Bad dedup
        mod.DEDUP_STRATEGY = "invalid"
        assert "DEDUP_STRATEGY" in validate_adapter(mod, "x")
        mod.DEDUP_STRATEGY = "file"
        # Missing callable
        delattr(mod, "parse")
        assert "missing required function" in validate_adapter(mod, "x")
        mod.parse = lambda source: iter([])
        # discover missing locations param
        mod.discover = lambda: []
        assert "locations" in validate_adapter(mod, "x")


def _fixture_source(tmp_path, fixture, subdir, dest_name=None):
    """Copy a fixture into a subdirectory and return a Source."""
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    dest = d / (dest_name or Path(fixture).name)
    dest.write_text((FIXTURES_DIR / fixture).read_text())
    return Source(kind="file", location=dest)


class TestClaudeCodeAdapter:

    def test_can_handle(self):
        assert claude_code.can_handle(Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl"))
        assert not claude_code.can_handle(Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json"))

    def test_parse_full(self):
        conv = list(claude_code.parse(Source(kind="file", location=FIXTURES_DIR / "claude_code_minimal.jsonl")))[0]
        assert conv.external_id == "claude_code::test-session-1" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "claude_code" and conv.harness.source == "anthropic"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert "Hello" in prompt.content[0].content["text"] and len(prompt.responses) == 2
        resp = prompt.responses[0]
        tc = resp.tool_calls[0]
        assert tc.tool_name == "Read" and tc.status == "success" and "Test Project" in str(tc.result)
        assert resp.usage.input_tokens == 100 and resp.usage.output_tokens == 50
        assert resp.attributes.get("cache_creation_input_tokens") == "10"


class TestCodexCliAdapter:

    def test_can_handle(self):
        assert codex_cli.can_handle(Source(kind="file", location=Path("/mock/sessions/test.jsonl")))
        assert not codex_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "codex_cli_minimal.jsonl"))

    def test_parse_full(self, tmp_path):
        codex_source = _fixture_source(tmp_path, "codex_cli_minimal.jsonl", "sessions")
        """Parse extracts conversation with metadata, prompts, tools, usage."""
        conv = list(codex_cli.parse(codex_source))[0]
        assert conv.external_id == "codex_cli::codex-session-1" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "codex_cli" and conv.harness.source == "openai"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert "Run ls" in prompt.content[0].content["text"] and len(prompt.responses) == 1
        resp = prompt.responses[0]
        tc = resp.tool_calls[0]
        assert tc.tool_name == "shell_command" and tc.status == "success" and "README.md" in str(tc.result)
        assert resp.usage.input_tokens == 120 and resp.usage.output_tokens == 45
        assert resp.attributes.get("cache_read_input_tokens") == "10"


class TestGeminiCliAdapter:

    def test_can_handle(self):
        assert gemini_cli.can_handle(Source(kind="file", location=Path("/mock/chats/test.json")))
        assert not gemini_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json"))

    def test_parse_full(self, tmp_path):
        gemini_source = _fixture_source(tmp_path, "gemini_cli_minimal.json", "chats")
        """Parse extracts conversation with metadata, prompts, tools, usage, thinking."""
        conv = list(gemini_cli.parse(gemini_source))[0]
        assert conv.external_id == "gemini_cli::gemini-session-1"
        assert conv.harness.name == "gemini_cli" and conv.harness.source == "google"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert "List the files" in prompt.content[0].content["text"]
        resp = prompt.responses[0]
        assert resp.model == "gemini-2.0-flash"
        assert resp.tool_calls[0].tool_name == "list_files" and resp.tool_calls[0].status == "success"
        assert resp.usage.input_tokens == 50 and resp.usage.output_tokens == 30
        thinking = [b for b in resp.content if b.block_type == "thinking"]
        assert thinking[0].content["subject"] == "Planning"


class TestAiderAdapter:

    def test_can_handle(self):
        assert aider.can_handle(Source(kind="file", location=Path("/project/.aider.chat.history.md")))
        assert not aider.can_handle(Source(kind="file", location=Path("/project/README.md")))
        assert not aider.can_handle(Source(kind="directory", location=Path("/project")))

    def test_parse_full(self):
        source = Source(kind="file", location=FIXTURES_DIR / ".aider.chat.history.md")
        convos = list(aider.parse(source))
        assert len(convos) == 2
        assert [c.external_id for c in aider.parse(source)] == [c.external_id for c in convos]
        conv = convos[0]
        assert conv.external_id.startswith("aider::") and "2025-07-15 14:32:01" in conv.external_id
        assert conv.started_at == "2025-07-15T14:32:01" and conv.harness.name == "aider"
        assert conv.workspace_path == str(FIXTURES_DIR) and len(conv.prompts) == 2
        assert "write a hello world script" in conv.prompts[0].content[0].content["text"]
        assert "now add a greeting function" in conv.prompts[1].content[0].content["text"]
        assert "that takes a name parameter" in conv.prompts[1].content[0].content["text"]
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "text" and "hello world" in b.content["text"].lower()]
        all_blocks = [b for r in conv.prompts[0].responses for b in r.content]
        assert [b for b in all_blocks if b.block_type == "tool_output" and "Applied edit" in b.content["text"]]
        resp_cost = next((r for r in conv.prompts[0].responses if r.attributes.get("approx_cost")), None)
        assert resp_cost and resp_cost.attributes["approx_cost"] == "0.01"
        assert convos[1].started_at == "2025-07-15T15:10:00"
        assert "fix the bug in auth.py" in convos[1].prompts[0].content[0].content["text"]

    def test_parse_empty_and_header_only(self, tmp_path):
        empty = tmp_path / "e.md"
        empty.write_text("")
        assert list(aider.parse(Source(kind="file", location=empty))) == []
        header = tmp_path / "h.md"
        header.write_text("\n# aider chat started at 2025-01-01 00:00:00\n\n")
        assert list(aider.parse(Source(kind="file", location=header))) == []

    def test_parse_token_count_helper(self):
        for raw, expected in [("4.5k", 4500), ("1.2k", 1200), ("256", 256), ("1.5M", 1_500_000), ("bad", None)]:
            assert aider._parse_token_count(raw) == expected


def _vscode_session_dir(tmp_path, fixture, ws="/test/workspace"):
    """Set up VSCode workspace dir structure with fixture. Returns Source."""
    h = tmp_path / "hash"
    cs = h / "chatSessions"
    cs.mkdir(parents=True)
    shutil.copy(FIXTURES_DIR / fixture, cs / Path(fixture).name)
    if ws:
        (h / "workspace.json").write_text(json.dumps({"folder": f"file://{ws}"}))
    return Source(kind="file", location=cs / Path(fixture).name)


class TestVscodeAdapter:
    def test_can_handle(self):
        assert vscode.can_handle(Source(kind="file", location=Path("/mock/chatSessions/test.json")))
        assert vscode.can_handle(Source(kind="file", location=Path("/mock/chatSessions/test.jsonl")))
        assert not vscode.can_handle(Source(kind="file", location=FIXTURES_DIR / "vscode_minimal.json"))
        assert not vscode.can_handle(Source(kind="directory", location=Path("/mock/chatSessions")))

    def test_parse_json_full(self, tmp_path):
        conv = list(vscode.parse(_vscode_session_dir(tmp_path, "vscode_minimal.json")))[0]
        assert conv.workspace_path == "/test/workspace" and conv.harness.name == "vscode"
        assert conv.started_at and "2024-02-15" in conv.started_at and conv.ended_at
        assert len(conv.prompts) == 2
        assert "read a file" in conv.prompts[0].content[0].content["text"]
        assert conv.prompts[0].responses[0].model == "gpt-4o"
        r0_text = [b for b in conv.prompts[0].responses[0].content if b.block_type == "text"]
        assert r0_text and "open()" in r0_text[0].content["text"]
        tc = conv.prompts[1].responses[0].tool_calls[0]
        assert tc.tool_name == "listFiles" and tc.result == {"files": ["README.md", "src/", "tests/"]}
        assert [b for b in conv.prompts[1].responses[0].content if b.block_type == "text_edit"]
        assert all(r.usage is None for p in conv.prompts for r in p.responses)

    def test_parse_jsonl_full(self, tmp_path):
        conv = list(vscode.parse(_vscode_session_dir(tmp_path, "vscode_minimal.jsonl")))[0]
        assert conv.workspace_path == "/test/workspace" and "2024-02-15" in conv.started_at
        assert len(conv.prompts) == 2 and conv.prompts[1].responses[0].tool_calls[0].tool_name == "listFiles"

    def test_parse_edge_cases(self, tmp_path):
        cs = tmp_path / "nw" / "chatSessions"
        cs.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", cs / "t.json")
        assert list(vscode.parse(Source(kind="file", location=cs / "t.json")))[0].workspace_path is None
        cs2 = tmp_path / "e" / "chatSessions"
        cs2.mkdir(parents=True)
        empty = {"version": 3, "sessionId": "e", "creationDate": 1708012345678, "requests": []}
        (cs2 / "e.json").write_text(json.dumps(empty))
        assert list(vscode.parse(Source(kind="file", location=cs2 / "e.json"))) == []
        (cs2 / "e.jsonl").write_text(json.dumps({"kind": 0, "v": empty}) + "\n")
        assert list(vscode.parse(Source(kind="file", location=cs2 / "e.jsonl"))) == []
        (cs2 / "s.json").write_text(json.dumps({**empty, "sessionId": "s",
            "requests": [{"requestId": "r1", "message": {"text": "Hello"}, "timestamp": 1708012345678,
                "modelId": "gpt-4o", "response": [{"kind": "markdownContent", "content": {"value": "Hi"}}], "responseId": "r1"}]}))
        assert list(vscode.parse(Source(kind="file", location=cs2 / "s.json")))

    def test_replay_path_helpers(self):
        obj = {"requests": [{"response": [], "result": None}]}
        vscode._set_at_path(obj, ["requests", 0, "result"], {"ok": True})
        assert obj["requests"][0]["result"] == {"ok": True}
        vscode._append_at_path(obj, ["requests", 0, "response"], [{"kind": "text"}])
        assert len(obj["requests"][0]["response"]) == 1
        obj2 = {"requests": []}
        vscode._append_at_path(obj2, ["requests"], [{"id": "r1"}])
        assert len(obj2["requests"]) == 1
        vscode._set_at_path(obj2, ["requests", 99, "result"], "v")  # noop
        assert len(obj2["requests"]) == 1


class TestPiAgentAdapter:

    def test_can_handle(self):
        assert pi_agent.can_handle(Source(kind="file", location=Path("/mock/.pi/agent/sessions/test.jsonl")))
        assert not pi_agent.can_handle(Source(kind="file", location=FIXTURES_DIR / "pi_agent_minimal.jsonl"))
        assert not pi_agent.can_handle(Source(kind="directory", location=Path("/mock/.pi/agent/sessions")))

    def test_parse_full(self, tmp_path):
        pi_source = _fixture_source(tmp_path, "pi_agent_minimal.jsonl", ".pi/agent/sessions/--test--")
        """Parse extracts conversation with metadata, prompts, tools, usage."""
        conv = list(pi_agent.parse(pi_source))[0]
        assert conv.external_id == "pi_agent::pi-session-001" and conv.workspace_path == "/test/workspace"
        assert conv.harness.name == "pi_agent" and conv.harness.source == "multi"
        assert len(conv.prompts) == 1
        prompt = conv.prompts[0]
        assert "Read the README" in prompt.content[0].content["text"]
        assert len(prompt.responses) == 2
        tc = prompt.responses[0].tool_calls[0]
        assert tc.tool_name == "Read" and tc.status == "success" and "Test Project" in str(tc.result)
        resp = conv.prompts[0].responses[0]
        assert resp.usage.input_tokens == 500 and resp.usage.output_tokens == 120 and resp.model == "claude-opus-4-6"
        assert resp.attributes["cache_read_input_tokens"] == "50" and resp.attributes["cost"] == "0.0111"
        assert [b for b in resp.content if b.block_type == "thinking" and "README" in b.content["text"]]

    def test_parse_empty_file(self, tmp_path):
        sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "empty.jsonl").write_text("")
        assert list(pi_agent.parse(Source(kind="file", location=sessions_dir / "empty.jsonl"))) == []


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


class TestCopilotCliAdapter:

    def test_can_handle(self):
        assert copilot_cli.can_handle(Source(kind="file", location=Path("/mock/.copilot/session-state/uuid/events.jsonl")))
        assert not copilot_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "copilot_cli_minimal.jsonl"))
        assert not copilot_cli.can_handle(Source(kind="directory", location=Path("/mock/.copilot/session-state")))

    def test_parse_full(self, tmp_path):
        copilot_source = _fixture_source(tmp_path, "copilot_cli_minimal.jsonl", ".copilot/session-state/test-uuid", "events.jsonl")
        conv = list(copilot_cli.parse(copilot_source))[0]
        assert conv.external_id == "copilot_cli::copilot-session-001" and conv.workspace_path == "/test/workspace"
        assert conv.branch == "main" and conv.harness.name == "copilot_cli" and len(conv.prompts) == 1
        assert "List the files" in conv.prompts[0].content[0].content["text"] and len(conv.prompts[0].responses) == 2
        tc = conv.prompts[0].responses[0].tool_calls[0]
        assert tc.tool_name == "bash" and tc.status == "success" and "README.md" in str(tc.result)
        assert [b for b in conv.prompts[0].responses[0].content if b.block_type == "thinking"]
        assert conv.prompts[0].responses[0].model == "claude-haiku-4.5"

    def test_parse_empty_file(self, tmp_path):
        d = tmp_path / ".copilot" / "session-state" / "uuid"
        d.mkdir(parents=True)
        (d / "events.jsonl").write_text("")
        assert list(copilot_cli.parse(Source(kind="file", location=d / "events.jsonl"))) == []


# =============================================================================
# SDK utility + peek tests
# =============================================================================


class TestSDK:
    def test_discover_and_build(self, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        for name in ("a.jsonl", "b.jsonl"):
            (d / name).write_text("{}\n")
        assert len(list(sdk.discover_files(None, [str(d)], ["*.jsonl"]))) == 2
        assert list(sdk.discover_files(None, ["/nonexistent"], ["*.jsonl"])) == []
        h = sdk.build_harness("test", "src", "jsonl")
        assert h.display_name == "Test" and h.name == "test"
        assert sdk.build_harness("t", "s", "j", display_name="X").display_name == "X"

    def test_timestamp_bounds_and_load_jsonl(self, tmp_path):
        assert sdk.timestamp_bounds([{"timestamp": "C"}, {"timestamp": "A"}]) == ("A", "C")
        assert sdk.timestamp_bounds([]) == (None, None)
        f = tmp_path / "data.jsonl"
        f.write_text('{"a":1}\nbad\n{"b":2}\n\n')
        records, errors = sdk.load_jsonl(f)
        assert len(records) == 2 and len(errors) == 1 and errors[0].line_number == 2

    def test_tool_call_linker_and_flush(self):
        linker = sdk.ToolCallLinker()
        linker.add_use("t1", name="file.read")
        linker.add_use("t2", name="shell")
        linker.add_result("t1", content="data")
        pairs = linker.get_pairs()
        assert len(pairs) == 2 and pairs[0][2] is not None and pairs[1][2] is None
        assert len(linker.pending_uses()) == 1
        # flush_pending_calls
        resp = Response(timestamp="T1")
        sdk.flush_pending_calls({"c1": (resp, "sh", {"cmd": "ls"}), "c2": (resp, "t", "raw")})
        assert len(resp.tool_calls) == 2 and all(tc.status == "pending" for tc in resp.tool_calls)
        assert resp.tool_calls[1].input == {"raw": "raw"}

    def test_seek_last_lines(self, tmp_path):
        (tmp_path / "s.txt").write_text("a\nb\nc\n")
        assert sdk.seek_last_lines(tmp_path / "s.txt", 2) == ["b", "c"]
        assert sdk.seek_last_lines(tmp_path / "nope.txt", 5) == []
        (tmp_path / "e.txt").write_text("")
        assert sdk.seek_last_lines(tmp_path / "e.txt", 5) == []
        (tmp_path / "big.txt").write_text("\n".join(f"line {i}" for i in range(5000)) + "\n")
        assert len(sdk.seek_last_lines(tmp_path / "big.txt", 5, chunk_size=256)) == 5
        assert sdk.seek_last_lines(tmp_path / "big.txt", 5, chunk_size=256)[-1] == "line 4999"
        assert len(sdk.seek_last_lines(tmp_path / "big.txt", 10000, chunk_size=256)) == 5000

    def test_text_helpers(self):
        blocks = [{"type": "text", "text": "hi"}, {"type": "image"}, {"type": "tool_use", "name": "f"},
                  {"type": "tool_result"}, {"type": "thinking", "thinking": "hmm"}, {"type": "other"}, "str"]
        r = sdk.extract_text_with_placeholders(blocks)
        assert all(s in r for s in ["hi", "[image]", "[tool: f]", "[tool result]", "[thinking]"])
        assert "[thinking] hmm" in sdk.extract_text_with_placeholders(blocks, include_thinking=True)
        assert "[thinking] t" in sdk.extract_text_with_placeholders([{"type": "thinking", "text": "t"}], include_thinking=True)
        assert sdk.extract_text_with_placeholders([{"type": "thinking"}], include_thinking=True) == "[thinking]"
        assert sdk.extract_text_with_placeholders([]) is None
        assert sdk._is_tool_placeholder_only("[tool: f]\n[tool: g]") and not sdk._is_tool_placeholder_only("")

    def test_extract_tool_hint(self):
        hints = {"file.read": ["file_path"], "shell.execute": ["command"]}
        assert sdk.extract_tool_hint("file.read", {"file_path": "/a/b/c/d/e.py"}, hints) == "d/e.py"
        assert sdk.extract_tool_hint("shell.execute", {"command": "ls"}, hints) == "ls"
        assert sdk.extract_tool_hint("unknown", {}, hints) is None
        assert sdk.extract_tool_hint("shell.execute", {"command": "x" * 100}, hints).endswith("...")
        assert sdk.extract_tool_hint("file.read", {"file_path": 123}, hints) is None
        assert sdk.extract_tool_hint("shell.execute", {"command": ""}, hints) is None

    def test_iter_jsonl_and_tail(self, tmp_path):
        f = write_jsonl(tmp_path, [{"a": 1}, {"b": 2}, {"c": 3}])
        assert len(list(sdk.iter_jsonl(f))) == 3
        assert list(sdk.iter_jsonl(tmp_path / "nope.jsonl")) == []
        (tmp_path / "bad.jsonl").write_text('{"ok":1}\nnot json\n')
        assert len(list(sdk.iter_jsonl(tmp_path / "bad.jsonl"))) == 1
        assert list(sdk.peek_jsonl_tail(f, 2))[-1] == {"c": 3}
        assert all(isinstance(r, str) for r in sdk.peek_jsonl_tail(f, 2, parse_json=False))

    def test_peek_scan_exchanges_hooks(self, tmp_path):

        # Scan: Claude 2 exchanges, Codex with metadata, empty, subagent
        recs = list(sdk.iter_jsonl(ClaudeSession(tmp_path, exchanges=2).build()))
        assert sdk.peek_scan_from_records(recs, claude_norm, default_session_id="test").exchange_count == 2
        recs2 = list(sdk.iter_jsonl(CodexSession(tmp_path, exchanges=1, cwd="/proj", model="gpt-4", name="cx.jsonl").build()))
        r2 = sdk.peek_scan_from_records(recs2, codex_norm, default_session_id="fb")
        assert r2.workspace_path == "/proj" and r2.model == "gpt-4"
        assert sdk.peek_scan_from_records([], claude_norm, default_session_id="x") is None
        recs3 = list(sdk.iter_jsonl(ClaudeSession(tmp_path, name="sub.jsonl").with_subagent("sub-1").build()))
        assert sdk.peek_scan_from_records(recs3, claude_norm, default_session_id="m").parent_session_id is not None
        # Exchanges: Claude with tools, Codex with usage, assistant-first, thinking
        recs4 = list(sdk.iter_jsonl(ClaudeSession(tmp_path, exchanges=3, name="t.jsonl").with_tools(["Read"]).build()))
        ex = sdk.peek_exchanges_from_records(recs4, claude_norm, last_n=2, tool_aliases={"Read": "file.read"})
        assert len(ex) == 2 and ex[0].prompt_text and len(ex[0].tool_calls) >= 1
        recs5 = list(sdk.iter_jsonl(CodexSession(tmp_path, exchanges=1, name="cu.jsonl").with_tools(["shell"]).with_usage().build()))
        assert sdk.peek_exchanges_from_records(recs5, codex_norm, last_n=5, tool_aliases={"shell": "shell.execute"})[0].input_tokens > 0
        ex3 = sdk.peek_exchanges_from_records([{"type": "assistant", "timestamp": "T1", "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "init"}]}}], claude_norm, last_n=5)
        assert len(ex3) == 1 and ex3[0].prompt_text is None
        ex4 = sdk.peek_exchanges_from_records([
            {"type": "user", "timestamp": "T1", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
            {"type": "assistant", "timestamp": "T2", "message": {"role": "assistant", "model": "m",
                "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "done"}]}},
        ], claude_norm, last_n=5, include_thinking=True)
        assert any(b.block_type == "thinking" for b in ex4[0].narrative)
        # make_peek_hooks
        scan, exchanges, tail = sdk.make_peek_hooks(claude_norm, tool_aliases={"Read": "file.read"})
        f = ClaudeSession(tmp_path, exchanges=2, name="h.jsonl").with_tools(["Read"]).build()
        assert scan(f).exchange_count == 2 and len(exchanges(f, last_n=1)) == 1 and list(tail(f, 1))


class TestRegistryAndDiscover:
    def test_load_adapters(self, tmp_path):
        builtins = load_builtin_adapters()
        assert len(builtins) >= 8 and "claude_code" in {p.name for p in builtins}
        assert load_dropin_adapters(tmp_path) == []
        assert len(load_all_adapters(dropin_path=tmp_path)) >= 8
        wrapped = wrap_adapter_paths(claude_code, ["/custom"])
        assert wrapped.DEFAULT_LOCATIONS == ["/custom"] and wrapped.NAME == claude_code.NAME

    def test_discover_all_adapters(self, tmp_path):
        def _touch(rel):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}\n")
        _touch("proj/p/s.jsonl")
        assert list(claude_code.discover(locations=[str(tmp_path / "proj")]))
        _touch("sessions/2024/r.jsonl")
        assert list(codex_cli.discover(locations=[str(tmp_path / "sessions")]))
        _touch("h/chats/s.json")
        assert list(gemini_cli.discover(locations=[str(tmp_path)]))
        (tmp_path / ".aider.chat.history.md").write_text("# aider\n")
        assert list(aider.discover(locations=[str(tmp_path)]))
        _touch("pi/s.jsonl")
        assert list(pi_agent.discover(locations=[str(tmp_path / "pi")]))
        _touch("ws/h/chatSessions/h.json")
        assert list(vscode.discover(locations=[str(tmp_path / "ws")]))
        _touch("ss/u/events.jsonl")
        assert list(copilot_cli.discover(locations=[str(tmp_path / "ss")]))


# =============================================================================
# Additional adapter parse edge cases
# =============================================================================


class TestCodexCliParseEdgeCases:
    def test_custom_and_function_tools(self, tmp_path):
        # Custom tool_call/output
        conv = list(codex_cli.parse(Source(kind="file", location=CodexSession(tmp_path, exchanges=1).with_custom_tools(["my_tool"]).build())))[0]
        tcs = [tc for p in conv.prompts for r in p.responses for tc in r.tool_calls]
        assert any(tc.tool_name == "my_tool" and tc.status == "success" for tc in tcs)
        # function_call/output
        conv2 = list(codex_cli.parse(Source(kind="file", location=CodexSession(tmp_path, exchanges=1, name="f.jsonl").with_tools(["shell"]).build())))[0]
        tcs2 = [tc for p in conv2.prompts for r in p.responses for tc in r.tool_calls]
        assert any(tc.tool_name == "shell" and tc.status == "success" for tc in tcs2)

    def test_can_handle_and_usage(self, tmp_path):
        d = tmp_path / "sessions" / "2024"
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text("{}\n")
        assert codex_cli.can_handle(Source(kind="file", location=d / "s.jsonl"))
        assert not codex_cli.can_handle(Source(kind="file", location=tmp_path / "other.jsonl"))
        # Usage with cached/reasoning tokens
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "turn_context", "timestamp": "T1", "payload": {"model": "gpt-4"}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hi"}]}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}]}},
            {"type": "event_msg", "timestamp": "T4", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50,
                    "cached_input_tokens": 30, "reasoning_output_tokens": 10}}}},
        ]
        resp = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0].prompts[0].responses[0]
        assert resp.usage.input_tokens == 100 and resp.attributes.get("cached_input_tokens") == "30"


class TestGeminiCliParseEdgeCases:
    def test_parse_with_tools_and_thinking(self, tmp_path):
        conv = list(gemini_cli.parse(Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json")))[0]
        assert conv.external_id.startswith("gemini_cli::") and conv.prompts[0].responses[0].model
        session = {"sessionId": "gem-1", "startTime": "T1", "lastUpdated": "T2", "messages": [
            {"type": "user", "id": "u1", "timestamp": "T1", "content": "search"},
            {"type": "gemini", "id": "g1", "timestamp": "T2", "model": "gemini-2.5", "content": "Found",
             "tokens": {"input": 100, "output": 50},
             "thoughts": [{"subject": "Planning", "description": "need to search"}],
             "toolCalls": [{"id": "tc1", "name": "search_files", "args": {"pattern": "*.py"},
                 "status": "success", "timestamp": "T2",
                 "result": [{"functionResponse": {"response": {"files": ["a.py"]}}}]}]}]}
        (tmp_path / "s.json").write_text(json.dumps(session))
        r = list(gemini_cli.parse(Source(kind="file", location=tmp_path / "s.json")))[0].prompts[0].responses[0]
        assert r.usage.input_tokens == 100 and r.tool_calls[0].tool_name == "search_files"
        assert [b for b in r.content if b.block_type == "thinking"]

    def test_parse_empty_peek_discover_tail(self, tmp_path):
        (tmp_path / "e.json").write_text("{}")
        (tmp_path / "n.json").write_text(json.dumps({"sessionId": "x"}))
        assert list(gemini_cli.parse(Source(kind="file", location=tmp_path / "e.json"))) == []
        assert list(gemini_cli.parse(Source(kind="file", location=tmp_path / "n.json"))) == []
        assert gemini_cli.peek_scan(FIXTURES_DIR / "gemini_cli_minimal.json").exchange_count >= 1
        assert gemini_cli.peek_exchanges(FIXTURES_DIR / "gemini_cli_minimal.json", last_n=5)
        assert gemini_cli.peek_scan(tmp_path / "e.json") is None
        assert list(gemini_cli.peek_tail(FIXTURES_DIR / "gemini_cli_minimal.json", lines=5))
        assert not gemini_cli.can_handle(Source(kind="sqlite", location=tmp_path / "e.json"))


class TestVSCodeNormalizerAndPeek:
    def test_peek_json_and_jsonl(self, tmp_path):
        cs = tmp_path / "ws" / "chatSessions"
        cs.mkdir(parents=True)
        shutil.copy(FIXTURES_DIR / "vscode_minimal.json", cs / "test.json")
        shutil.copy(FIXTURES_DIR / "vscode_minimal.jsonl", cs / "test.jsonl")
        assert vscode.peek_scan(cs / "test.json").exchange_count >= 1
        assert vscode.peek_exchanges(cs / "test.json", last_n=5)[0].prompt_text
        assert vscode.peek_scan(cs / "test.jsonl").exchange_count >= 1
        (tmp_path / "chatSessions").mkdir()
        (tmp_path / "chatSessions" / "e.json").write_text("{}")
        assert vscode.peek_scan(tmp_path / "chatSessions" / "e.json") is None


# =============================================================================
# Final edge case coverage
# =============================================================================


class TestAdapterEdgeCases:

    def test_claude_code_string_content_and_subagent(self, tmp_path):
        records = [
            {"type": "user", "sessionId": "s1", "agentId": "sub-1", "cwd": "/ws", "timestamp": "T1",
             "uuid": "u1", "message": {"role": "user", "content": "plain string"}},
            {"type": "assistant", "sessionId": "s1", "timestamp": "T2", "uuid": "a1",
             "message": {"role": "assistant", "model": "claude-3", "content": None,
                 "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}}},
        ]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert "agent" in conv.external_id and conv.prompts[0].content[0].content["text"] == "plain string"
        assert conv.prompts[0].responses[0].attributes.get("cache_read_input_tokens") == "3"
        n = claude_code.normalize_record
        assert n({"type": "user", "timestamp": "T1", "message": {"role": "user", "content": "hi"}}).kind == "user"
        assert n({"type": "assistant", "timestamp": "T2", "message": {"role": "assistant", "content": None}}).content_blocks == []
        assert n({"type": "system"}) is None
        assert not claude_code.can_handle(Source(kind="file", location=Path("/home/.codex/sessions/s.jsonl")))

    def test_codex_pending_tools_and_can_handle(self, tmp_path):
        records = [{"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}}]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert any(tc.status == "pending" for p in conv.prompts for r in p.responses for tc in r.tool_calls)
        assert not codex_cli.can_handle(Source(kind="sqlite", location=Path("/mock/sessions/s.jsonl")))

    def test_aider_discover(self, tmp_path):
        (tmp_path / ".aider.chat.history.md").write_text("# aider\n")
        assert list(aider.discover(locations=[str(tmp_path)]))
        assert list(aider.discover(locations=[str(tmp_path / "nope")])) == []
