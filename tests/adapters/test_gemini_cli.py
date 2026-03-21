"""Tests for Gemini CLI adapter."""

import json
from pathlib import Path

from conftest import FIXTURES_DIR

from siftd.adapters import gemini_cli
from siftd.domain.source import Source


def _fixture_source(tmp_path, fixture, subdir, dest_name=None):
    """Copy a fixture into a subdirectory and return a Source."""
    d = tmp_path / subdir
    d.mkdir(parents=True, exist_ok=True)
    dest = d / (dest_name or Path(fixture).name)
    dest.write_text((FIXTURES_DIR / fixture).read_text())
    return Source(kind="file", location=dest)


class TestGeminiCliAdapter:

    def test_can_handle(self):
        assert gemini_cli.can_handle(Source(kind="file", location=Path("/mock/chats/test.json")))
        assert not gemini_cli.can_handle(Source(kind="file", location=FIXTURES_DIR / "gemini_cli_minimal.json"))

    def test_parse_full(self, tmp_path):
        gemini_source = _fixture_source(tmp_path, "gemini_cli_minimal.json", "chats")
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
