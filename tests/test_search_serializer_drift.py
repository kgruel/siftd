"""Anti-drift tests for search dataclass serialization."""

from dataclasses import fields
from types import SimpleNamespace as NS

from siftd.domain.search_types import ConversationSearchSummary, ScoreBreakdown, SearchChunk
from siftd.serialization.serve_fmt import render_search


def test_serve_search_chunk_serialization_covers_all_dataclass_fields():
    """render_search() should serialize every SearchChunk field."""
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

    expected_keys = {f.name for f in fields(SearchChunk)}
    assert set(result.keys()) == expected_keys


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
