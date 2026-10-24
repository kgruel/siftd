"""Chunk IDs are always on in JSON.

H5b once hid them behind --debug-ids; Phase 2 made events addressable by
default, and the flag's trail was removed in 0.13.0 (#94).

Covers:
- SearchChunk.to_render_dict
- json_fmt._json_chunk_list
- json_fmt.render_search
- serve_fmt.render_search
- conversation_id preserved in conversation serializers (regression guard)
"""

from __future__ import annotations

from types import SimpleNamespace

from siftd.domain.search_types import SearchChunk, SearchView
from siftd.output import json_fmt
from siftd.serialization.serve_fmt import render_search as serve_render_search


def _chunk(**kwargs):
    defaults = dict(
        conversation_id="conv-abc",
        score=0.85,
        text="hello world",
        chunk_type="exchange",
        chunk_id="chunk-xyz",
        source_ids=["src-001", "src-002"],
    )
    defaults.update(kwargs)
    return SearchChunk(**defaults)


# ── SearchChunk.to_render_dict ──────────────────────────────────────────────

class TestToRenderDict:
    def test_default_includes_internal_ids(self):
        d = _chunk().to_render_dict()
        assert d["chunk_id"] == "chunk-xyz"
        assert d["source_ids"] == ["src-001", "src-002"]

    def test_default_preserves_public_fields(self):
        d = _chunk().to_render_dict()
        assert d["conversation_id"] == "conv-abc"
        assert d["score"] == 0.85
        assert d["chunk_type"] == "exchange"
        assert d["text"] == "hello world"


# ── json_fmt._json_chunk_list ───────────────────────────────────────────────

class TestJsonChunkList:
    def _rows(self, **kwargs):
        return [_chunk(**kwargs).to_render_dict()]

    def test_default_includes_internal_ids(self):
        out = json_fmt._json_chunk_list(self._rows())
        assert out[0]["chunk_id"] == "chunk-xyz"
        assert out[0]["source_ids"] == ["src-001", "src-002"]

    def test_default_preserves_conversation_id(self):
        out = json_fmt._json_chunk_list(self._rows())
        assert out[0]["conversation_id"] == "conv-abc"


# ── json_fmt.render_search ──────────────────────────────────────────────────

class TestJsonFmtRenderSearch:
    def _results(self):
        return [_chunk().to_render_dict()]

    def test_default_includes_chunk_ids(self):
        from painted import Fidelity
        out = json_fmt.render_search(self._results(), Fidelity(), query="q", view="chunks")
        chunk = out["results"][0]
        assert chunk["chunk_id"] == "chunk-xyz"
        assert chunk["source_ids"] == ["src-001", "src-002"]
        assert chunk["conversation_id"] == "conv-abc"
        assert chunk["score"] == 0.85
        assert "conversation" in chunk

    def test_thread_mode_default_includes_chunk_ids(self):
        from painted import Fidelity
        rows = self._results()
        out = json_fmt.render_search(
            SearchView(results=rows, view="thread", tier1=rows, tier2=[]),
            Fidelity(), query="q",
        )
        assert out["tier1"][0]["chunk_id"] == "chunk-xyz"


# ── serve_fmt.render_search ─────────────────────────────────────────────────

class TestServeFmtRenderSearch:
    def test_default_includes_chunk_ids(self):
        from painted import Fidelity
        chunk = _chunk()
        out = serve_render_search([chunk], Fidelity())
        result = out["results"][0]
        assert result["chunk_id"] == "chunk-xyz"
        assert result["source_ids"] == ["src-001", "src-002"]

    def test_default_preserves_conversation_id(self):
        from painted import Fidelity
        out = serve_render_search([_chunk()], Fidelity())
        assert out["results"][0]["conversation_id"] == "conv-abc"

    def test_result_count_unchanged(self):
        from painted import Fidelity
        chunks = [_chunk(), _chunk(conversation_id="c2", chunk_id="k2")]
        out = serve_render_search(chunks, Fidelity())
        assert out["result_count"] == 2


# ── Regression: conversation_id in conversation serializers ─────────────────

class TestConversationIdRegression:
    """conversation_id (as 'id') must remain visible in conversation output."""

    def test_serialize_conversation_summary_includes_id(self):
        from dataclasses import dataclass
        from siftd.serialization.conversations import serialize_conversation_summary

        @dataclass
        class FakeSummary:
            id: str = "conv-abc"
            workspace_path: str = "/proj"
            model: str = "gpt-4"
            started_at: str = "2024-01-01T00:00:00Z"
            prompt_count: int = 1
            response_count: int = 1
            total_tokens: int = 100
            cost: float = 0.01
            tags: list = None

            def __post_init__(self):
                if self.tags is None:
                    self.tags = []

        out = serialize_conversation_summary(FakeSummary())
        assert "id" in out
        assert out["id"] == "conv-abc"
