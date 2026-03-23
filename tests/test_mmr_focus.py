import builtins

from siftd.search import mmr_rerank


def test_mmr_focus_basic_selection():
    out = mmr_rerank(
        [
            {"conversation_id": "c1", "embedding": [1.0, 0.0], "score": 0.9},
            {"conversation_id": "c2", "embedding": [0.0, 1.0], "score": 0.8},
        ],
        query_embedding=[1.0, 0.0],
        limit=2,
    )
    assert [r["conversation_id"] for r in out] == ["c1", "c2"] and "embedding" not in out[0]


def test_mmr_focus_numpy_import_fallback(monkeypatch):
    orig_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("no numpy")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = mmr_rerank(
        [
            {"conversation_id": "c1", "embedding": [1.0, 0.0], "score": 0.9},
            {"conversation_id": "c1", "embedding": [0.9, 0.1], "score": 0.85},
            {"conversation_id": "c2", "embedding": [0.0, 1.0], "score": 0.5},
        ],
        query_embedding=[1.0, 0.0],
        limit=2,
    )
    assert [r["conversation_id"] for r in out] == ["c1", "c2"]
