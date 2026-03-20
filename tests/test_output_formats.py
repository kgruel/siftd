"""Tests for output format render_list implementations."""

import json
from dataclasses import dataclass, field

import pytest

from painted import Fidelity


@dataclass
class FakeSummary:
    """Minimal ConversationSummary stand-in for testing."""

    id: str
    workspace_path: str | None
    model: str | None
    started_at: str | None
    prompt_count: int
    response_count: int
    total_tokens: int
    cost: float | None
    tags: list[str] = field(default_factory=list)


@pytest.fixture
def summaries():
    return [
        FakeSummary(
            id="01ABCDEF123456",
            workspace_path="/home/user/my-project",
            model="claude-opus-4-5-20251101",
            started_at="2026-03-15T10:30:00Z",
            prompt_count=3,
            response_count=5,
            total_tokens=1234,
            cost=0.034,
            tags=["review", "bug"],
        ),
        FakeSummary(
            id="02XYZABC789012",
            workspace_path="/home/user/other",
            model="gpt-4o",
            started_at="2026-03-14T08:00:00",
            prompt_count=1,
            response_count=1,
            total_tokens=500,
            cost=None,
            tags=[],
        ),
    ]


class TestTerminalRenderList:
    @staticmethod
    def _block_to_text(block):
        """Extract plain text from a painted Block."""
        lines = []
        for y in range(block.height):
            lines.append("".join(cell.char for cell in block.row(y)).rstrip())
        return "\n".join(lines)

    def test_brief_depth_shows_id_timestamp_workspace(self, summaries):
        from siftd.output.terminal_fmt import render_list

        fidelity = Fidelity(depth=0)
        block = render_list(summaries, fidelity)
        output = self._block_to_text(block)

        lines = output.strip().split("\n")
        # header + separator + 2 data rows
        assert len(lines) == 4
        assert "id" in lines[0]
        assert "─" in lines[1]

        # Data row: only id, timestamp, workspace
        assert "01ABCDEF1234" in lines[2]
        assert "my-project" in lines[2]
        # Should NOT have model, tokens, cost
        assert "claude-opus" not in lines[2]
        assert "tok" not in lines[2]
        assert "$" not in lines[2]

    def test_default_depth_shows_standard_columns(self, summaries):
        from siftd.output.terminal_fmt import render_list

        fidelity = Fidelity(depth=1)
        block = render_list(summaries, fidelity)
        output = self._block_to_text(block)

        lines = output.strip().split("\n")
        # header + separator + 2 data rows
        assert len(lines) == 4

        line = lines[2]  # first data row
        assert "01ABCDEF1234" in line
        assert "my-project" in line
        assert "claude-opus-4-5" in line  # model with date stripped
        assert "3p/5r" in line
        assert "1.2k" in line
        assert "$0.0340" in line
        # Tags not shown at default depth
        assert "review" not in line

    def test_full_depth_shows_table_with_tags(self, summaries):
        from siftd.output.terminal_fmt import render_list

        fidelity = Fidelity(depth=3)
        block = render_list(summaries, fidelity)
        output = self._block_to_text(block)

        lines = output.strip().split("\n")
        # header + separator + 2 data rows
        assert len(lines) == 4
        assert "id" in lines[0]
        assert "tags" in lines[0]
        assert "─" in lines[1]
        assert "review, bug" in lines[2]

    def test_empty_list_returns_none(self):
        from siftd.output.terminal_fmt import render_list

        output = render_list([], Fidelity(depth=1))
        assert output is None

    def test_none_cost_shows_zero(self, summaries):
        from siftd.output.terminal_fmt import render_list

        fidelity = Fidelity(depth=1)
        block = render_list(summaries, fidelity)
        output = self._block_to_text(block)

        lines = output.strip().split("\n")
        assert "$0.0000" in lines[3]  # second data row (after header + sep)


