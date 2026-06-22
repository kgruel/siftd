"""Tests for the painted search renderer (painted_bridge.render_search_block).

The headline fix: FTS5 snippet() wraps matched terms in >>>...<<< delimiters,
which used to render as a literal '>>>error<<<' thicket. They now become accent
spans. These tests pin that transform and that every mode returns a Block.
"""

import pytest

pytest.importorskip("painted")

from painted import Fidelity, Style

from siftd.output._id_format import short_id
from siftd.output.painted_bridge import _expand_count, _match_spans, render_search_block


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

    def _gradient_results(self, n):
        return [
            {
                "conversation_id": f"01CONV{i}000000000000000000",
                "display_label": "USER",
                "score": 5.0 - i * 0.1,
                "_workspace": "w",
                "_started_at": "2026-01-01",
                "text": f"unique snippet number {i}",
                "turn_index": i,
            }
            for i in range(n)
        ]

    def _collapsed(self, lines, results, i):
        """A hit is collapsed iff its 'number i' snippet line also carries its id."""
        rows = [ln for ln in lines if f"number {i}" in ln]
        assert len(rows) == 1
        return short_id(results[i]["conversation_id"]) in rows[0]

    def test_disclosure_gradient_top_expands_tail_collapses(self):
        """A large result set keeps an expanded head (snippet + hint on separate
        lines) while the tail collapses to one line carrying both id and snippet.
        The head size is pinned to _expand_count(n) so an off-by-one is caught."""
        n = 8  # > _EXPAND_ALL_MAX, so the tail collapses
        results = self._gradient_results(n)
        lines = _block_text(
            render_search_block(results, Fidelity(), query="snippet", mode="chunks")
        ).splitlines()

        # Exactly _expand_count(n) hits expand; the rest collapse — and the boundary
        # is precise: the last head hit expands, the first tail hit collapses.
        head = _expand_count(n)
        expanded = [i for i in range(n) if not self._collapsed(lines, results, i)]
        assert expanded == list(range(head))
        assert not self._collapsed(lines, results, head - 1)  # last head expands
        assert self._collapsed(lines, results, head)          # first tail collapses

        # Top hit's snippet and its `siftd show` hint are on separate lines.
        top = [ln for ln in lines if "number 0" in ln]
        assert len(top) == 1 and "siftd show" not in top[0]
        assert any("siftd show" in ln and short_id(results[0]["conversation_id"]) in ln for ln in lines)

    def test_small_result_set_expands_every_hit(self):
        """At or below _EXPAND_ALL_MAX results, none collapse — there's screen room
        and nothing is gained by folding four or five hits to one line."""
        results = self._gradient_results(5)  # <= _EXPAND_ALL_MAX
        lines = _block_text(
            render_search_block(results, Fidelity(), query="snippet", mode="chunks")
        ).splitlines()

        # Every hit (including the last) expands: its snippet line carries no id,
        # and a separate `siftd show` hint line exists for it.
        for i in range(5):
            snippet_lines = [ln for ln in lines if f"number {i}" in ln]
            assert len(snippet_lines) == 1
            assert short_id(results[i]["conversation_id"]) not in snippet_lines[0]
            assert any(
                "siftd show" in ln and short_id(results[i]["conversation_id"]) in ln
                for ln in lines
            )

    def test_top_hit_snippet_is_not_truncated(self):
        """A top-tier hit shows its full snippet (word-wrapped), nothing dropped."""
        text = " ".join(f"word{i}" for i in range(60))  # long, multi-word, no newlines
        r = {
            "conversation_id": "01TOP00000000000000000000",
            "display_label": "USER",
            "score": 9.0,
            "_workspace": "w",
            "_started_at": "2026-01-01",
            "text": text,
            "turn_index": 0,
        }
        out = _block_text(render_search_block([r], Fidelity(), query="word", mode="chunks"))
        for i in range(60):
            assert f"word{i}" in out  # every token survives the wrap, none truncated

    def test_ascii_degradation_emits_no_unicode_glyphs(self):
        """On a non-Unicode stream (prefers_ascii — the case pytest's captured,
        non-TTY stdout reproduces, and a LANG=C TTY hits in production) the rank
        rail and the truncation ellipsis degrade to ASCII so the render can't
        raise UnicodeEncodeError. This is the search path's old crash gap."""
        # Many results → a collapsed · tail; long excerpt → oneline truncation (…).
        results = [
            {
                "conversation_id": f"01CONV{i}000000000000000000",
                "_workspace": "w",
                "_started_at": "2026-01-01",
                "max_score": 5.0 - i * 0.1,
                "chunk_count": 2,
                "best_excerpt": "averylongunbrokensnippet " * 20,
            }
            for i in range(8)
        ]
        text = _block_text(
            render_search_block(results, Fidelity(), query="snippet", mode="conversations")
        )
        for glyph in ("◆", "│", "·", "…"):
            assert glyph not in text
        text.encode("ascii")  # raises if any Unicode glyph leaked through

    def test_ascii_degradation_thread_and_context_glyphs(self):
        """The thread separator (──) and the context-match caret (▸) also degrade —
        the rail-only conversations test doesn't reach them."""
        # Thread: tier1 heading carries the ── separator; tier2 carries the rail.
        tier1 = [{"_workspace": "w", "_started_at": "2026-01-01", "display_label": "USER", "text": "x"}]
        tier2 = [{"conversation_id": "01T", "_workspace": "w", "_started_at": "2026-01-02", "score": 3.0, "text": "y"}]
        thread = _block_text(
            render_search_block([], Fidelity(), query="x", mode="thread", tier1=tier1, tier2=tier2)
        )
        assert "─" not in thread and "·" not in thread
        thread.encode("ascii")

        # Chunks with a _context window: the matched turn carries the ▸ caret.
        ctx = [{
            "conversation_id": "01C", "display_label": "USER", "score": 0.9,
            "_workspace": "w", "_started_at": "2026-01-01",
            "_context": [("p0", "before", "b", False), ("p1", "match", "m", True)],
        }]
        chunks = _block_text(render_search_block(ctx, Fidelity(), query="match", mode="chunks"))
        assert "▸" not in chunks
        chunks.encode("ascii")
