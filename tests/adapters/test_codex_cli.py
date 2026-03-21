"""Tests for Codex CLI adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, CodexSession, write_jsonl

from siftd.adapters import codex_cli
from siftd.domain.source import Source


def _fixture_source(tmp_path, fixture, subdir, dest_name=None):
    """Copy a fixture into a subdirectory and return a Source."""
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    dest = d / (dest_name or Path(fixture).name)
    dest.write_text((FIXTURES_DIR / fixture).read_text())
    return Source(kind="file", location=dest)


class TestCodexCliAdapter:

    def test_can_handle(self):
        assert codex_cli.can_handle(Source(kind="file", location=Path("/mock/sessions/test.jsonl")))
        assert not codex_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "codex_cli_minimal.jsonl"))

    def test_parse_full(self, tmp_path):
        codex_source = _fixture_source(tmp_path, "codex_cli_minimal.jsonl", "sessions")
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

    def test_pending_tools(self, tmp_path):
        records = [{"type": "session_meta", "timestamp": "T0", "payload": {"id": "s1", "cwd": "/p"}},
            {"type": "response_item", "timestamp": "T1", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}},
            {"type": "response_item", "timestamp": "T2", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}},
            {"type": "response_item", "timestamp": "T3", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}}]
        conv = list(codex_cli.parse(Source(kind="file", location=write_jsonl(tmp_path, records))))[0]
        assert any(tc.status == "pending" for p in conv.prompts for r in p.responses for tc in r.tool_calls)
        assert not codex_cli.can_handle(Source(kind="sqlite", location=Path("/mock/sessions/s.jsonl")))