class TestMarkdownRenderList:
    def test_brief_depth_three_columns(self, summaries):
        from siftd.output.markdown_fmt import render_list

        fidelity = Fidelity(depth=0)
        output = render_list(summaries, fidelity)

        lines = output.strip().split("\n")
        # Header + separator + 2 data rows
        assert len(lines) == 4
        assert "| ID | Started | Workspace |" == lines[0]
        assert "| --- | --- | --- |" == lines[1]
        assert "01ABCDEF1234" in lines[2]
        assert "my-project" in lines[2]
        # No model column
        assert "Model" not in lines[0]

    def test_default_depth_seven_columns(self, summaries):
        from siftd.output.markdown_fmt import render_list

        fidelity = Fidelity(depth=1)
        output = render_list(summaries, fidelity)

        lines = output.strip().split("\n")
        assert "| ID | Started | Workspace | Model | Turns | Tokens | Cost |" == lines[0]
        assert lines[1].count("---") == 7
        assert "claude-opus-4-5" in lines[2]
        assert "3p/5r" in lines[2]

    def test_full_depth_includes_tags(self, summaries):
        from siftd.output.markdown_fmt import render_list

        fidelity = Fidelity(depth=3)
        output = render_list(summaries, fidelity)

        lines = output.strip().split("\n")
        assert "Tags" in lines[0]
        assert "review, bug" in lines[2]

    def test_empty_list_returns_empty_string(self):
        from siftd.output.markdown_fmt import render_list

        output = render_list([], Fidelity(depth=1))
        assert output == ""


class TestJsonRenderList:
    def test_includes_all_fields_regardless_of_depth(self, summaries):
        from siftd.output.json_fmt import render_list

        for depth in (0, 1, 3):
            output = render_list(summaries, Fidelity(depth=depth))
            data = json.loads(output)

            assert len(data) == 2
            entry = data[0]
            assert entry["id"] == "01ABCDEF123456"
            assert entry["workspace"] == "/home/user/my-project"
            assert entry["model"] == "claude-opus-4-5-20251101"
            assert entry["prompts"] == 3
            assert entry["responses"] == 5
            assert entry["tokens"] == 1234
            assert entry["cost"] == 0.034
            assert entry["tags"] == ["review", "bug"]

    def test_empty_list_returns_empty_array(self):
        from siftd.output.json_fmt import render_list

        output = render_list([], Fidelity(depth=1))
        assert json.loads(output) == []

    def test_null_cost_preserved(self, summaries):
        from siftd.output.json_fmt import render_list

        output = render_list(summaries, Fidelity(depth=1))
        data = json.loads(output)
        assert data[1]["cost"] is None


class TestFormatTable:
    def test_returns_string(self):
        from siftd.output.common import format_table

        result = format_table(["a", "b"], [["1", "22"], ["333", "4"]])
        lines = result.split("\n")
        assert len(lines) == 4  # header + sep + 2 rows
        assert "---" in lines[1]

    def test_print_table_delegates(self, capsys):
        from siftd.output.common import print_table

        print_table(["x"], [["y"]])
        captured = capsys.readouterr()
        assert "x" in captured.out
        assert "y" in captured.out


class TestFidelityFromArgsBrief:
    def test_brief_sets_depth_zero(self):
        from siftd.cli_common import fidelity_from_args

        args = type("Args", (), {"brief": True, "full": False, "thinking": False, "tools": None, "chars": None})()
        fidelity = fidelity_from_args(args)
        assert fidelity.depth == 0
        assert fidelity.chars == 80

    def test_default_depth_is_one(self):
        from siftd.cli_common import fidelity_from_args

        args = type("Args", (), {"brief": False, "full": False, "thinking": False, "tools": None, "chars": None})()
        fidelity = fidelity_from_args(args)
        assert fidelity.depth == 1

    def test_full_depth_is_three(self):
        from siftd.cli_common import fidelity_from_args

        args = type("Args", (), {"brief": False, "full": True, "thinking": False, "tools": None, "chars": None})()
        fidelity = fidelity_from_args(args)
        assert fidelity.depth == 3
