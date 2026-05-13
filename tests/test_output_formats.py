"""Tests for output format rendering: lists, search, detail, narrative."""

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
        assert "01ABCDEF" in lines[2]
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
        assert "01ABCDEF" in line
        assert "my-project" in line
        assert "claude-opus-4-5" in line  # model with date stripped
        assert "3p/5r" in line
        assert "1.2k" in line
        # Cost lives at depth>=3; tags too
        assert "$" not in line
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

    def test_none_cost_renders_question_mark_at_full_depth(self, summaries):
        from siftd.output.terminal_fmt import render_list

        fidelity = Fidelity(depth=3)
        block = render_list(summaries, fidelity)
        output = self._block_to_text(block)

        lines = output.strip().split("\n")
        # gpt-4o row has cost=None — renderer is honest, no caveat required
        assert "$0.0340" in lines[2]
        cost_cell_present = any("?" in cell for cell in lines[3].split())
        assert cost_cell_present, lines[3]
        assert "$0.0000" not in lines[3]

    def test_caveat_footer_summarizes_kinds(self, summaries):
        from siftd.doctor.checks import Finding
        from siftd.output.terminal_fmt import render_list

        caveats = [
            Finding(
                check="pricing-missing",
                severity="warning",
                message=f"No pricing data for {s.model}",
                fix_available=False,
                context={"model": s.model},
                target=s.id,
            )
            for s in summaries
        ]
        block = render_list(summaries, Fidelity(depth=3), caveats=caveats)
        output = self._block_to_text(block)
        assert "2 row(s) with pricing-missing" in output

    def test_no_caveats_no_footer(self, summaries):
        from siftd.output.terminal_fmt import render_list

        block = render_list(summaries, Fidelity(depth=3), caveats=[])
        output = self._block_to_text(block)
        assert "row(s) with" not in output


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
        assert "01ABCDEF" in lines[2]
        assert "my-project" in lines[2]
        # No model column
        assert "Model" not in lines[0]

    def test_default_depth_six_columns(self, summaries):
        from siftd.output.markdown_fmt import render_list

        fidelity = Fidelity(depth=1)
        output = render_list(summaries, fidelity)

        lines = output.strip().split("\n")
        # Cost moved to depth>=3; default depth has six columns
        assert "| ID | Started | Workspace | Model | Turns | Tokens |" == lines[0]
        assert lines[1].count("---") == 6
        assert "claude-opus-4-5" in lines[2]
        assert "3p/5r" in lines[2]
        assert "Cost" not in lines[0]

    def test_full_depth_includes_cost_and_tags(self, summaries):
        from siftd.output.markdown_fmt import render_list

        fidelity = Fidelity(depth=3)
        output = render_list(summaries, fidelity)

        lines = output.strip().split("\n")
        assert "Cost" in lines[0]
        assert "Tags" in lines[0]
        assert "review, bug" in lines[2]
        assert "$0.0340" in lines[2]

    def test_none_cost_renders_question_mark(self, summaries):
        from siftd.output.markdown_fmt import render_list

        output = render_list(summaries, Fidelity(depth=3))
        lines = output.strip().split("\n")
        # First row has cost=0.034, second has cost=None
        assert "$0.0340" in lines[2]
        cells = [cell.strip() for cell in lines[3].split("|")]
        assert "?" in cells
        assert "$0.0000" not in lines[3]

    def test_empty_list_returns_empty_string(self):
        from siftd.output.markdown_fmt import render_list

        output = render_list([], Fidelity(depth=1))
        assert output == ""


class TestHtmlRenderList:
    def test_default_depth_no_cost_column(self, summaries):
        from siftd.output.html_fmt import render_list

        output = render_list(summaries, Fidelity(depth=1))
        # Header presence
        assert "<th class=\"model\">Model</th>" in output
        assert "<th class=\"metric\">Tokens</th>" in output
        # Cost at depth>=3 only
        assert "<th class=\"metric\">Cost</th>" not in output

    def test_full_depth_includes_cost(self, summaries):
        from siftd.output.html_fmt import render_list

        output = render_list(summaries, Fidelity(depth=3))
        assert "<th class=\"metric\">Cost</th>" in output
        assert "$0.0340" in output

    def test_none_cost_renders_question_mark(self, summaries):
        from siftd.output.html_fmt import render_list

        output = render_list(summaries, Fidelity(depth=3))
        # First row has cost=0.034 (keeps the dollar amount),
        # second row has cost=None (renders '?')
        assert '<td class="metric missing">?</td>' in output
        assert "$0.0340" in output
        assert "$0.0000" not in output


