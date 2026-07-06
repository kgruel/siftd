import sys
from types import SimpleNamespace

import pytest

import siftd.api.search as api_search


class _Conn:
    def close(self):
        pass


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
    fake_fts = SimpleNamespace(fts5_recall_conversations=lambda conn, q, limit=80, raw_fts=False: ({"c1"}, "and"))
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
    )
    fake_indexer = SimpleNamespace(
        build_embeddings_index=lambda **k: SimpleNamespace(
            chunks_added=2,
            chunks_removed=1,
            conversations_indexed=4,
            conversations_pruned=0,
            total_chunks=3,
            backend_name="fastembed",
            model="bge",
            dimension=384,
        ),
    )
    monkeypatch.setitem(sys.modules, "siftd.embeddings", fake_embeddings_api)
    monkeypatch.setitem(sys.modules, "siftd.embeddings.indexer", fake_indexer)
    assert api_search.build_index(db_path=tmp_path / "db") == {
        "chunks_added": 2,
        "chunks_removed": 1,
        "conversations_indexed": 4,
        "conversations_pruned": 0,
        "total_chunks": 3,
        "backend_name": "fastembed",
        "model": "bge",
        "dimension": 384,
    }

    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: {"keep"},
        MAX_MMR_CANDIDATES=1000,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(api_search, "fts5_search_content", lambda conn, q, limit=10, raw_fts=False: [{"conversation_id": "drop", "rank": -2.0, "snippet": "x", "kind": "prompt"}, {"conversation_id": "keep", "rank": -1.0, "snippet": "y", "kind": "response"}])

    # bm25 rank -1.0 → normalized 1/(1+1) = 0.5 (bounded, monotone-increasing).
    out = api_search.hybrid_search("q", db_path=tmp_path / "db.sqlite", mode="fts", n=2)
    assert len(out) == 1 and out[0]["conversation_id"] == "keep" and out[0]["score"] == 0.5


# ---------------------------------------------------------------------------
# RRF hybrid branches — the vector list runs over the full candidate set (no FTS
# narrowing), fused with a chunk-level keyword list via the source_ids bridge.
# ---------------------------------------------------------------------------


