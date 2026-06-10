"""Phase 2: chunk IDs are now default-on in JSON.

Previously H5b hid them behind --debug-ids. Phase 2 reverses that to make
events addressable by default. The --debug-ids flag is retained as a
deprecated no-op alias for one minor version.

Covers:
- SearchChunk.to_render_dict default-on
- json_fmt._json_chunk_list default-on
- json_fmt.render_search default-on (debug_ids kwarg accepted, no longer gates)
- serve_fmt.render_search default-on (same)
- conversation_id preserved in conversation serializers (regression guard)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from siftd.domain.search_types import SearchChunk
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

    def test_explicit_debug_ids_false_omits(self):
        """Legacy opt-out still works for callers that want the old shape."""
        d = _chunk().to_render_dict(debug_ids=False)
        assert "chunk_id" not in d
        assert "source_ids" not in d


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
        out = json_fmt.render_search(self._results(), Fidelity(), query="q", mode="chunks")
        chunk = out["results"][0]
        assert chunk["chunk_id"] == "chunk-xyz"
        assert chunk["source_ids"] == ["src-001", "src-002"]
        assert chunk["conversation_id"] == "conv-abc"
        assert chunk["score"] == 0.85
        assert "conversation" in chunk

    def test_debug_ids_kwarg_is_noop(self):
        """--debug-ids no longer gates IDs; flag accepted for back-compat."""
        from painted import Fidelity
        out = json_fmt.render_search(
            self._results(), Fidelity(), query="q", mode="chunks", debug_ids=False,
        )
        chunk = out["results"][0]
        assert chunk["chunk_id"] == "chunk-xyz"
        assert chunk["source_ids"] == ["src-001", "src-002"]

    def test_thread_mode_default_includes_chunk_ids(self):
        from painted import Fidelity
        rows = self._results()
        out = json_fmt.render_search(
            rows, Fidelity(), query="q", mode="thread", tier1=rows, tier2=[],
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
        for debug_ids in (False, True):
            out = serve_render_search(chunks, Fidelity(), debug_ids=debug_ids)
            assert out["result_count"] == 2


# ── Serve route render_context plumbing (kept for back-compat) ─────────────

import pytest


@pytest.mark.serve
def test_search_route_still_accepts_debug_ids(monkeypatch, tmp_path):
    """search_route still accepts debug_ids parameter (kept for backward compat).

    The rendered output no longer changes based on the value, but the route
    still threads it through for one minor version of deprecation.
    """
    pytest.importorskip("litestar")
    from siftd.serve import routes

    seen_rc = []

    def fake_dispatch(*args, **kwargs):
        seen_rc.append(kwargs.get("render_context") or {})
        return {"result_count": 0, "results": []}

    monkeypatch.setattr(routes, "_dispatch", fake_dispatch)
    asyncio.run(routes.search_route.fn(SimpleNamespace(), tmp_path / "db.db", q="hi", debug_ids=False, mode=None))
    assert "debug_ids" in seen_rc[0]


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