class TestJsonRenderList:
    def test_includes_all_fields_regardless_of_depth(self, summaries):
        from siftd.output.json_fmt import render_list

        for depth in (0, 1, 3):
            output = render_list(summaries, Fidelity(depth=depth))
            data = json.loads(output)

            assert set(data.keys()) == {"result", "caveats"}
            assert len(data["result"]) == 2
            entry = data["result"][0]
            assert entry["id"] == "01ABCDEF123456"
            assert entry["workspace"] == "/home/user/my-project"
            assert entry["model"] == "claude-opus-4-5-20251101"
            assert entry["prompts"] == 3
            assert entry["responses"] == 5
            assert entry["tokens"] == 1234
            assert entry["cost"] == 0.034
            assert entry["tags"] == ["review", "bug"]

    def test_empty_list_returns_envelope_with_empty_result(self):
        from siftd.output.json_fmt import render_list

        output = render_list([], Fidelity(depth=1))
        data = json.loads(output)
        assert data == {"result": [], "caveats": []}

    def test_null_cost_preserved(self, summaries):
        from siftd.output.json_fmt import render_list

        output = render_list(summaries, Fidelity(depth=1))
        data = json.loads(output)
        assert data["result"][1]["cost"] is None

    def test_caveats_emitted_when_present(self, summaries):
        from siftd.doctor.checks import Finding
        from siftd.output.json_fmt import render_list

        caveat = Finding(
            check="pricing-missing",
            severity="warning",
            message="No pricing data for gpt-4o",
            fix_available=False,
            context={"model": "gpt-4o"},
            target="02XYZABC789012",
        )
        output = render_list(summaries, Fidelity(depth=3), caveats=[caveat])
        data = json.loads(output)
        assert len(data["caveats"]) == 1
        assert data["caveats"][0]["check"] == "pricing-missing"
        assert data["caveats"][0]["target"] == "02XYZABC789012"
        assert data["caveats"][0]["context"] == {"model": "gpt-4o"}


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


# ---------------------------------------------------------------------------
# render_search tests — exercises all 3 modes across formats
# ---------------------------------------------------------------------------


def _chunk_result(**overrides):
    """Build a minimal search result dict."""
    base = {
        "conversation_id": "01ABC123456789",
        "chunk_id": "chunk001",
        "score": 0.85,
        "chunk_type": "response",
        "text": "The answer is 42.",
        "source_ids": [],
        "_started_at": "2026-03-15",
        "_workspace": "my-project",
    }
    base.update(overrides)
    return base


def _conv_result(**overrides):
    """Build a minimal conversation-mode search result."""
    base = {
        "conversation_id": "01ABC123456789",
        "max_score": 0.92,
        "mean_score": 0.75,
        "chunk_count": 3,
        "_started_at": "2026-03-15",
        "_workspace": "my-project",
        "best_excerpt": "The answer is 42.",
    }
    base.update(overrides)
    return base


class TestJsonRenderSearch:
    def test_chunks_mode(self):
        from siftd.output.json_fmt import render_search

        result = render_search(
            [_chunk_result()], Fidelity(depth=1), query="meaning of life", mode="chunks"
        )
        assert result["mode"] == "chunks"
        assert result["query"] == "meaning of life"
        assert result["result_count"] == 1
        assert result["results"][0]["score"] == 0.85

    def test_conversations_mode(self):
        from siftd.output.json_fmt import render_search

        result = render_search(
            [_conv_result()], Fidelity(depth=1), query="q", mode="conversations"
        )
        assert result["mode"] == "conversations"
        assert result["results"][0]["max_score"] == 0.92
        assert result["results"][0]["chunk_count"] == 3

    def test_thread_mode(self):
        from siftd.output.json_fmt import render_search

        tier1 = [_chunk_result(text="expanded")]
        tier2 = [_chunk_result(text="compact", score=0.5)]
        result = render_search(
            [], Fidelity(depth=1), query="q", mode="thread", tier1=tier1, tier2=tier2
        )
        assert result["mode"] == "thread"
        assert result["result_count"] == 2
        assert len(result["tier1"]) == 1
        assert len(result["tier2"]) == 1

    def test_chunk_with_file_refs(self):
        from siftd.output.json_fmt import render_search

        @dataclass
        class Ref:
            basename: str
            path: str
            op: str
            content: str | None = None

        chunk = _chunk_result(file_refs=[Ref("f.py", "/f.py", "r", "import os")])
        result = render_search([chunk], Fidelity(depth=1), query="q")
        refs = result["results"][0]["file_refs"]
        assert len(refs) == 1
        assert refs[0]["basename"] == "f.py"
        assert refs[0]["content_length"] == 9  # len("import os")

    def test_render_workspaces(self):
        from siftd.output.json_fmt import render_workspaces

        result = render_workspaces([], Fidelity(depth=1))
        assert result == {"workspaces": []}

    def test_render_tags(self):
        from siftd.output.json_fmt import render_tags

        result = render_tags([], Fidelity(depth=1))
        assert result == {"tags": []}

    def test_chunk_with_breakdown(self):
        from siftd.output.json_fmt import render_search
        from siftd.search import ScoreBreakdown

        bd = ScoreBreakdown(embedding_sim=0.85, recency_boost=1.1)
        chunk = _chunk_result(breakdown=bd)
        result = render_search([chunk], Fidelity(depth=1), query="q")
        assert "breakdown" in result["results"][0]

    def test_caveats_included_in_envelope(self):
        from siftd.doctor.checks import Finding
        from siftd.output.json_fmt import render_search

        caveat = Finding(
            check="embeddings-stale",
            severity="warning",
            message="Embeddings index is stale",
            fix_available=True,
        )
        result = render_search(
            [_chunk_result()], Fidelity(depth=1), query="q", caveats=[caveat]
        )
        assert "caveats" in result
        assert len(result["caveats"]) == 1
        assert result["caveats"][0]["message"] == "Embeddings index is stale"
        assert result["caveats"][0]["check"] == "embeddings-stale"

    def test_no_caveats_yields_empty_list(self):
        from siftd.output.json_fmt import render_search

        result = render_search([_chunk_result()], Fidelity(depth=1), query="q")
        assert result["caveats"] == []

    def test_caveats_present_in_all_modes(self):
        from siftd.doctor.checks import Finding
        from siftd.output.json_fmt import render_search

        caveat = Finding(
            check="fts-stale", severity="warning", message="FTS index stale", fix_available=False
        )
        for mode, kwargs in [
            ("chunks", {"mode": "chunks"}),
            ("conversations", {"mode": "conversations"}),
            ("thread", {"mode": "thread", "tier1": [], "tier2": []}),
        ]:
            result = render_search([], Fidelity(depth=1), query="q", caveats=[caveat], **kwargs)
            assert result["caveats"][0]["check"] == "fts-stale", f"missing in {mode} mode"