def _rrf_fakes(monkeypatch):
    """Stub the RRF seams so hybrid_search runs without a DB or fastembed."""
    monkeypatch.setitem(sys.modules, "siftd.embeddings.indexer", SimpleNamespace(SCHEMA_VERSION=1))
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(api_search, "open_embeddings_db", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(api_search, "validate_index_compat", lambda *a, **k: None)
    monkeypatch.setattr(api_search, "fts5_search_content", lambda *a, **k: [])
    monkeypatch.setattr(api_search, "chunks_for_events", lambda *a, **k: {})


def test_hybrid_mode_candidate_empty_and_no_results(monkeypatch, tmp_path):
    monkeypatch.setenv("SIFTD_HYBRID_STRATEGY", "rrf")  # RRF is opt-in — pin the fusion path
    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: set(),
        MAX_MMR_CANDIDATES=1000,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    _rrf_fakes(monkeypatch)
    monkeypatch.setattr(api_search, "search_similar", lambda *a, **k: [])

    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    # Empty candidate set short-circuits before embedding.
    assert api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", embed_backend=backend, embed_db=tmp_path / "e.db") == []

    # Non-empty candidates but empty vector + empty keyword lists → no results.
    fake_search.resolve_candidates = lambda *a, **k: {"x"}
    assert api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", embed_backend=backend, embed_db=tmp_path / "e.db") == []


def test_hybrid_mode_forwards_tag_kind_to_resolve_candidates(monkeypatch, tmp_path):
    """I07: hybrid/semantic must honor --tag-kind like the FTS-only path does."""
    seen = {}

    def _capture(*a, **k):
        seen.update(k)
        return set()  # empty candidates short-circuits before embeddings

    fake_search = SimpleNamespace(
        resolve_candidates=_capture,
        MAX_MMR_CANDIDATES=1000,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    _rrf_fakes(monkeypatch)
    monkeypatch.setattr(api_search, "search_similar", lambda *a, **k: [])

    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    api_search.hybrid_search(
        "q", db_path=tmp_path / "db", mode="hybrid", embed_backend=backend,
        embed_db=tmp_path / "e.db", tag=["t"], tag_kind=["decision"],
    )
    assert seen.get("tag_kind") == ["decision"]


def test_hybrid_mode_recency_and_mmr(monkeypatch, tmp_path):
    monkeypatch.setenv("SIFTD_HYBRID_STRATEGY", "rrf")  # RRF is opt-in — pin the fusion path
    calls = {}

    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: {"c1"},
        MAX_MMR_CANDIDATES=1000,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: (calls.setdefault("mmr", True), results)[1],
        apply_temporal_weight=lambda results, *_a, **_k: (calls.setdefault("recency", True), results)[1],
    )
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    _rrf_fakes(monkeypatch)
    monkeypatch.setattr(api_search, "fetch_conversation_timestamps", lambda conn, ids: {"c1": "2024-01-01"})
    monkeypatch.setattr(
        api_search, "search_similar",
        lambda *a, **k: [{"conversation_id": "c1", "score": 0.9, "source_ids": [], "chunk_id": "x",
                          "chunk_type": "exchange", "text": "t",
                          "breakdown": api_search.ScoreBreakdown(embedding_sim=0.9)}],
    )

    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    out = api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", recency=True, rerank="mmr", embed_backend=backend, embed_db=tmp_path / "e.db")
    # Vector-only chunk (no keyword hit) still fuses (vector_rank contribution).
    assert out and out[0].conversation_id == "c1"
    assert out[0].breakdown.vector_rank == 1 and out[0].breakdown.keyword_rank is None
    assert calls.get("recency") and calls.get("mmr")


def test_hybrid_uses_default_backend_when_no_injection(monkeypatch, tmp_path):
    monkeypatch.setenv("SIFTD_HYBRID_STRATEGY", "rrf")  # RRF is opt-in — pin the fusion path
    calls = {}
    fake_search = SimpleNamespace(
        resolve_candidates=lambda *a, **k: None,
        MAX_MMR_CANDIDATES=1000,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])

    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setitem(sys.modules, "siftd.embeddings.base", SimpleNamespace(get_backend=lambda verbose=False: calls.setdefault("backend", backend)))
    _rrf_fakes(monkeypatch)
    monkeypatch.setattr(
        api_search, "search_similar",
        lambda conn, emb, limit=10, conversation_ids=None, include_embeddings=False:
            [{"conversation_id": "c1", "score": 0.9, "source_ids": [], "chunk_id": "x",
              "chunk_type": "exchange", "text": "t",
              "breakdown": api_search.ScoreBreakdown(embedding_sim=0.9)}],
    )

    out = api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid", embed_db=tmp_path / "e.db")
    assert out and calls.get("backend") is backend


# ---------------------------------------------------------------------------
# narrow-then-rank composition — the SHIPPED default (no SIFTD_HYBRID_STRATEGY).
# RRF's tests pin the env knob; these exercise the TRUE default path (fts5 recall
# ∩ candidates → embeddings rerank), so a composition regression in the default
# engine can't slip through as a pass.
# ---------------------------------------------------------------------------


