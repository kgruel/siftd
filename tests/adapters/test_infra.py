"""Tests for adapter infrastructure: validation, registry, SDK utilities."""

from types import ModuleType

from conftest import ClaudeSession, CodexSession, write_jsonl

import siftd.adapters.sdk as sdk
from siftd.adapters import aider, claude_code, codex_cli, copilot_cli, gemini_cli, pi_agent, vscode
from siftd.adapters.claude_code import normalize_record as claude_norm
from siftd.adapters.codex_cli import normalize_record as codex_norm
from siftd.adapters.registry import load_all_adapters, load_builtin_adapters, load_dropin_adapters, wrap_adapter_paths
from siftd.adapters.validation import ADAPTER_INTERFACE_VERSION, validate_adapter
from siftd.domain import Response


class TestValidateAdapter:
    def test_validation(self):
        mod = ModuleType("test_adapter")
        for attr, val in [("ADAPTER_INTERFACE_VERSION", ADAPTER_INTERFACE_VERSION), ("NAME", "test"),
                          ("DEFAULT_LOCATIONS", []), ("DEDUP_STRATEGY", "file"), ("HARNESS_SOURCE", "test")]:
            setattr(mod, attr, val)
        mod.discover, mod.can_handle, mod.parse = (lambda locations=None: []), (lambda source: False), (lambda source: iter([]))
        assert validate_adapter(mod, "test") is None
        mod.ADAPTER_INTERFACE_VERSION = 999
        assert "incompatible" in validate_adapter(mod, "x")
        mod.ADAPTER_INTERFACE_VERSION = ADAPTER_INTERFACE_VERSION
        mod2 = ModuleType("bad")
        assert "missing required attribute" in validate_adapter(mod2, "x")
        mod2.ADAPTER_INTERFACE_VERSION = "not_int"
        assert "must be" in validate_adapter(mod2, "x")
        mod.DEDUP_STRATEGY = "invalid"
        assert "DEDUP_STRATEGY" in validate_adapter(mod, "x")
        mod.DEDUP_STRATEGY = "file"
        delattr(mod, "parse")
        assert "missing required function" in validate_adapter(mod, "x")
        mod.parse = lambda source: iter([])
        mod.discover = lambda: []
        assert "locations" in validate_adapter(mod, "x")


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
        assert len(linker.get_pairs()) == 2 and linker.get_pairs()[0][2] is not None and len(linker.pending_uses()) == 1
        resp = Response(timestamp="T1")
        sdk.flush_pending_calls({"c1": (resp, "sh", {"cmd": "ls"}), "c2": (resp, "t", "raw")})
        assert len(resp.tool_calls) == 2 and all(tc.status == "pending" for tc in resp.tool_calls)

    def test_seek_last_lines(self, tmp_path):
        (tmp_path / "s.txt").write_text("a\nb\nc\n")
        (tmp_path / "e.txt").write_text("")
        assert sdk.seek_last_lines(tmp_path / "s.txt", 2) == ["b", "c"]
        assert sdk.seek_last_lines(tmp_path / "nope.txt", 5) == [] == sdk.seek_last_lines(tmp_path / "e.txt", 5)
        (tmp_path / "big.txt").write_text("\n".join(f"line {i}" for i in range(5000)) + "\n")
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
        def _recs(f):
            return list(sdk.iter_jsonl(f))
        assert sdk.peek_scan_from_records(_recs(ClaudeSession(tmp_path, exchanges=2).build()), claude_norm, default_session_id="test").exchange_count == 2
        r2 = sdk.peek_scan_from_records(_recs(CodexSession(tmp_path, exchanges=1, cwd="/proj", model="gpt-4", name="cx.jsonl").build()), codex_norm, default_session_id="fb")
        assert r2.workspace_path == "/proj" and r2.model == "gpt-4"
        assert sdk.peek_scan_from_records([], claude_norm, default_session_id="x") is None
        assert sdk.peek_scan_from_records(_recs(ClaudeSession(tmp_path, name="sub.jsonl").with_subagent("sub-1").build()), claude_norm, default_session_id="m").parent_session_id is not None
        ex = sdk.peek_exchanges_from_records(_recs(ClaudeSession(tmp_path, exchanges=3, name="t.jsonl").with_tools(["Read"]).build()), claude_norm, last_n=2, tool_aliases={"Read": "file.read"})
        assert len(ex) == 2 and ex[0].prompt_text and len(ex[0].tool_calls) >= 1
        assert sdk.peek_exchanges_from_records(_recs(CodexSession(tmp_path, exchanges=1, name="cu.jsonl").with_tools(["shell"]).with_usage().build()), codex_norm, last_n=5, tool_aliases={"shell": "shell.execute"})[0].input_tokens > 0
        assert sdk.peek_exchanges_from_records([{"type": "assistant", "timestamp": "T1", "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "init"}]}}], claude_norm, last_n=5)[0].prompt_text is None
        ex4 = sdk.peek_exchanges_from_records([
            {"type": "user", "timestamp": "T1", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
            {"type": "assistant", "timestamp": "T2", "message": {"role": "assistant", "model": "m",
                "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "done"}]}},
        ], claude_norm, last_n=5, include_thinking=True)
        assert any(b.block_type == "thinking" for b in ex4[0].narrative)
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