class TestMarkdownRenderSearch:
    def test_chunks_mode(self):
        from siftd.output.markdown_fmt import render_search

        output = render_search(
            [_chunk_result()], Fidelity(depth=1), query="test query", mode="chunks"
        )
        assert "## Results for: test query" in output
        assert "01ABC123" in output
        assert "0.850" in output

    def test_conversations_mode(self):
        from siftd.output.markdown_fmt import render_search

        output = render_search(
            [_conv_result()], Fidelity(depth=1), query="q", mode="conversations"
        )
        assert "## Conversations for: q" in output
        assert "0.920" in output

    def test_thread_mode_with_exchanges(self):
        from siftd.output.markdown_fmt import render_search

        tier1 = [_chunk_result(_exchanges=[("p1", "What?", "That.")])]
        output = render_search(
            [], Fidelity(depth=1), query="q", mode="thread", tier1=tier1
        )
        assert "> What?" in output
        assert "That." in output

    def test_thread_mode_text_fallback(self):
        from siftd.output.markdown_fmt import render_search

        tier1 = [_chunk_result(text="fallback text")]
        tier2 = [_chunk_result(text="compact", score=0.4)]
        output = render_search(
            [], Fidelity(depth=1), query="q", mode="thread", tier1=tier1, tier2=tier2
        )
        assert "fallback text" in output
        assert "compact" in output
        assert "More results" in output

    def test_chunks_mode_with_exchanges(self):
        from siftd.output.markdown_fmt import render_search

        chunk = _chunk_result(_exchanges=[("p1", "Ask", "Answer")])
        output = render_search([chunk], Fidelity(depth=1), query="q")
        assert "> Ask" in output
        assert "Answer" in output

    def test_chunks_mode_with_context(self):
        from siftd.output.markdown_fmt import render_search

        chunk = _chunk_result(_context=[("p1", "Q?", "A!", True), ("p2", "Q2", "A2", False)])
        output = render_search([chunk], Fidelity(depth=1), query="q")
        assert "**>>>**" in output  # matched entry
        assert "Q?" in output

    def test_chunks_mode_text_truncation(self):
        from siftd.output.markdown_fmt import render_search

        long_text = "x" * 500
        chunk = _chunk_result(text=long_text)
        output = render_search([chunk], Fidelity(depth=0), query="q")
        assert "..." in output  # truncated at depth 0

    def test_caveats_footer_appended(self):
        from siftd.doctor.checks import Finding
        from siftd.output.markdown_fmt import render_search

        caveat = Finding(
            check="search-mode-degraded",
            severity="warning",
            message="Semantic search unavailable; using FTS5",
            fix_available=False,
        )
        output = render_search(
            [_chunk_result()], Fidelity(depth=1), query="q", caveats=[caveat]
        )
        assert "> **Note:** Semantic search unavailable; using FTS5" in output

    def test_no_caveats_no_footer(self):
        from siftd.output.markdown_fmt import render_search

        output = render_search([_chunk_result()], Fidelity(depth=1), query="q")
        assert "> **Note:**" not in output

    def test_caveats_footer_in_conversations_mode(self):
        from siftd.doctor.checks import Finding
        from siftd.output.markdown_fmt import render_search

        caveat = Finding(
            check="fts-stale", severity="warning", message="FTS stale", fix_available=False
        )
        output = render_search(
            [_conv_result()], Fidelity(depth=1), query="q", mode="conversations", caveats=[caveat]
        )
        assert "> **Note:** FTS stale" in output


