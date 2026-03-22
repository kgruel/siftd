"""Tests for Codex CLI adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, CodexSession, fixture_source, write_jsonl

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
        # Out-of-order timestamps to cover L109 (started_at update)
        records = [{"type": "session_meta", "timestamp": "2024-01-01T00:00:05Z", "payload": {"id": "s", "cwd": "/w"}},
            {"type": "response_item", "timestamp": "2024-01-01T00:00:01Z", "payload": {"type": "message", "role": "user", "content": ["plain text"]}},
            {"type": "response_item", "timestamp": "2024-01-01T00:00:02Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "other_type", "data": 1}]}}]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records, "ts.jsonl"))))[0]
        assert "00:01" in conv.started_at and conv.prompts[0].content[0].content["text"] == "plain text"
