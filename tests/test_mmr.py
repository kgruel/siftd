"""Tests for MMR diversity reranking in search.py."""

import pytest

from siftd.search import mmr_rerank

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk(conv_id, embedding, score, **extra):
    """Build a minimal scored-chunk dict for mmr_rerank input."""
    return {"conversation_id": conv_id, "embedding": embedding, "score": score, **extra}


# Orthogonal unit vectors — cosine sim between any two is 0.0.
E1 = [1.0, 0.0, 0.0]
E2 = [0.0, 1.0, 0.0]
E3 = [0.0, 0.0, 1.0]


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------

class TestMmrRerankBasics:
    """Core mmr_rerank behavior."""

    def test_empty_input(self):
        """Empty results list returns empty."""
        assert mmr_rerank([], query_embedding=E1) == []

    def test_single_result(self):
        """Single result is returned as-is (minus embedding key)."""
        results = [_chunk("c1", E1, score=0.9)]
        out = mmr_rerank(results, query_embedding=E1, limit=5)

        assert len(out) == 1
        assert out[0]["conversation_id"] == "c1"
        assert out[0]["score"] == 0.9
        assert "embedding" not in out[0]

    def test_limit_respected(self):
        """Output size is capped at limit."""
        results = [_chunk(f"c{i}", E1, score=0.5) for i in range(20)]
        out = mmr_rerank(results, query_embedding=E1, limit=3)
        assert len(out) == 3

    def test_embedding_stripped(self):
        """Returned dicts do not contain the 'embedding' key."""
        results = [_chunk("c1", E1, score=0.8), _chunk("c2", E2, score=0.7)]
        out = mmr_rerank(results, query_embedding=E1, limit=10)
        for r in out:
            assert "embedding" not in r

    def test_extra_keys_preserved(self):
        """Non-standard keys in the input dict survive reranking."""
        results = [_chunk("c1", E1, score=0.8, text="hello", chunk_type="prompt")]
        out = mmr_rerank(results, query_embedding=E1, limit=5)
        assert out[0]["text"] == "hello"
        assert out[0]["chunk_type"] == "prompt"


# ---------------------------------------------------------------------------
# Conversation-level penalty (same-conversation suppression)
# ---------------------------------------------------------------------------

class TestConversationPenalty:
    """Same-conversation chunks are penalized with penalty=1.0."""

    def test_same_conv_suppressed(self):
        """Given two chunks from the same conversation and one from another,
        the second same-conv chunk should rank below the other-conv chunk
        even if its relevance score is higher."""
        results = [
            _chunk("c1", E1, score=0.95),   # best relevance, same conv as next
            _chunk("c1", E2, score=0.90),   # same conv — should be penalized
            _chunk("c2", E3, score=0.50),   # different conv — gets diversity boost
        ]
        out = mmr_rerank(results, query_embedding=E1, limit=3, lambda_=0.5)

        # First pick: c1 (highest relevance, no penalty)
        assert out[0]["conversation_id"] == "c1"
        # Second pick: c2 should beat the second c1 chunk because the same-conv
        # penalty (1.0) is worse than cross-conv penalty (0.0 for orthogonal vecs)
        assert out[1]["conversation_id"] == "c2"
        assert out[2]["conversation_id"] == "c1"

    def test_diversity_across_conversations(self):
        """With enough conversations, MMR selects one chunk per conversation first."""
        results = [
            _chunk("c1", E1, score=0.9),
            _chunk("c1", E2, score=0.85),
            _chunk("c2", E2, score=0.8),
            _chunk("c2", E3, score=0.75),
            _chunk("c3", E3, score=0.7),
        ]
        out = mmr_rerank(results, query_embedding=E1, limit=3, lambda_=0.5)
        conv_ids = [r["conversation_id"] for r in out]
        # All three different conversations should appear before any repeats
        assert set(conv_ids) == {"c1", "c2", "c3"}


# ---------------------------------------------------------------------------
# Lambda parameter
# ---------------------------------------------------------------------------

