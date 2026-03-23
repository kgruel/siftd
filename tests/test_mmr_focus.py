from siftd.search import _mmr_rerank_python, mmr_rerank


def test_mmr_focus_smoke():
    base = [{"conversation_id": "c1", "embedding": [1.0, 0.0], "score": 0.9}, {"conversation_id": "c2", "embedding": [0.0, 1.0], "score": 0.8}]
    out = mmr_rerank(base, query_embedding=[1.0, 0.0], limit=2)
    assert [r["conversation_id"] for r in out] == ["c1", "c2"] and "embedding" not in out[0]
    assert [r["conversation_id"] for r in _mmr_rerank_python(base, lambda_=0.7, limit=2)] == ["c1", "c2"]