def _narrow_fake_search(**over):
    base = dict(
        resolve_candidates=lambda *a, **k: {"c1", "c2", "c3"},
        MAX_MMR_CANDIDATES=1000,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _narrow_fakes(monkeypatch, fake_search):
    """Stub the narrow-then-rank seams so hybrid_search runs the DEFAULT engine
    without a DB, fastembed, or the SIFTD_HYBRID_STRATEGY knob set. Deliberately does
    NOT stub the RRF seams — if the RRF path ran by mistake it would hit a real
    fts5_search_content on the fake _Conn and fail loudly."""
    monkeypatch.delenv("SIFTD_HYBRID_STRATEGY", raising=False)  # true default = narrow
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setitem(sys.modules, "siftd.embeddings.indexer", SimpleNamespace(SCHEMA_VERSION=1))
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(api_search, "open_embeddings_db", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(api_search, "validate_index_compat", lambda *a, **k: None)


def _vec_result(conv="c2"):
    return {"conversation_id": conv, "score": 0.9, "source_ids": [], "chunk_id": "x",
            "chunk_type": "exchange", "text": "t",
            "breakdown": api_search.ScoreBreakdown(embedding_sim=0.9)}


def test_narrow_default_intersects_fts_with_candidates(monkeypatch, tmp_path):
    seen = {}
    fake_search = _narrow_fake_search(resolve_candidates=lambda *a, **k: {"c1", "c2", "c3"})
    _narrow_fakes(monkeypatch, fake_search)
    monkeypatch.setattr(api_search, "fts5_recall_conversations", lambda *a, **k: ({"c2", "c3", "c4"}, "and"))

    def _capture(conn, emb, limit=10, conversation_ids=None, include_embeddings=False):
        seen["cids"] = conversation_ids
        return [_vec_result("c2")]

    monkeypatch.setattr(api_search, "search_similar", _capture)
    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    out = api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid",
                                   rerank="relevance", embed_backend=backend, embed_db=tmp_path / "e.db")
    # fts5_ids ∩ candidates = {c2, c3} — the vector search runs over the intersection.
    assert seen["cids"] == {"c2", "c3"}
    assert out and out[0].conversation_id == "c2"


def test_narrow_default_empty_intersection_falls_back_to_candidates(monkeypatch, tmp_path):
    seen = {}
    fake_search = _narrow_fake_search(resolve_candidates=lambda *a, **k: {"c1"})
    _narrow_fakes(monkeypatch, fake_search)
    monkeypatch.setattr(api_search, "fts5_recall_conversations", lambda *a, **k: ({"c2"}, "and"))

    def _capture(conn, emb, limit=10, conversation_ids=None, include_embeddings=False):
        seen["cids"] = conversation_ids
        return [_vec_result("c1")]

    monkeypatch.setattr(api_search, "search_similar", _capture)
    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid",
                             rerank="relevance", embed_backend=backend, embed_db=tmp_path / "e.db")
    # Empty fts∩candidate intersection must NOT narrow to empty — it falls back to the
    # candidate set so a keyword miss doesn't zero out an otherwise-valid search.
    assert seen["cids"] == {"c1"}


def test_narrow_default_recency_runs_before_mmr(monkeypatch, tmp_path):
    order = []
    fake_search = _narrow_fake_search(
        resolve_candidates=lambda *a, **k: {"c1"},
        apply_temporal_weight=lambda results, *_a, **_k: (order.append("recency"), results)[1],
        mmr_rerank=lambda results, *_a, **_k: (order.append("mmr"), results)[1],
    )
    _narrow_fakes(monkeypatch, fake_search)
    monkeypatch.setattr(api_search, "fts5_recall_conversations", lambda *a, **k: ({"c1"}, "and"))
    monkeypatch.setattr(api_search, "fetch_conversation_timestamps", lambda conn, ids: {"c1": "2024-01-01"})
    monkeypatch.setattr(api_search, "search_similar", lambda *a, **k: [_vec_result("c1")])

    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid",
                             recency=True, rerank="mmr", embed_backend=backend, embed_db=tmp_path / "e.db")
    assert order == ["recency", "mmr"]  # temporal weight applied before diversity rerank


def test_narrow_default_forwards_tag_kind_and_runs_narrow_path(monkeypatch, tmp_path):
    seen = {}
    called = {"fts_recall": 0}

    def _capture_resolve(*a, **k):
        seen.update(k)
        return {"c1"}

    fake_search = _narrow_fake_search(resolve_candidates=_capture_resolve)
    _narrow_fakes(monkeypatch, fake_search)

    def _fts_recall(*a, **k):
        called["fts_recall"] += 1
        return ({"c1"}, "and")

    monkeypatch.setattr(api_search, "fts5_recall_conversations", _fts_recall)
    monkeypatch.setattr(api_search, "search_similar", lambda *a, **k: [_vec_result("c1")])
    backend = SimpleNamespace(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    out = api_search.hybrid_search("q", db_path=tmp_path / "db", mode="hybrid",
                                   rerank="relevance", embed_backend=backend, embed_db=tmp_path / "e.db",
                                   tag=["t"], tag_kind=["decision"])
    assert seen.get("tag_kind") == ["decision"]  # forwarded to candidate resolution
    assert called["fts_recall"] == 1  # the narrow path ran (not RRF)
    assert out and out[0].conversation_id == "c1"