class TestLambdaParameter:
    """Lambda controls relevance vs diversity tradeoff."""

    @pytest.fixture
    def candidates(self):
        """Two chunks: one high-relevance (same conv as first pick),
        one lower-relevance from a different conv with orthogonal embedding."""
        return [
            _chunk("c1", E1, score=0.95),   # will always be first pick
            _chunk("c1", E2, score=0.90),   # same conv, high relevance
            _chunk("c2", E3, score=0.50),   # different conv, low relevance
        ]

    def test_lambda_1_pure_relevance(self, candidates):
        """λ=1.0 ignores diversity penalty entirely — pure relevance order."""
        out = mmr_rerank(candidates, query_embedding=E1, limit=3, lambda_=1.0)
        # With λ=1.0: mmr_score = 1.0 * relevance - 0.0 * penalty = relevance
        # So order is purely by score: 0.95, 0.90, 0.50
        scores = [r["score"] for r in out]
        assert scores == sorted(scores, reverse=True)

    def test_lambda_0_pure_diversity(self, candidates):
        """λ=0.0 ignores relevance — pure diversity (minimize penalty)."""
        out = mmr_rerank(candidates, query_embedding=E1, limit=3, lambda_=0.0)
        # With λ=0.0: mmr_score = 0.0 * relevance - 1.0 * penalty
        # First pick: c1/E1 (no penalty)
        # Second pick: c1/E2 has penalty=1.0 (same conv), c2/E3 has penalty=0.0 (orthogonal)
        # So c2 must be second
        assert out[0]["conversation_id"] == "c1"
        assert out[1]["conversation_id"] == "c2"

    def test_lambda_affects_ordering(self):
        """Different lambda values produce different orderings for the same input."""
        results = [
            _chunk("c1", E1, score=0.9),
            _chunk("c1", E2, score=0.85),
            _chunk("c2", E3, score=0.5),
        ]
        out_relevance = mmr_rerank(results, query_embedding=E1, limit=3, lambda_=1.0)
        out_diversity = mmr_rerank(results, query_embedding=E1, limit=3, lambda_=0.0)

        ids_rel = [r["conversation_id"] for r in out_relevance]
        ids_div = [r["conversation_id"] for r in out_diversity]
        # Pure relevance keeps same-conv together; pure diversity splits them
        assert ids_rel != ids_div


# ---------------------------------------------------------------------------
# Cross-conversation cosine similarity penalty
# ---------------------------------------------------------------------------

class TestCrossConversationPenalty:
    """When chunks are from different conversations, penalty = max cosine sim
    to already-selected embeddings."""

    def test_similar_embeddings_penalized(self):
        """A chunk with embedding similar to an already-selected one is penalized
        more than one with an orthogonal embedding."""
        nearly_e1 = [0.99, 0.1, 0.0]  # close to E1
        results = [
            _chunk("c1", E1, score=0.9),            # first pick
            _chunk("c2", nearly_e1, score=0.85),     # similar to c1's embedding
            _chunk("c3", E3, score=0.60),            # orthogonal to c1's embedding
        ]
        out = mmr_rerank(results, query_embedding=E1, limit=3, lambda_=0.5)
        # c3 (orthogonal, low penalty) should beat c2 (similar to c1, high penalty)
        assert out[1]["conversation_id"] == "c3"
        assert out[2]["conversation_id"] == "c2"

    def test_orthogonal_embeddings_no_penalty(self):
        """Orthogonal embeddings across different conversations get penalty ≈ 0."""
        results = [
            _chunk("c1", E1, score=0.9),
            _chunk("c2", E2, score=0.8),
            _chunk("c3", E3, score=0.7),
        ]
        # With orthogonal embeddings, diversity penalty is 0 for all,
        # so order should match relevance order regardless of lambda
        out = mmr_rerank(results, query_embedding=E1, limit=3, lambda_=0.5)
        scores = [r["score"] for r in out]
        assert scores == sorted(scores, reverse=True)