class TestTerminalRenderSearch:
    def test_chunks_mode(self):
        from siftd.output.terminal_fmt import render_search

        output = render_search(
            [_chunk_result()], Fidelity(depth=1), query="test query", mode="chunks"
        )
        assert "Results for: test query" in output
        assert "01ABC123" in output
        assert "0.850" in output

    def test_conversations_mode(self):
        from siftd.output.terminal_fmt import render_search

        output = render_search(
            [_conv_result()], Fidelity(depth=1), query="q", mode="conversations"
        )
        assert "Conversations for: q" in output
        assert "0.920" in output

    def test_thread_mode_with_exchanges(self):
        from siftd.output.terminal_fmt import render_search

        tier1 = [_chunk_result(_exchanges=[("p1", "What?", "That.")])]
        output = render_search(
            [], Fidelity(depth=1), query="q", mode="thread", tier1=tier1
        )
        assert "[user] What?" in output
        assert "[asst] That." in output

    def test_thread_mode_text_fallback_and_file_refs(self):
        from siftd.output.terminal_fmt import render_search

        @dataclass
        class Ref:
            basename: str
            path: str
            op: str

        tier1 = [_chunk_result(text="fallback", file_refs=[Ref("a.py", "/a.py", "r")])]
        tier2 = [_chunk_result(text="compact", score=0.4, file_refs=[Ref("b.py", "/b.py", "w")])]
        output = render_search(
            [], Fidelity(depth=1), query="q", mode="thread", tier1=tier1, tier2=tier2
        )
        assert "fallback" in output
        assert "refs:" in output
        assert "More results" in output or "──" in output

    def test_chunks_mode_with_exchanges(self):
        from siftd.output.terminal_fmt import render_search

        chunk = _chunk_result(_exchanges=[("p1", "Multi\nline", "Reply\nhere")])
        output = render_search([chunk], Fidelity(depth=1), query="q")
        assert "> Multi" in output
        assert "Reply" in output

    def test_chunks_mode_with_context(self):
        from siftd.output.terminal_fmt import render_search

        chunk = _chunk_result(
            _context=[("p1", "Multi\nline Q", "Response\ntext", True), ("p2", "Q2", None, False)]
        )
        output = render_search([chunk], Fidelity(depth=1), query="q")
        assert ">>>" in output
        assert "Multi" in output
        assert "Response" in output

    def test_chunks_mode_with_file_refs(self):
        from siftd.output.terminal_fmt import render_search

        @dataclass
        class Ref:
            basename: str
            path: str
            op: str

        chunk = _chunk_result(file_refs=[Ref("f.py", "/f.py", "r")])
        output = render_search([chunk], Fidelity(depth=1), query="q")
        assert "refs:" in output
        assert "f.py(r)" in output

    def test_caveats_note_appended(self):
        from siftd.doctor.checks import Finding
        from siftd.output.terminal_fmt import render_search

        caveat = Finding(
            check="embeddings-stale",
            severity="warning",
            message="Embeddings index is stale — run siftd ingest",
            fix_available=True,
        )
        output = render_search(
            [_chunk_result()], Fidelity(depth=1), query="q", caveats=[caveat]
        )
        assert "note: Embeddings index is stale — run siftd ingest" in output

    def test_no_caveats_no_note(self):
        from siftd.output.terminal_fmt import render_search

        output = render_search([_chunk_result()], Fidelity(depth=1), query="q")
        assert "note:" not in output

    def test_caveats_note_in_conversations_mode(self):
        from siftd.doctor.checks import Finding
        from siftd.output.terminal_fmt import render_search

        caveat = Finding(
            check="fts-stale", severity="warning", message="FTS stale", fix_available=False
        )
        output = render_search(
            [_conv_result()], Fidelity(depth=1), query="q", mode="conversations", caveats=[caveat]
        )
        assert "note: FTS stale" in output


class TestHtmlRenderSearch:
    def test_chunks_mode_smoke(self):
        from siftd.output.html_fmt import render_search

        output = render_search(
            [_chunk_result()], Fidelity(depth=1), query="test query", mode="chunks"
        )
        assert 'class="search-results chunks"' in output
        assert "01ABC123" in output
        assert "0.850" in output

    def test_conversations_mode_smoke(self):
        from siftd.output.html_fmt import render_search

        output = render_search(
            [_conv_result()], Fidelity(depth=1), query="q", mode="conversations"
        )
        assert 'class="search-results conversations"' in output
        assert "0.920" in output

    def test_thread_mode_smoke(self):
        from siftd.output.html_fmt import render_search

        tier1 = [_chunk_result(_exchanges=[("p1", "What?", "That.")])]
        output = render_search(
            [], Fidelity(depth=1), query="q", mode="thread", tier1=tier1
        )
        assert 'class="search-results thread"' in output
        assert "What?" in output

    def test_caveats_aside_appended(self):
        from siftd.doctor.checks import Finding
        from siftd.output.html_fmt import render_search

        caveat = Finding(
            check="search-mode-degraded",
            severity="warning",
            message="Semantic search unavailable",
            fix_available=False,
        )
        output = render_search(
            [_chunk_result()], Fidelity(depth=1), query="q", caveats=[caveat]
        )
        assert '<aside class="caveats">' in output
        assert '<p class="caveat">Semantic search unavailable</p>' in output

    def test_no_caveats_no_aside(self):
        from siftd.output.html_fmt import render_search

        output = render_search([_chunk_result()], Fidelity(depth=1), query="q")
        assert '<aside class="caveats">' not in output

    def test_caveats_aside_in_conversations_mode(self):
        from siftd.doctor.checks import Finding
        from siftd.output.html_fmt import render_search

        caveat = Finding(
            check="fts-stale", severity="warning", message="FTS stale", fix_available=False
        )
        output = render_search(
            [_conv_result()], Fidelity(depth=1), query="q", mode="conversations", caveats=[caveat]
        )
        assert '<aside class="caveats">' in output
        assert "FTS stale" in output


# ---------------------------------------------------------------------------
# render_detail tests
# ---------------------------------------------------------------------------


@dataclass
class FakeTurn:
    """Minimal turn for render_detail testing."""

    timestamp: str | None = "2026-03-15T10:00:00Z"
    prompt_text: str | None = "Hello"
    narrative: list = field(default_factory=list)
    total_input_tokens: int = 100
    total_output_tokens: int = 200
    tool_call_summaries: list = field(default_factory=list)


@dataclass
class FakeDetail:
    """Minimal ConversationDetail for render_detail testing."""

    id: str = "01DETAIL123456"
    workspace_path: str | None = "/home/user/project"
    started_at: str | None = "2026-03-15T10:00:00Z"
    model: str | None = "claude-opus-4-5"
    total_input_tokens: int = 500
    total_output_tokens: int = 1000
    tags: list = field(default_factory=list)


class TestJsonRenderDetail:
    def test_fallback_without_detail(self):
        """When no detail context is provided, render_detail uses turn-level data."""
        from siftd.output.json_fmt import render_detail

        turn = FakeTurn(prompt_text="What is 2+2?")
        result = render_detail([turn], Fidelity(depth=1))
        assert "turns" in result
        assert result["turns"][0]["prompt"] == "What is 2+2?"
        assert result["turns"][0]["tokens"]["input"] == 100


