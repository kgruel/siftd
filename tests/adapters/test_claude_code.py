"""Tests for Claude Code adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, default_location_source, write_jsonl

from siftd.adapters import claude_code
from siftd.domain.source import Source


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

    def test_string_content_and_subagent(self, tmp_path):
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

    def test_normalizer_edges(self):
        n = claude_code.normalize_record
        # String content (L307), list content (L308), unknown content type (L310)
        assert n({"type": "assistant", "timestamp": "T", "message": {"role": "assistant", "content": "text"}}).content_blocks[0]["text"] == "text"
        assert n({"type": "assistant", "timestamp": "T", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}).content_blocks
        assert n({"type": "assistant", "timestamp": "T", "message": {"role": "assistant", "content": 42}}).content_blocks == []
        # tool_result detection (L319)
        tr = n({"type": "user", "timestamp": "T", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x"}]}})
        assert tr.kind == "tool_result"
        # agentId extraction (L324) — only on user records
        u = n({"type": "user", "timestamp": "T", "agentId": "sub-1", "message": {"role": "user", "content": "hi"}})
        assert u.extra.get("agent_id") == "sub-1"

    def test_discover_and_parse_edges(self, tmp_path):
        assert list(claude_code.discover(locations=[str(FIXTURES_DIR)]))
        (tmp_path / "empty.jsonl").write_text("")
        assert list(claude_code.parse(Source(kind="file", location=tmp_path / "empty.jsonl"))) == []
        assert not claude_code.can_handle(Source(kind="directory", location=FIXTURES_DIR))
        assert claude_code.can_handle(default_location_source(claude_code))
        # Record with non-user/assistant type gets skipped (L167)
        records = [{"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "T1",
             "uuid": "u1", "message": {"role": "user", "content": "hi"}},
            {"type": "result", "sessionId": "s", "timestamp": "T2"},
            {"type": "assistant", "sessionId": "s", "timestamp": "T3", "uuid": "a1",
             "message": {"role": "assistant", "model": "m", "content": "ok"}}]
        conv = list(claude_code.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert len(conv.prompts) == 1
