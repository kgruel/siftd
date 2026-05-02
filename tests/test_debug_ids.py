"""Tests for H5b: hide internal search-chunk IDs by default.

Covers:
- SearchChunk.to_render_dict(debug_ids=False/True)
- json_fmt._json_chunk_list and render_search
- serve_fmt.render_search
- Serve route plumbs debug_ids via render_context
- conversation_id preserved in conversation serializers (regression guard)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from siftd.domain.search_types import ScoreBreakdown, SearchChunk
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
    def test_default_omits_internal_ids(self):
        d = _chunk().to_render_dict()
        assert "chunk_id" not in d
        assert "source_ids" not in d

    def test_default_preserves_public_fields(self):
        d = _chunk().to_render_dict()
        assert d["conversation_id"] == "conv-abc"
        assert d["score"] == 0.85
        assert d["chunk_type"] == "exchange"
        assert d["text"] == "hello world"

    def test_debug_ids_includes_internal_ids(self):
        chunk = _chunk()
        d = chunk.to_render_dict(debug_ids=True)
        assert d["chunk_id"] == "chunk-xyz"
        assert d["source_ids"] == ["src-001", "src-002"]

    def test_debug_ids_preserves_public_fields(self):
        d = _chunk().to_render_dict(debug_ids=True)
        assert d["conversation_id"] == "conv-abc"


# ── json_fmt._json_chunk_list ───────────────────────────────────────────────

class TestJsonChunkList:
    def _rows(self, **kwargs):
        return [_chunk(**kwargs).to_render_dict(debug_ids=True)]

    def test_default_omits_internal_ids(self):
        out = json_fmt._json_chunk_list(self._rows())
        assert "chunk_id" not in out[0]
        assert "source_ids" not in out[0]

    def test_default_preserves_conversation_id(self):
        out = json_fmt._json_chunk_list(self._rows())
        assert out[0]["conversation_id"] == "conv-abc"

    def test_debug_ids_includes_internal_ids(self):
        out = json_fmt._json_chunk_list(self._rows(), debug_ids=True)
        assert out[0]["chunk_id"] == "chunk-xyz"
        assert out[0]["source_ids"] == ["src-001", "src-002"]


# ── json_fmt.render_search ──────────────────────────────────────────────────

class TestJsonFmtRenderSearch:
    def _results(self):
        return [_chunk().to_render_dict(debug_ids=True)]

    def test_default_snapshot(self):
        from painted import Fidelity
        out = json_fmt.render_search(self._results(), Fidelity(), query="q", mode="chunks")
        chunk = out["results"][0]
        assert "chunk_id" not in chunk
        assert "source_ids" not in chunk
        assert chunk["conversation_id"] == "conv-abc"
        assert chunk["score"] == 0.85
        assert "conversation" in chunk

    def test_debug_ids_snapshot(self):
        from painted import Fidelity
        out = json_fmt.render_search(
            self._results(), Fidelity(), query="q", mode="chunks", debug_ids=True
        )
        chunk = out["results"][0]
        assert chunk["chunk_id"] == "chunk-xyz"
        assert chunk["source_ids"] == ["src-001", "src-002"]
        assert chunk["conversation_id"] == "conv-abc"

    def test_thread_mode_default_omits_internal_ids(self):
        from painted import Fidelity
        rows = self._results()
        out = json_fmt.render_search(
            rows, Fidelity(), query="q", mode="thread", tier1=rows, tier2=[]
        )
        assert "chunk_id" not in out["tier1"][0]
        assert "source_ids" not in out["tier1"][0]

    def test_thread_mode_debug_ids_includes_them(self):
        from painted import Fidelity
        rows = self._results()
        out = json_fmt.render_search(
            rows, Fidelity(), query="q", mode="thread", tier1=rows, tier2=[], debug_ids=True
        )
        assert out["tier1"][0]["chunk_id"] == "chunk-xyz"


# ── serve_fmt.render_search ─────────────────────────────────────────────────

class TestServeFmtRenderSearch:
    def test_default_omits_internal_ids(self):
        from painted import Fidelity
        chunk = _chunk()
        out = serve_render_search([chunk], Fidelity())
        result = out["results"][0]
        assert "chunk_id" not in result
        assert "source_ids" not in result

    def test_default_preserves_conversation_id(self):
        from painted import Fidelity
        out = serve_render_search([_chunk()], Fidelity())
        assert out["results"][0]["conversation_id"] == "conv-abc"

    def test_debug_ids_includes_internal_ids(self):
        from painted import Fidelity
        chunk = _chunk()
        out = serve_render_search([chunk], Fidelity(), debug_ids=True)
        result = out["results"][0]
        assert result["chunk_id"] == "chunk-xyz"
        assert result["source_ids"] == ["src-001", "src-002"]

    def test_result_count_unchanged(self):
        from painted import Fidelity
        chunks = [_chunk(), _chunk(conversation_id="c2", chunk_id="k2")]
        for debug_ids in (False, True):
            out = serve_render_search(chunks, Fidelity(), debug_ids=debug_ids)
            assert out["result_count"] == 2


# ── Serve route render_context plumbing ────────────────────────────────────

import pytest


@pytest.mark.serve
def test_search_route_plumbs_debug_ids_false(monkeypatch, tmp_path):
    """search_route passes render_context={"debug_ids": False} by default."""
    import asyncio
    pytest.importorskip("litestar")
    from siftd.serve import routes

    seen_rc = []

    def fake_dispatch(*args, **kwargs):
        seen_rc.append(kwargs.get("render_context") or {})
        return {"result_count": 0, "results": []}

    monkeypatch.setattr(routes, "_dispatch", fake_dispatch)
    asyncio.run(routes.search_route.fn(SimpleNamespace(), tmp_path / "db.db", q="hi", debug_ids=False))
    assert seen_rc[0].get("debug_ids") is False


@pytest.mark.serve
def test_search_route_plumbs_debug_ids_true(monkeypatch, tmp_path):
    """search_route passes render_context={"debug_ids": True} when debug_ids=True."""
    import asyncio
    pytest.importorskip("litestar")
    from siftd.serve import routes

    seen_rc = []

    def fake_dispatch(*args, **kwargs):
        seen_rc.append(kwargs.get("render_context") or {})
        return {"result_count": 0, "results": []}

    monkeypatch.setattr(routes, "_dispatch", fake_dispatch)
    asyncio.run(routes.search_route.fn(SimpleNamespace(), tmp_path / "db.db", q="hi", debug_ids=True))
    assert seen_rc[0].get("debug_ids") is True


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