class TestMarkdownRenderDetail:
    def test_renders_header_and_turns(self):
        from siftd.output.markdown_fmt import render_detail

        detail = FakeDetail(tags=["review"])
        turn = FakeTurn()
        output = render_detail([turn], Fidelity(depth=1), detail=detail)
        assert "# Session 01DETAIL1234" in output
        assert "project" in output
        assert "### " in output  # turn headers
        assert "Hello" in output

    def test_truncates_long_prompts(self):
        from siftd.output.markdown_fmt import render_detail

        detail = FakeDetail()
        turn = FakeTurn(prompt_text="x" * 200)
        output = render_detail([turn], Fidelity(depth=1, chars=50), detail=detail)
        assert "..." in output


# ---------------------------------------------------------------------------
# MarkdownEmitter tests
# ---------------------------------------------------------------------------


class TestMarkdownEmitter:
    def test_tool_content(self):
        from siftd.output.narrative import MarkdownEmitter

        e = MarkdownEmitter()
        e.tool_content("shell.execute", 1, "ls -la", "file1.py\nfile2.py", None)
        text = "\n".join(e.lines)
        assert "**shell.execute**" in text
        assert "`ls -la`" in text
        assert "file1.py" in text

    def test_tool_content_with_count_and_status(self):
        from siftd.output.narrative import MarkdownEmitter

        e = MarkdownEmitter()
        e.tool_content("file.read", 3, None, None, "error")
        text = "\n".join(e.lines)
        assert "×3" in text
        assert "(error)" in text

    def test_tool_content_truncates_long_input(self):
        from siftd.output.narrative import MarkdownEmitter

        e = MarkdownEmitter()
        e.tool_content("test", 1, "x" * 200, None, None)
        text = "\n".join(e.lines)
        assert "..." in text  # truncated at 100 chars

    def test_tool_content_truncates_long_result(self):
        from siftd.output.narrative import MarkdownEmitter

        e = MarkdownEmitter()
        e.tool_content("test", 1, None, "y" * 300, None)
        text = "\n".join(e.lines)
        assert "..." in text  # result truncated at 200 chars

    def test_tool_output(self):
        from siftd.output.narrative import MarkdownEmitter

        e = MarkdownEmitter()
        e.tool_output("tool_result", "output here")
        text = "\n".join(e.lines)
        assert "```" in text
        assert "output here" in text

    def test_thinking(self):
        from siftd.output.narrative import MarkdownEmitter

        e = MarkdownEmitter()
        e.thinking("deep thoughts\nmore thoughts")
        text = "\n".join(e.lines)
        assert "> **Thinking**" in text
        assert "> deep thoughts" in text
        assert "> more thoughts" in text

    def test_thinking_placeholder(self):
        from siftd.output.narrative import MarkdownEmitter

        e = MarkdownEmitter()
        e.thinking_placeholder()
        assert "*[thinking]*" in e.lines


# ---------------------------------------------------------------------------
# painted_bridge tests
# ---------------------------------------------------------------------------


def _block_to_text(block):
    """Extract plain text from a painted Block."""
    lines = []
    for y in range(block.height):
        lines.append("".join(cell.char for cell in block.row(y)).rstrip())
    return "\n".join(lines)


class TestEmitOutput:
    def test_string_output(self, capsys):
        from siftd.output.painted_bridge import emit_output

        emit_output("hello world")
        assert capsys.readouterr().out.strip() == "hello world"

    def test_dict_output(self, capsys):
        from siftd.output.painted_bridge import emit_output

        emit_output({"key": "value"})
        out = capsys.readouterr().out
        assert '"key": "value"' in out

    def test_block_output(self, capsys):
        from painted import Line, Span, Style

        from siftd.output.painted_bridge import emit_output

        line = Line(spans=(Span("test", Style()),))
        block = line.to_block(4)
        emit_output(block)
        assert "test" in capsys.readouterr().out

    def test_falsy_noop(self, capsys):
        from siftd.output.painted_bridge import emit_output

        emit_output(None)
        emit_output("")
        assert capsys.readouterr().out == ""


class TestFormatGenericInput:
    def test_priority_keys(self):
        from siftd.output.tool_presenters import _format_generic_input

        result = _format_generic_input('{"command": "ls", "path": "/tmp"}')
        assert "command: ls" in result
        assert "path: /tmp" in result

    def test_no_priority_keys_falls_back_to_json(self):
        from siftd.output.tool_presenters import _format_generic_input

        result = _format_generic_input('{"foo": "bar"}')
        assert "foo" in result

    def test_non_json_returns_raw(self):
        from siftd.output.tool_presenters import _format_generic_input

        assert _format_generic_input("plain text") == "plain text"


class TestFormatGenericResult:
    def test_output_with_meta(self):
        from siftd.output.tool_presenters import _format_generic_result

        result = _format_generic_result('{"output": "hello", "exit_code": 0}')
        assert "exit_code: 0" in result
        assert "hello" in result

    def test_text_key_fallback(self):
        from siftd.output.tool_presenters import _format_generic_result

        result = _format_generic_result('{"text": "some text"}')
        assert result == "some text"

    def test_compact_keys(self):
        from siftd.output.tool_presenters import _format_generic_result

        result = _format_generic_result('{"error": "fail", "status": "error"}')
        assert "error: fail" in result
        assert "status: error" in result

    def test_empty_dict_json_fallback(self):
        from siftd.output.tool_presenters import _format_generic_result

        result = _format_generic_result('{"x": 1}')
        assert "1" in result

    def test_non_json_returns_raw(self):
        from siftd.output.tool_presenters import _format_generic_result

        assert _format_generic_result("raw text") == "raw text"


