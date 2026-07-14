"""Tests for Codex CLI adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, CodexSession, default_location_source, fixture_source, write_jsonl

from siftd.adapters import codex_cli
from siftd.domain.source import Source


class TestCodexCliAdapter:

    def test_can_handle(self):
        assert codex_cli.can_handle(Source(kind="file", location=Path("/mock/sessions/test.jsonl")))
        assert not codex_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "codex_cli_minimal.jsonl"))

    def test_parse_full(self, tmp_path):
        codex_source = fixture_source(tmp_path, "codex_cli_minimal.jsonl", "sessions")
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


class TestCodexCliParseEdgeCases:
    def test_custom_and_function_tools(self, tmp_path):
        def _tcs(c):
            return [tc for p in c.prompts for r in p.responses for tc in r.tool_calls]
        conv = list(codex_cli.parse(Source(kind="file", location=CodexSession(tmp_path, exchanges=1).with_custom_tools(["my_tool"]).build())))[0]
        assert any(tc.tool_name == "my_tool" and tc.status == "success" for tc in _tcs(conv))
        conv2 = list(codex_cli.parse(Source(kind="file", location=CodexSession(tmp_path, exchanges=1, name="f.jsonl").with_tools(["shell"]).build())))[0]
        assert any(tc.tool_name == "shell" and tc.status == "success" for tc in _tcs(conv2))

    def test_can_handle_and_usage(self, tmp_path):
        d = tmp_path / "sessions" / "2024"
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text("{}\n")
        assert codex_cli.can_handle(Source(kind="file", location=d / "s.jsonl"))
        assert not codex_cli.can_handle(Source(kind="file", location=tmp_path / "other.jsonl"))
        records = [{"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "turn_context", "timestamp": "T1", "payload": {"model": "gpt-4"}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}},
            {"type": "event_msg", "timestamp": "T4", "payload": {"type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 30, "reasoning_output_tokens": 10}}}}]
        resp = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0].prompts[0].responses[0]
        assert resp.usage.input_tokens == 100 and resp.attributes.get("cached_input_tokens") == "30"

    def test_pending_tools_and_early_call(self, tmp_path):
        # function_call before assistant message → _get_or_create_response creates new (L289-292)
        records = [{"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}}]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert any(tc.status == "pending" for p in conv.prompts for r in p.responses for tc in r.tool_calls)
        assert not codex_cli.can_handle(Source(kind="sqlite", location=Path("/mock/sessions/s.jsonl")))

    def test_normalizer(self):
        n = codex_cli.normalize_record
        assert n({"type": "session_meta", "timestamp": "T", "payload": {"id": "s", "cwd": "/w"}}).session_id == "s"
        assert n({"type": "turn_context", "timestamp": "T", "payload": {"model": "m"}}).model == "m"
        u = n({"type": "response_item", "timestamp": "T", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]}})
        assert u.kind == "user" and u.content_blocks[0]["text"] == "q"
        a = n({"type": "response_item", "timestamp": "T", "payload": {"type": "message", "role": "assistant", "content": ["plain", {"type": "output_text", "text": "ok"}]}})
        assert a.kind == "assistant" and a.content_blocks[0]["text"] == "plain" and a.content_blocks[1]["text"] == "ok"
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "function_call", "name": "sh"}}).kind == "tool_use"
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "custom_tool_call", "name": "x"}}).tool_name == "x"
        ev = n({"type": "event_msg", "timestamp": "T", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 5, "output_tokens": 3}}}})
        assert ev.kind == "usage" and ev.input_tokens == 5
        assert n({"type": "event_msg", "timestamp": "T", "payload": {"type": "other"}}) is None
        assert n({"type": "unknown"}) is None
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "unknown"}}) is None

    def test_normalizer_block_edges(self):
        n = codex_cli.normalize_record
        a = n({"type": "response_item", "timestamp": "T", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "reasoning", "text": "think"}]}})
        assert a.content_blocks[0]["type"] == "reasoning"

    def test_discover_and_parse_edges(self, tmp_path):
        d = tmp_path / "sessions"
        d.mkdir()
        (d / "s.jsonl").write_text("{}\n")
        assert list(codex_cli.discover(locations=[str(tmp_path)]))
        (d / "empty.jsonl").write_text("")
        assert list(codex_cli.parse(Source(kind="file", location=d / "empty.jsonl"))) == []
        assert not codex_cli.can_handle(Source(kind="file", location=Path("/mock/sessions/test.json")))
        assert codex_cli.can_handle(default_location_source(codex_cli))
        # Out-of-order timestamps to cover L109 (started_at update)
        records = [{"type": "session_meta", "timestamp": "2024-01-01T00:00:05Z", "payload": {"id": "s", "cwd": "/w"}},
            {"type": "response_item", "timestamp": "2024-01-01T00:00:01Z", "payload": {"type": "message", "role": "user", "content": ["plain text"]}},
            {"type": "response_item", "timestamp": "2024-01-01T00:00:02Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "other_type", "data": 1}]}}]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "ts.jsonl"))))[0]
        assert "00:01" in conv.started_at and conv.prompts[0].content[0].content["text"] == "plain text"

    def test_tool_aliases_cover_exec_and_wait(self):
        assert codex_cli.TOOL_ALIASES["exec"] == "shell.execute"
        assert codex_cli.TOOL_ALIASES["wait"] == "shell.wait"

    def test_reasoning_summary_parse(self, tmp_path):
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "reasoning", "id": "r1", "encrypted_content": "xxx", "summary": [{"type": "summary_text", "text": "I will run ls"}]}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}},
        ]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "reason.jsonl"))))[0]
        blocks = [b for p in conv.prompts for r in p.responses for b in r.content]
        thinking = [b for b in blocks if b.block_type == "thinking"]
        assert thinking and thinking[0].content["thinking"] == "I will run ls"

    def test_reasoning_empty_summary_parse_is_noop(self, tmp_path):
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "reasoning", "id": "r1", "encrypted_content": "xxx", "summary": []}},
        ]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "reason2.jsonl"))))[0]
        # Empty summary must not synthesize a response
        assert conv.prompts[0].responses == []

    def test_reasoning_normalizer(self):
        n = codex_cli.normalize_record
        r = n({"type": "response_item", "timestamp": "T", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan"}]}})
        assert r.kind == "assistant" and r.content_blocks == [{"type": "thinking", "thinking": "plan"}]
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "reasoning", "summary": []}}) is None
        assert n({"type": "response_item", "timestamp": "T", "payload": {"type": "reasoning", "summary": None}}) is None

    def test_task_complete_parse_attributes(self, tmp_path):
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
            {"type": "event_msg", "timestamp": "T3", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 5}}}},
            {"type": "event_msg", "timestamp": "T4", "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "done", "duration_ms": 4321, "time_to_first_token_ms": 250}},
        ]
        resp = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "tc.jsonl"))))[0].prompts[0].responses[0]
        # token_count cleared pending_usage_response; task_complete still lands
        assert resp.usage.input_tokens == 10
        assert resp.attributes.get("duration_ms") == "4321"
        assert resp.attributes.get("time_to_first_token_ms") == "250"

    def test_task_complete_normalizer(self):
        n = codex_cli.normalize_record
        r = n({"type": "event_msg", "timestamp": "T", "payload": {"type": "task_complete", "duration_ms": 4321, "time_to_first_token_ms": 250}})
        assert r.kind == "metadata"
        assert r.extra == {"duration_ms": "4321", "time_to_first_token_ms": "250"}
        r2 = n({"type": "event_msg", "timestamp": "T", "payload": {"type": "task_complete"}})
        assert r2.kind == "metadata" and r2.extra == {}

    def test_orphan_tool_call_before_user_message(self, tmp_path):
        """Tool calls before any user message create a synthetic prompt."""
        records = [
            {"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {
                "type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
            {"type": "response_item", "timestamp": "T2", "payload": {
                "type": "function_call_output", "call_id": "c1", "output": "done"}},
        ]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "orphan.jsonl"))))[0]
        assert len(conv.prompts) == 1, "synthetic prompt should be created"
        assert len(conv.prompts[0].responses) == 1
        assert conv.prompts[0].responses[0].tool_calls[0].tool_name == "shell"
