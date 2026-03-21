"""Tests for Aider adapter."""

from pathlib import Path

from conftest import FIXTURES_DIR

from siftd.adapters import aider
from siftd.domain.source import Source


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

    def test_discover(self, tmp_path):
        (tmp_path / ".aider.chat.history.md").write_text("# aider\n")
        assert list(aider.discover(locations=[str(tmp_path)]))
        assert list(aider.discover(locations=[str(tmp_path / "nope")])) == []

    def test_analytics_and_tool_before_response(self, tmp_path):
        assert aider.can_handle(Source(kind="file", location=Path("/home/.aider/analytics.jsonl")))
        assert not aider.can_handle(Source(kind="file", location=Path("/random/analytics.jsonl")))
        d = tmp_path / ".aider"
        d.mkdir()
        (d / "analytics.jsonl").write_text('{"event": "test"}\n')
        assert list(aider.discover(locations=[str(d)]))
        md = "# aider chat started at 2025-01-01 00:00:00\n\n#### do it\n\n> Applied edit to main.py\n\nDone\n"
        (tmp_path / ".aider.chat.history.md").write_text(md)
        conv = list(aider.parse(Source(kind="file", location=tmp_path / ".aider.chat.history.md")))[0]
        assert conv.prompts[0].responses and any(b.block_type == "tool_output" for r in conv.prompts[0].responses for b in r.content)