@dataclass
class FakeNarrativeBlock:
    block_type: str
    content: str | None = None
    tool_calls: list = field(default_factory=list)


@dataclass
class FakeToolCall:
    tool_name: str
    count: int = 1
    status: str | None = None
    input: str | None = None
    result: str | None = None


class TestRenderNarrativeBlock:
    def test_text_block(self):
        from siftd.output.painted_bridge import render_narrative_block

        blocks = [FakeNarrativeBlock("text", "Hello world")]
        result = render_narrative_block(blocks, fidelity=Fidelity(depth=1))
        text = _block_to_text(result)
        assert "Hello world" in text

    def test_thinking_block(self):
        from siftd.output.painted_bridge import render_narrative_block

        blocks = [FakeNarrativeBlock("thinking", "deep thought")]
        result = render_narrative_block(blocks, fidelity=Fidelity(depth=3, visible=frozenset({"thinking"})))
        text = _block_to_text(result)
        assert "thinking" in text.lower()

    def test_tool_calls_compact(self):
        from siftd.output.painted_bridge import render_narrative_block

        tc = FakeToolCall("shell.execute", count=2, status="error")
        blocks = [FakeNarrativeBlock("tool_calls", tool_calls=[tc])]
        result = render_narrative_block(blocks, fidelity=Fidelity(depth=0))
        text = _block_to_text(result)
        assert "shell.execute" in text
        assert "×2" in text

    def test_tool_calls_expanded(self):
        from siftd.output.painted_bridge import render_narrative_block

        tc = FakeToolCall(
            "shell.execute",
            input='{"command": "ls"}',
            result='{"output": "file.py", "exit_code": 0}',
        )
        blocks = [FakeNarrativeBlock("tool_calls", tool_calls=[tc])]
        result = render_narrative_block(
            blocks, fidelity=Fidelity(depth=3, visible=frozenset({"tools"}))
        )
        text = _block_to_text(result)
        assert "ls" in text

    def test_tool_result_block(self):
        from siftd.output.painted_bridge import render_narrative_block

        blocks = [FakeNarrativeBlock("tool_result", "result content")]
        result = render_narrative_block(
            blocks, fidelity=Fidelity(depth=3, visible=frozenset({"tools"}))
        )
        text = _block_to_text(result)
        assert "result content" in text

    def test_empty_blocks(self):
        from siftd.output.painted_bridge import render_narrative_block

        result = render_narrative_block([], fidelity=Fidelity(depth=1))
        assert result.height == 0


@dataclass
class FakeToolSummary:
    tool_name: str
    count: int = 1
    status: str | None = None


class TestToolPresenters:
    """Test individual tool presenters via render_narrative_block."""

    def _render_tool(self, tool_name, input_json=None, result_json=None, status=None):
        from siftd.output.painted_bridge import render_narrative_block

        tc = FakeToolCall(tool_name, input=input_json, result=result_json, status=status)
        blocks = [FakeNarrativeBlock("tool_calls", tool_calls=[tc])]
        result = render_narrative_block(
            blocks, fidelity=Fidelity(depth=3, visible=frozenset({"tools"}))
        )
        return _block_to_text(result)

    def test_shell_execute_raw_result_fallback(self):
        text = self._render_tool("shell.execute", result_json="raw output text")
        assert "raw output" in text

    def test_shell_execute_overflow_preview(self):
        import json as json_mod

        # 10 lines triggers overflow (max 6 preview lines when tool_chars > 0)
        output = "\n".join(f"line{i}" for i in range(10))
        tc = FakeToolCall(
            "shell.execute",
            result=json_mod.dumps({"output": output, "exit_code": 0}),
        )
        from siftd.output.painted_bridge import render_narrative_block

        blocks = [FakeNarrativeBlock("tool_calls", tool_calls=[tc])]
        result = render_narrative_block(
            blocks,
            fidelity=Fidelity(depth=1, visible=frozenset({"tools"})),
            tool_chars=120,
        )
        text = _block_to_text(result)
        assert "more lines" in text

    def test_file_read_raw_input_string(self):
        # Non-JSON input falls back to raw string display
        text = self._render_tool("file.read", input_json="/path/to/file.py")
        assert "file.py" in text

    def test_file_edit_raw_input_string(self):
        text = self._render_tool("file.edit", input_json="not-json-path")
        assert "not-json-path" in text

    def test_file_edit_error(self):
        text = self._render_tool(
            "file.edit",
            input_json='{"path": "f.py"}',
            result_json='{"error": "conflict"}',
            status="error",
        )
        assert "conflict" in text

    def test_file_write_raw_input_string(self):
        text = self._render_tool("file.write", input_json="not-json")
        assert "not-json" in text

    def test_file_write_error(self):
        text = self._render_tool(
            "file.write",
            input_json='{"path": "f.py"}',
            result_json='{"error": "permission denied"}',
            status="error",
        )
        assert "permission denied" in text

    def test_search_grep_raw_input_string(self):
        text = self._render_tool("search.grep", input_json="not-json")
        assert "not-json" in text

    def test_search_grep_raw_result_string(self):
        text = self._render_tool("search.grep", result_json="raw grep output")
        assert "raw grep output" in text

    def test_file_glob_raw_input_string(self):
        text = self._render_tool("file.glob", input_json="not-json")
        assert "not-json" in text

    def test_file_glob_result_dict(self):
        text = self._render_tool("file.glob", result_json='{"output": "a.py\\nb.py"}')
        assert "a.py" in text

    def test_file_glob_raw_result_string(self):
        text = self._render_tool("file.glob", result_json="raw glob")
        assert "raw glob" in text

    def test_todo_tasks(self):
        tasks = [
            {"description": "Fix bug", "status": "done"},
            {"description": "Write tests", "status": "pending"},
            "Plain string task",
        ]
        import json as json_mod

        text = self._render_tool(
            "ui.todo", input_json=json_mod.dumps({"title": "Plan", "tasks": tasks})
        )
        assert "Plan" in text
        assert "Fix bug" in text
        assert "✓" in text
        assert "○" in text

    def test_todo_raw_input(self):
        text = self._render_tool("ui.todo", input_json="not-json")
        assert "not-json" in text


