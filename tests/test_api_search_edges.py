import sys
from types import SimpleNamespace

import pytest

import siftd.api.search as api_search


def test_lazy_getattr_and_missing(monkeypatch):
    fake_mod = SimpleNamespace(SearchResult=object, apply_temporal_weight=lambda *a, **k: [], IndexCompatError=RuntimeError)
    monkeypatch.setattr("importlib.import_module", lambda name: fake_mod)
    assert api_search.__getattr__("SearchResult") is object
    with pytest.raises(AttributeError):
        api_search.__getattr__("nope")


def test_wrapper_delegation(monkeypatch):
    fake_embeddings = SimpleNamespace(
        open_embeddings_db=lambda db_path, read_only=False: (db_path, read_only),
        search_similar=lambda *a, **k: [{"ok": 1}],
        validate_index_compat=lambda *a, **k: None,
    )
    fake_fts = SimpleNamespace(fts5_recall_conversations=lambda conn, q, limit=80: ({"c1"}, "and"))
    monkeypatch.setitem(sys.modules, "siftd.storage.embeddings", fake_embeddings)
    monkeypatch.setitem(sys.modules, "siftd.storage.fts", fake_fts)

    assert api_search.open_embeddings_db("x", read_only=True) == ("x", True)
    assert api_search.search_similar("c", [0.1], limit=1)[0]["ok"] == 1
    assert api_search.validate_index_compat("c", "b", "m", 1, 1) is None
    assert api_search.fts5_recall_conversations("c", "q") == ({"c1"}, "and")


def test_list_ids_build_index_and_fts_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(api_search, "fetch_all_conversation_ids", lambda conn: ["a", "b"])
    assert api_search.list_conversation_ids(object()) == {"a", "b"}

    fake_embeddings_api = SimpleNamespace(
        require_embeddings=lambda *_a, **_k: None,
        build_embeddings_index=lambda **k: SimpleNamespace(chunks_added=2, total_chunks=3),
        SCHEMA_VERSION=1,
        get_backend=lambda **k: SimpleNamespace(name="b", model="m", dimension=1, embed_one=lambda q: [0.1]),
    )
    monkeypatch.setitem(sys.modules, "siftd.embeddings", fake_embeddings_api)
    assert api_search.build_index(db_path=tmp_path / "db") == {"chunks_added": 2, "total_chunks": 3}

    class _Conn:
        def close(self):
            pass

    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: {"keep"},
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(api_search, "fts5_search_content", lambda conn, q, limit=10: [{"conversation_id": "drop", "rank": -2.0, "snippet": "x", "side": "prompt"}, {"conversation_id": "keep", "rank": -1.0, "snippet": "y", "side": "response"}])

    out = api_search.hybrid_search("q", db_path=tmp_path / "db.sqlite", mode="fts", n=2)
    assert len(out) == 1 and out[0]["conversation_id"] == "keep" and out[0]["score"] == 1.0


def test_hybrid_mode_candidate_empty_and_no_results(monkeypatch, tmp_path):
    class _Conn:
        def close(self):
            pass

    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: set(),
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setitem(sys.modules, "siftd.embeddings", SimpleNamespace(SCHEMA_VERSION=1))
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(api_search, "fts5_recall_conversations", lambda conn, q, limit=80: ({"x"}, "and"))
    monkeypatch.setattr(api_search, "open_embeddings_db", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(api_search, "validate_index_compat", lambda *a, **k: None)
    monkeypatch.setattr(api_search, "search_similar", lambda *a, **k: [])

    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_one=lambda q: [0.1])
    assert api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", embed_backend=backend, embed_db=tmp_path / "e.db") == []

    fake_search.resolve_candidates = lambda *a, **k: {"x"}
    assert api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", embed_backend=backend, embed_db=tmp_path / "e.db") == []


def test_hybrid_mode_recency_and_mmr(monkeypatch, tmp_path):
    calls = {}

    class _Conn:
        def close(self):
            pass

    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: {"c1"},
        annotate_fts5_breakdown=lambda *a, **k: calls.setdefault("annotate", True),
        mmr_rerank=lambda results, *_a, **_k: (calls.setdefault("mmr", True), results)[1],
        apply_temporal_weight=lambda results, *_a, **_k: (calls.setdefault("recency", True), results)[1],
    )
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setitem(sys.modules, "siftd.embeddings", SimpleNamespace(SCHEMA_VERSION=1))
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(api_search, "fts5_recall_conversations", lambda conn, q, limit=80: ({"c1"}, "and"))
    monkeypatch.setattr(api_search, "open_embeddings_db", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(api_search, "validate_index_compat", lambda *a, **k: None)
    monkeypatch.setattr(api_search, "fetch_conversation_timestamps", lambda conn, ids: {"c1": "2024-01-01"})
    monkeypatch.setattr(api_search, "search_similar", lambda *a, **k: [{"conversation_id": "c1", "score": 0.9, "source_ids": [], "chunk_id": "x"}])

    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_one=lambda q: [0.1])
    out = api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", recency=True, rerank="mmr", embed_backend=backend, embed_db=tmp_path / "e.db")
    assert out and calls.get("annotate") and calls.get("recency") and calls.get("mmr")


def test_hybrid_uses_default_backend_and_fts_ids_when_candidates_none(monkeypatch, tmp_path):
    class _Conn:
        def close(self):
            pass

    calls = {}
    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: None,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_one=lambda q: [0.1])
    fake_embeddings = SimpleNamespace(SCHEMA_VERSION=1, get_backend=lambda preferred=None, verbose=False: calls.setdefault("backend", backend))

    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setitem(sys.modules, "siftd.embeddings", fake_embeddings)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(api_search, "fts5_recall_conversations", lambda conn, q, limit=80: ({"c1"}, "and"))
    monkeypatch.setattr(api_search, "open_embeddings_db", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(api_search, "validate_index_compat", lambda *a, **k: None)
    monkeypatch.setattr(api_search, "search_similar", lambda conn, emb, limit=10, conversation_ids=None, include_embeddings=False: [{"conversation_id": next(iter(conversation_ids)), "score": 0.9, "source_ids": [], "chunk_id": "x"}])

    out = api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", embed_db=tmp_path / "e.db")
    assert out and calls.get("backend") is backend
