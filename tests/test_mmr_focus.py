import builtins

from siftd.search import mmr_rerank


def test_mmr_focus_selection_and_numpy_fallback(monkeypatch):
    base = [
        {"conversation_id": "c1", "embedding": [1.0, 0.0], "score": 0.9},
        {"conversation_id": "c2", "embedding": [0.0, 1.0], "score": 0.8},
    ]
    out = mmr_rerank(base, query_embedding=[1.0, 0.0], limit=2)
    assert [r["conversation_id"] for r in out] == ["c1", "c2"] and "embedding" not in out[0]

    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("no numpy")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out2 = mmr_rerank(base + [{"conversation_id": "c1", "embedding": [0.9, 0.1], "score": 0.85}], query_embedding=[1.0, 0.0], limit=2)
    assert [r["conversation_id"] for r in out2] == ["c1", "c2"]