class TestRenderQueryDetailBlock:
    def test_renders_header_and_turns(self):
        from siftd.output.painted_bridge import render_query_detail_block

        detail = FakeDetail(tags=["review"])
        turn = FakeTurn()
        result = render_query_detail_block(
            detail, turns=[turn], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "Conversation:" in text
        assert "01DETAIL" in text
        assert "project" in text
        assert "[prompt]" in text
        assert "[response]" in text

    def test_turn_with_tool_summaries(self):
        from siftd.output.painted_bridge import render_query_detail_block

        detail = FakeDetail()
        turn = FakeTurn(
            narrative=[],
            tool_call_summaries=[
                FakeToolSummary("file.read", count=3, status="error"),
            ],
        )
        result = render_query_detail_block(
            detail, turns=[turn], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "file.read" in text
        assert "×3" in text

    def test_turn_with_narrative(self):
        from siftd.output.painted_bridge import render_query_detail_block

        detail = FakeDetail()
        turn = FakeTurn(narrative=[FakeNarrativeBlock("text", "AI response text")])
        result = render_query_detail_block(
            detail, turns=[turn], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "AI response text" in text

    def test_prompt_only_turn_no_response(self):
        from siftd.output.painted_bridge import render_query_detail_block

        detail = FakeDetail()
        turn = FakeTurn(
            total_input_tokens=0,
            total_output_tokens=0,
            tool_call_summaries=[],
        )
        result = render_query_detail_block(
            detail, turns=[turn], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "[prompt]" in text

    def test_empty_turns(self):
        from siftd.output.painted_bridge import render_query_detail_block

        detail = FakeDetail()
        result = render_query_detail_block(
            detail, turns=[], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "Conversation:" in text


@dataclass
class FakePeekExchange:
    timestamp: str | None = "2026-03-15T10:00:00Z"
    prompt_text: str | None = "Hello"
    narrative: list = field(default_factory=list)
    response_text: str | None = None
    tool_calls: list = field(default_factory=list)
    input_tokens: int = 50
    output_tokens: int = 100


@dataclass
class FakePeekInfo:
    session_id: str = "sess001"
    workspace_name: str | None = "my-project"
    workspace_path: str | None = None
    branch: str | None = "main"
    model: str | None = "claude-opus-4-5"
    adapter_name: str | None = "claude_code"
    exchange_count: int = 5
    last_activity: float | None = None
    file_path: str = "/home/.claude/session.jsonl"
    parent_session_id: str | None = None
    preview_available: bool = True


@dataclass
class FakePeekDetail:
    info: FakePeekInfo = field(default_factory=FakePeekInfo)
    started_at: str | None = "2026-03-15T10:00:00Z"


class TestRenderPeekDetailBlock:
    def test_renders_header_and_exchanges(self):
        from siftd.output.painted_bridge import render_peek_detail_block

        detail = FakePeekDetail()
        exchange = FakePeekExchange()
        result = render_peek_detail_block(
            detail, exchanges=[exchange], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "Session:" in text
        assert "sess001" in text
        assert "my-project" in text
        assert "[prompt]" in text

    def test_exchange_with_response_text(self):
        from siftd.output.painted_bridge import render_peek_detail_block

        detail = FakePeekDetail()
        exchange = FakePeekExchange(narrative=[], response_text="I can help with that.")
        result = render_peek_detail_block(
            detail, exchanges=[exchange], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "I can help" in text

    def test_exchange_with_tool_calls(self):
        from siftd.output.painted_bridge import render_peek_detail_block

        detail = FakePeekDetail()
        exchange = FakePeekExchange(
            narrative=[], tool_calls=[("file.read", 1)]
        )
        result = render_peek_detail_block(
            detail, exchanges=[exchange], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "file.read" in text

    def test_empty_exchanges(self):
        from siftd.output.painted_bridge import render_peek_detail_block

        detail = FakePeekDetail()
        result = render_peek_detail_block(
            detail, exchanges=[], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "Session:" in text

    def test_prompt_only_exchange(self):
        from siftd.output.painted_bridge import render_peek_detail_block

        detail = FakePeekDetail()
        exchange = FakePeekExchange(input_tokens=0, output_tokens=0, tool_calls=[])
        result = render_peek_detail_block(
            detail, exchanges=[exchange], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "[prompt]" in text

    def test_parent_session_shown(self):
        from siftd.output.painted_bridge import render_peek_detail_block

        info = FakePeekInfo(parent_session_id="parent001")
        detail = FakePeekDetail(info=info)
        result = render_peek_detail_block(
            detail, exchanges=[], fidelity=Fidelity(depth=1)
        )
        text = _block_to_text(result)
        assert "Parent:" in text
        assert "parent001" in text


@dataclass
class FakeFollowEvent:
    timestamp: str | None = "2026-03-15T10:00:00Z"
    is_user: bool = False
    text: str | None = None
    narrative: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list = field(default_factory=list)


class TestRenderFollowEventBlock:
    def test_user_event(self):
        from siftd.output.painted_bridge import render_follow_event_block

        event = FakeFollowEvent(is_user=True, text="How do I fix this?")
        result = render_follow_event_block(event, fidelity=Fidelity(depth=1))
        text = _block_to_text(result)
        assert "[prompt]" in text
        assert "How do I fix" in text

    def test_assistant_event_with_text(self):
        from siftd.output.painted_bridge import render_follow_event_block

        event = FakeFollowEvent(text="Here's the fix.", input_tokens=100, output_tokens=200)
        result = render_follow_event_block(event, fidelity=Fidelity(depth=1))
        text = _block_to_text(result)
        assert "[response]" in text
        assert "Here's the fix" in text
        assert "tok" in text

    def test_assistant_event_with_tool_calls(self):
        from siftd.output.painted_bridge import render_follow_event_block

        event = FakeFollowEvent(tool_calls=[("shell.execute", 1)])
        result = render_follow_event_block(event, fidelity=Fidelity(depth=1))
        text = _block_to_text(result)
        assert "shell.execute" in text

    def test_assistant_event_no_content(self):
        """Event with no text, narrative, or tools — just the response header."""
        from siftd.output.painted_bridge import render_follow_event_block

        event = FakeFollowEvent(input_tokens=10, output_tokens=20)
        result = render_follow_event_block(event, fidelity=Fidelity(depth=1))
        text = _block_to_text(result)
        assert "[response]" in text

    def test_assistant_event_with_narrative(self):
        from siftd.output.painted_bridge import render_follow_event_block

        event = FakeFollowEvent(
            narrative=[FakeNarrativeBlock("text", "narrative content")],
            input_tokens=50,
            output_tokens=100,
        )
        result = render_follow_event_block(event, fidelity=Fidelity(depth=1))
        text = _block_to_text(result)
        assert "narrative content" in text


class TestRenderPeekListBlock:
    def test_renders_session_list(self):
        from siftd.output.painted_bridge import render_peek_list_block

        info = FakePeekInfo(last_activity=0.0)
        result = render_peek_list_block([info], children_by_parent={})
        assert result is not None
        text = _block_to_text(result)
        assert "sess001" in text  # session id appears in table

    def test_empty_list_returns_none(self):
        from siftd.output.painted_bridge import render_peek_list_block

        assert render_peek_list_block([], children_by_parent={}) is None

    def test_preview_unavailable(self):
        from siftd.output.painted_bridge import render_peek_list_block

        info = FakePeekInfo(last_activity=0.0, preview_available=False)
        result = render_peek_list_block([info], children_by_parent={})
        text = _block_to_text(result)
        assert "preview unavailable" in text

    def test_children_shown(self):
        from siftd.output.painted_bridge import render_peek_list_block

        info = FakePeekInfo(last_activity=0.0)
        children = {"sess001": [FakePeekInfo(session_id="child1")]}
        result = render_peek_list_block([info], children_by_parent=children)
        text = _block_to_text(result)
        assert "+1 agents" in text


# ---------------------------------------------------------------------------
# terminal render_detail test
# ---------------------------------------------------------------------------


class TestTerminalRenderDetail:
    def test_renders_via_painted_bridge(self):
        from siftd.output.terminal_fmt import render_detail

        detail = FakeDetail()
        turn = FakeTurn()
        result = render_detail([turn], Fidelity(depth=1), detail=detail)
        text = _block_to_text(result)
        assert "01DETAIL" in text
        assert "[prompt]" in text


class TestSelectFormat:
    def test_explicit_name(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(name="json")
        assert fmt.name == "json"

    def test_unknown_name_raises(self):
        from siftd.output.format_registry import select_format

        with pytest.raises(ValueError, match="Unknown format"):
            select_format(name="nonexistent")

    def test_json_mode(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(json_mode=True)
        assert fmt.name == "json"

    def test_non_tty_selects_markdown(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(is_tty=False)
        assert fmt.name == "markdown"

    def test_tty_selects_terminal(self):
        from siftd.output.format_registry import select_format

        fmt = select_format(is_tty=True)
        assert fmt.name == "terminal"


class TestFidelityFromArgsBrief:
    def test_brief_sets_depth_zero(self):
        from siftd.cli._common import fidelity_from_args

        args = type("Args", (), {"brief": True, "full": False, "thinking": False, "tools": None, "chars": None})()
        fidelity = fidelity_from_args(args)
        assert fidelity.depth == 0
        assert fidelity.chars == 80

    def test_default_depth_is_one(self):
        from siftd.cli._common import fidelity_from_args

        args = type("Args", (), {"brief": False, "full": False, "thinking": False, "tools": None, "chars": None})()
        fidelity = fidelity_from_args(args)
        assert fidelity.depth == 1

    def test_full_depth_is_three(self):
        from siftd.cli._common import fidelity_from_args

        args = type("Args", (), {"brief": False, "full": True, "thinking": False, "tools": None, "chars": None})()
        fidelity = fidelity_from_args(args)
        assert fidelity.depth == 3
