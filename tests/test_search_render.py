"""Tests for the painted search renderer (painted_bridge.render_search_block).

The headline fix: FTS5 snippet() wraps matched terms in >>>...<<< delimiters,
which used to render as a literal '>>>error<<<' thicket. They now become accent
spans. These tests pin that transform and that every mode returns a Block.
"""

import pytest

pytest.importorskip("painted")

from painted import Fidelity, Style

from siftd.output._id_format import short_id
from siftd.output.painted_bridge import _match_spans, render_search_block


def _block_text(block):
    import io

    from painted import print_block

    buf = io.StringIO()
    print_block(block, buf, use_ansi=False)
    return buf.getvalue()


class TestMatchSpans:
    def test_strips_markers_and_highlights_matches(self):
        spans = _match_spans("API >>>error<<<: 500 >>>error<<< again")
        text = "".join(s.text for s in spans)
        assert ">>>" not in text and "<<<" not in text
        assert text == "API error: 500 error again"
        # matched terms carry a non-default (accent) style ...
        matched = [s for s in spans if s.text == "error"]
        assert matched and all(s.style != Style() for s in matched)
        # ... and the surrounding text stays unstyled
        assert all(s.style == Style() for s in spans if s.text != "error")

    def test_plain_text_is_one_unstyled_span(self):
        spans = _match_spans("just text")
        assert len(spans) == 1
        assert spans[0].text == "just text"
        assert spans[0].style == Style()


class TestRenderSearchBlock:
    def _chunk(self, **over):
        r = {
            "conversation_id": "01KJEMF5G4PYVK1F6YRC9G728A",
            "display_label": "USER",
            "score": 5.62,
            "_workspace": "ndebug",
            "_started_at": "2026-02-26",
            "text": "API >>>error<<<: 500",
            "turn_index": 12,
        }
        r.update(over)
        return r

    def test_chunks_returns_block(self):
        block = render_search_block([self._chunk()], Fidelity(), query="error", mode="chunks")
        assert block.height >= 1 and block.width >= 1

    def test_full_mode_returns_block(self):
        # depth>=2 with no char limit = --full (natural sizing, width=None path)
        fid = Fidelity(depth=2)
        block = render_search_block([self._chunk()], fid, query="error", mode="chunks")
        assert block.height >= 1

    def test_conversations_returns_block(self):
        r = {
            "conversation_id": "c1",
            "_workspace": "w",
            "_started_at": "2026-01-01",
            "max_score": 5.0,
            "mean_score": 4.0,
            "chunk_count": 2,
            "best_excerpt": "an >>>error<<< here",
        }
        block = render_search_block([r], Fidelity(), query="error", mode="conversations")
        assert block.height >= 1

    def test_thread_returns_block(self):
        tier1 = [{"_workspace": "w", "_started_at": "2026-01-01", "display_label": "USER", "text": ">>>x<<<"}]
        tier2 = [{"conversation_id": "c2", "_workspace": "w", "_started_at": "2026-01-02", "score": 3.0, "text": "y"}]
        block = render_search_block([], Fidelity(), query="x", mode="thread", tier1=tier1, tier2=tier2)
        assert block.height >= 1

    def test_empty_results_returns_block(self):
        block = render_search_block([], Fidelity(), query="nothing", mode="chunks")
        assert block.height >= 1  # at least the title line

    def test_disclosure_gradient_top_expands_tail_collapses(self):
        """Top hits expand (snippet + hint on separate lines); the tail collapses
        to one line carrying both its id and snippet."""
        results = [
            {
                "conversation_id": f"01CONV{i}000000000000000000",
                "display_label": "USER",
                "score": 5.0 - i * 0.1,
                "_workspace": "w",
                "_started_at": "2026-01-01",
                "text": f"unique snippet number {i}",
                "turn_index": i,
            }
            for i in range(5)
        ]
        lines = _block_text(
            render_search_block(results, Fidelity(), query="snippet", mode="chunks")
        ).splitlines()

        # Tail hit (rank 4 >= _EXPAND_TOP) is a single combined line: id + snippet.
        tail = [ln for ln in lines if "number 4" in ln]
        assert len(tail) == 1
        assert short_id(results[4]["conversation_id"]) in tail[0]

        # Top hit (rank 0) expands: its snippet and the `siftd show` hint are on
        # separate lines (the hint is not folded into the snippet line).
        top = [ln for ln in lines if "number 0" in ln]
        assert len(top) == 1
        assert "siftd show" not in top[0]
        assert any("siftd show" in ln and short_id(results[0]["conversation_id"]) in ln for ln in lines)
