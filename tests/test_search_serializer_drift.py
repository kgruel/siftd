"""Anti-drift tests for search dataclass serialization."""

from dataclasses import fields
from types import SimpleNamespace as NS

import pytest

from siftd.domain.search_types import ConversationSearchSummary, ScoreBreakdown, SearchChunk
from siftd.serialization.serve_fmt import render_search


def test_serve_search_chunk_serialization_default_includes_internal_ids():
    """Phase 2: render_search() default mode includes chunk_id and source_ids."""
    chunk = SearchChunk(
        conversation_id="c1",
        score=0.9,
        text="hello",
        chunk_type="exchange",
        workspace_path="/repo",
        started_at="2024-01-01T00:00:00Z",
        chunk_id="k1",
        source_ids=["p1"],
        breakdown=ScoreBreakdown(embedding_sim=0.9),
        file_refs=[],
        exchanges=[("p1", "q", "a")],
        context_window=[("p1", "q", "a", True)],
    )
    out = render_search([chunk], NS(depth=1))
    result = out["results"][0]

    assert result["chunk_id"] == "k1"
    assert result["source_ids"] == ["p1"]
    assert "conversation_id" in result


def test_serve_search_curated_shape_and_roundtrip():
    """Slice 4: serve_fmt emits the curated wire chunk shape (mirroring
    output/json_fmt — a ``conversation`` sub-object, not raw dataclass fields),
    and carries the fields the deserializer needs to rebuild a render-identical
    SearchView (display_label, exchanges)."""
    from siftd.api.deserialize import deserialize_search_view
    from siftd.domain.search_types import SearchView

    chunk = SearchChunk(
        conversation_id="c1",
        score=0.9,
        text="hello",
        chunk_type="exchange",
        workspace_path="/repo",
        started_at="2024-01-01T00:00:00Z",
        chunk_id="k1",
        source_ids=["p1"],
        breakdown=ScoreBreakdown(embedding_sim=0.9),
        file_refs=[],
        exchanges=[("p1", "q", "a")],
        context_window=[("p1", "q", "a", True)],
    )
    sv = SearchView(results=[chunk.to_render_dict()], view="chunks")
    out = render_search(sv, NS(depth=1), mode="hybrid")
    result = out["results"][0]

    # Curated public shape (matches output/json_fmt), NOT the raw dataclass fields.
    assert result["conversation_id"] == "c1"
    assert result["chunk_id"] == "k1"
    assert result["source_ids"] == ["p1"]
    assert result["conversation"] == {"started_at": "2024-01-01", "workspace": "/repo"}
    assert result["display_label"] == "EXCHANGE"
    assert result["exchanges"] == [["p1", "q", "a"]]
    assert "breakdown" in result
    assert out["view"] == "chunks" and out["mode"] == "hybrid"

    # Round-trips back to a render-dict the formatters consume.
    back = deserialize_search_view(out)
    assert back.view == "chunks"
    r0 = back.results[0]
    assert r0["_workspace"] == "/repo" and r0["_started_at"] == "2024-01-01"
    assert r0["_exchanges"] == [("p1", "q", "a")]
    assert r0["chunk_id"] == "k1" and r0["source_ids"] == ["p1"]


def test_scorebreakdown_rrf_fields_survive_wire_roundtrip():
    """Slice 4: vector_rank / keyword_rank / fused_score must survive render_search
    (into the wire dict) AND deserialize_search_view (back onto ScoreBreakdown).

    The prior round-trip test only asserted ``"breakdown" in result`` — vacuous for
    the new fields; a serializer that dropped them would still pass. This pins the
    exact keys/values on both wire crossings."""
    from siftd.api.deserialize import deserialize_search_view
    from siftd.domain.search_types import SearchView

    chunk = SearchChunk(
        conversation_id="c1",
        score=0.031513,  # the fused score in hybrid mode
        text="hello",
        chunk_type="exchange",
        chunk_id="k1",
        source_ids=["p1"],
        breakdown=ScoreBreakdown(
            embedding_sim=0.42, vector_rank=2, keyword_rank=5, fused_score=0.031513
        ),
    )
    out = render_search(SearchView(results=[chunk.to_render_dict()], view="chunks"), NS(depth=1), mode="hybrid")
    wire_bd = out["results"][0]["breakdown"]
    assert wire_bd["vector_rank"] == 2
    assert wire_bd["keyword_rank"] == 5
    assert wire_bd["fused_score"] == pytest.approx(0.031513)  # to_dict rounds to 6dp

    back_bd = deserialize_search_view(out).results[0]["breakdown"]
    assert isinstance(back_bd, ScoreBreakdown)
    assert back_bd.vector_rank == 2
    assert back_bd.keyword_rank == 5
    assert back_bd.fused_score == pytest.approx(0.031513)


def test_conversation_summary_render_mapping_is_explicit():
    """Conversation summary renderer must map all dataclass fields intentionally."""
    summary = ConversationSearchSummary(
        conversation_id="c1",
        max_score=0.9,
        mean_score=0.8,
        chunk_count=3,
        best_excerpt="x",
        workspace_path="repo",
        started_at="2024-01-01",
        file_refs=[],
    )
    rendered = summary.to_render_dict()

    mapped = {
        "conversation_id": rendered["conversation_id"],
        "max_score": rendered["max_score"],
        "mean_score": rendered["mean_score"],
        "chunk_count": rendered["chunk_count"],
        "best_excerpt": rendered["best_excerpt"],
        "workspace_path": rendered["_workspace"],
        "started_at": rendered["_started_at"],
        "file_refs": rendered["file_refs"],
    }
    expected_keys = {f.name for f in fields(ConversationSearchSummary)}
    assert set(mapped.keys()) == expected_keys
