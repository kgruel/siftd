"""Tests for Pi Agent adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR, default_location_source, fixture_source

from siftd.adapters import pi_agent
from siftd.domain.source import Source


class TestPiAgentAdapter:

    def test_can_handle(self):
        assert pi_agent.can_handle(Source(kind="file", location=Path("/mock/.pi/agent/sessions/test.jsonl")))
        assert not pi_agent.can_handle(Source(kind="file", location=FIXTURES_DIR / "pi_agent_minimal.jsonl"))
        assert not pi_agent.can_handle(Source(kind="directory", location=Path("/mock/.pi/agent/sessions")))
        assert pi_agent.can_handle(default_location_source(pi_agent))

    def test_parse_full(self, tmp_path):
        pi_source = fixture_source(tmp_path, "pi_agent_minimal.jsonl", ".pi/agent/sessions/--test--")
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
        # pi's input excludes the 50 cache-read tokens; siftd stores it inclusive: 500 + 50.
        assert resp.usage.input_tokens == 550 and resp.usage.output_tokens == 120 and resp.model == "claude-opus-4-6"
        assert resp.attributes["cache_read_input_tokens"] == "50" and resp.attributes["cost"] == "0.0111"
        assert [b for b in resp.content if b.block_type == "thinking" and "README" in b.content["text"]]

    def test_parse_empty_file(self, tmp_path):
        sessions_dir = tmp_path / ".pi" / "agent" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "empty.jsonl").write_text("")
        assert list(pi_agent.parse(Source(kind="file", location=sessions_dir / "empty.jsonl"))) == []
