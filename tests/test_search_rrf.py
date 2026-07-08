"""Slice-4 RRF hybrid engine: fusion, threshold rewire, fts normalization, degrade."""

from types import SimpleNamespace as NS

import pytest

import siftd.api.search as s
from siftd.domain.search_types import ScoreBreakdown, SearchChunk, SearchView


def _vec(chunk_id, conv, *, vector_rank, sim=0.5, source_ids=("p1",)):
    return {
        "conversation_id": conv,
        "chunk_id": chunk_id,
        "chunk_type": "exchange",
        "text": "t",
        "source_ids": list(source_ids),
        "breakdown": ScoreBreakdown(embedding_sim=sim, vector_rank=vector_rank),
    }


def _hit(conv, event_id, rank=-1.0, kind="response"):
    return {"conversation_id": conv, "event_id": event_id, "kind": kind, "snippet": "s", "rank": rank}


# ---------------------------------------------------------------------------
# Strategy default (F3 ruling guard)
# ---------------------------------------------------------------------------


def test_hybrid_strategy_is_per_preset_with_env_override(monkeypatch):
    """0.11.0: the default strategy is per-preset (strong → rrf, weak/local → narrow);
    SIFTD_HYBRID_STRATEGY still force-overrides either way."""
    strong = NS(name="remote:voyage")
    weak = NS(name="fastembed")

    monkeypatch.delenv("SIFTD_HYBRID_STRATEGY", raising=False)
    assert s._hybrid_strategy(strong) == "rrf"
    assert s._hybrid_strategy(weak) == "narrow"

    # env override wins over the preset default, both directions.
    monkeypatch.setenv("SIFTD_HYBRID_STRATEGY", "narrow")
    assert s._hybrid_strategy(strong) == "narrow"
    monkeypatch.setenv("SIFTD_HYBRID_STRATEGY", "rrf")
    assert s._hybrid_strategy(weak) == "rrf"
    # an unrecognized value is ignored → falls back to the preset default.
    monkeypatch.setenv("SIFTD_HYBRID_STRATEGY", "bogus")
    assert s._hybrid_strategy(strong) == "rrf"
    assert s._hybrid_strategy(weak) == "narrow"


def test_preset_recall_defaults(monkeypatch):
    """Narrow FTS width is a global default (40) regardless of preset strength."""
    assert s._preset_recall(NS(name="remote:voyage")) == 40
    assert s._preset_recall(NS(name="fastembed")) == 40
    assert s._preset_recall(NS(name="remote:ollama")) == 40


# ---------------------------------------------------------------------------
# Bridging
# ---------------------------------------------------------------------------


def test_bridge_prompt_event_hit_to_chunk(monkeypatch):
    """An FTS hit on a PROMPT event maps to the chunk whose source_ids covers it."""
    vec = [_vec("k1", "c1", vector_rank=1, source_ids=["p1", "r1"])]
    kw = [_hit("c1", "p1", kind="prompt")]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {
        "p1": [{"chunk_id": "k1", "conversation_id": "c1", "chunk_type": "exchange", "text": "t", "source_ids": ["p1", "r1"]}]
    })
    out = s._fuse_hybrid(vec, kw, object(), n=10)
    assert len(out) == 1
    bd = out[0]["breakdown"]
    assert bd.vector_rank == 1 and bd.keyword_rank == 1 and bd.fts5_matched is True


def test_bridge_response_event_hit_to_chunk(monkeypatch):
    """An FTS hit on a RESPONSE event (source_ids carries it since slice 2) also bridges."""
    vec = [_vec("k1", "c1", vector_rank=1, source_ids=["p1", "r1"])]
    kw = [_hit("c1", "r1", kind="response")]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {
        "r1": [{"chunk_id": "k1", "conversation_id": "c1", "chunk_type": "exchange", "text": "t", "source_ids": ["p1", "r1"]}]
    })
    out = s._fuse_hybrid(vec, kw, object(), n=10)
    assert out[0]["breakdown"].keyword_rank == 1


def test_unbridged_hit_becomes_entrant(monkeypatch):
    """An FTS hit with no covering chunk enters fusion as a vector-free entrant."""
    kw = [_hit("c1", "e1", kind="prompt")]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {})  # nothing covers e1
    out = s._fuse_hybrid([], kw, object(), n=10)
    assert len(out) == 1
    assert out[0]["chunk_id"] is None
    assert out[0]["event_id"] == "e1"
    bd = out[0]["breakdown"]
    assert bd.vector_rank is None and bd.keyword_rank == 1
    assert out[0]["score"] == pytest.approx(1 / 21)  # w_kw=1.0 / (k_kw=20 + rank 1)


def test_bridged_hit_never_also_an_entrant(monkeypatch):
    """Dedup: a hit that bridges to a chunk does not additionally appear as an entrant."""
    vec = [_vec("k1", "c1", vector_rank=1, source_ids=["p1"])]
    kw = [_hit("c1", "p1", kind="prompt")]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {
        "p1": [{"chunk_id": "k1", "conversation_id": "c1", "chunk_type": "exchange", "text": "t", "source_ids": ["p1"]}]
    })
    out = s._fuse_hybrid(vec, kw, object(), n=10)
    assert len(out) == 1 and out[0]["chunk_id"] == "k1"


def test_bridged_chunk_absent_from_vector_list_enters_as_keyword_only(monkeypatch):
    """A covering chunk not in the vector top-K still enters fusion (keyword-only)."""
    kw = [_hit("c1", "r9", kind="response")]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {
        "r9": [{"chunk_id": "k9", "conversation_id": "c1", "chunk_type": "exchange", "text": "kw", "source_ids": ["p9", "r9"]}]
    })
    out = s._fuse_hybrid([], kw, object(), n=10)
    assert len(out) == 1 and out[0]["chunk_id"] == "k9"
    bd = out[0]["breakdown"]
    assert bd.vector_rank is None and bd.keyword_rank == 1


# ---------------------------------------------------------------------------
# Fused ordering math
# ---------------------------------------------------------------------------


def test_fused_score_math_and_both_lists_outrank_one(monkeypatch):
    """A chunk in BOTH lists outranks a better-vector-rank chunk in only one list."""
    vec = [
        _vec("k_vec", "c1", vector_rank=1),  # vector only, rank 1
        _vec("k_both", "c2", vector_rank=2, source_ids=["p2"]),  # vector rank 2 + keyword
    ]
    kw = [_hit("c2", "p2", kind="prompt")]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {
        "p2": [{"chunk_id": "k_both", "conversation_id": "c2", "chunk_type": "exchange", "text": "t", "source_ids": ["p2"]}]
    })
    out = s._fuse_hybrid(vec, kw, object(), n=10)
    assert [r["chunk_id"] for r in out] == ["k_both", "k_vec"]
    # k_both: vector rank 2 + keyword rank 1; k_vec: vector rank 1 only (k_vec=k_kw=20).
    assert out[0]["score"] == pytest.approx(1 / 22 + 1 / 21)
    assert out[1]["score"] == pytest.approx(1 / 21)


def test_keyword_rank_is_best_among_bridged_hits(monkeypatch):
    """A chunk bridged by multiple hits takes the best (lowest) keyword rank."""
    vec = [_vec("k1", "c1", vector_rank=5, source_ids=["p1", "r1"])]
    # p1 is the 3rd keyword hit, r1 is the 1st — the chunk should take rank 1.
    kw = [_hit("c1", "r1"), _hit("cX", "eX"), _hit("c1", "p1", kind="prompt")]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {
        "r1": [{"chunk_id": "k1", "conversation_id": "c1", "chunk_type": "exchange", "text": "t", "source_ids": ["p1", "r1"]}],
        "p1": [{"chunk_id": "k1", "conversation_id": "c1", "chunk_type": "exchange", "text": "t", "source_ids": ["p1", "r1"]}],
        # eX unbridged → entrant
    })
    out = s._fuse_hybrid(vec, kw, object(), n=10)
    k1 = next(r for r in out if r["chunk_id"] == "k1")
    assert k1["breakdown"].keyword_rank == 1


# ---------------------------------------------------------------------------
# Conversation-dedup rollup (the flooding fix on the RRF path)
# ---------------------------------------------------------------------------


def test_dedup_keeps_best_ranked_chunk_per_conversation(monkeypatch):
    """The fused chunk ranking is projected onto distinct conversations: a conversation
    with several fused chunks contributes only its best-ranked one."""
    vec = [
        _vec("k1", "c1", vector_rank=1),  # c1's best chunk
        _vec("k2", "c1", vector_rank=2),  # c1 again — flooding
        _vec("k3", "c2", vector_rank=3),  # c2
    ]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {})
    out = s._fuse_hybrid(vec, [], object(), n=10)
    assert [r["chunk_id"] for r in out] == ["k1", "k3"]  # k2 (c1 dup) suppressed
    assert [r["conversation_id"] for r in out] == ["c1", "c2"]


def test_dedup_trims_to_n_distinct_conversations(monkeypatch):
    """n bounds distinct conversations, not chunk slots."""
    vec = [_vec(f"k{i}", f"c{i}", vector_rank=i + 1) for i in range(5)]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {})
    out = s._fuse_hybrid(vec, [], object(), n=3)
    assert [r["conversation_id"] for r in out] == ["c0", "c1", "c2"]


def test_dedup_off_preserves_all_chunk_slots(monkeypatch):
    """dedup=False is the pre-rollup slot behavior (a conv may repeat) — the mode the
    sweep self-check pins."""
    vec = [_vec("k1", "c1", vector_rank=1), _vec("k2", "c1", vector_rank=2)]
    monkeypatch.setattr(s, "chunks_for_events", lambda conn, eids: {})
    out = s._fuse_hybrid(vec, [], object(), n=10, dedup=False)
    assert [r["chunk_id"] for r in out] == ["k1", "k2"]


# ---------------------------------------------------------------------------
# Threshold / first rewire to cosine (entrants exempt)
# ---------------------------------------------------------------------------


def test_threshold_tests_cosine_and_keeps_entrants():
    """--threshold 0.5 drops low-cosine vector chunks but KEEPS FTS-only entrants."""
    vec_chunk = SearchChunk(conversation_id="c1", score=0.03, text="t", chunk_type="exchange",
                            chunk_id="k1", breakdown=ScoreBreakdown(embedding_sim=0.2, vector_rank=1))
    entrant = SearchChunk(conversation_id="c2", score=0.016, text="t", chunk_type="prompt",
                          breakdown=ScoreBreakdown(embedding_sim=0.0))  # vector_rank None
    kept = s.filter_by_threshold([vec_chunk, entrant], threshold=0.5)
    assert [c.conversation_id for c in kept] == ["c2"]  # low-cosine dropped, entrant exempt


def test_first_mention_gates_on_cosine(tmp_path):
    """--select first tests cosine, not the ~0.02-scale fused score."""
    from siftd.storage.sqlite import open_database

    db = tmp_path / "m.db"
    open_database(db).close()
    hi = SearchChunk(conversation_id="c1", score=0.03, text="t", chunk_type="exchange",
                     chunk_id="k1", breakdown=ScoreBreakdown(embedding_sim=0.7, vector_rank=1))
    lo = SearchChunk(conversation_id="c2", score=0.02, text="t", chunk_type="exchange",
                     chunk_id="k2", breakdown=ScoreBreakdown(embedding_sim=0.5, vector_rank=2))
    picked = s.first_mention([hi, lo], threshold=0.65, db_path=db)
    assert picked is not None and picked.conversation_id == "c1"


# ---------------------------------------------------------------------------
# FTS normalization (bounded, monotone-increasing with match quality)
# ---------------------------------------------------------------------------


def test_fts_score_normalization_monotone_and_bounded():
    ranks = [-0.5, -1.0, -5.0, -20.0]  # more negative = better bm25
    scores = [s._normalize_fts_score(r) for r in ranks]
    assert all(0 < x < 1 for x in scores)
    assert scores == sorted(scores)  # monotone increasing with |rank|
    assert scores[-1] == max(scores)  # best (most negative) → max score


def test_fts_mode_top_hit_carries_max_normalized_score(tmp_path):
    """Integration: the top SQL-ordered fts hit carries the max normalized score."""
    from siftd.storage.fts import rebuild_fts_index
    from siftd.storage.sqlite import open_database

    db = tmp_path / "m.db"
    conn = open_database(db, read_only=False)
    try:
        conn.execute("INSERT INTO harnesses (id, name) VALUES ('h1', 'test')")
        # Vary term frequency so bm25 ranks differ across conversations.
        docs = {"c_a": "alpha", "c_b": "alpha alpha alpha alpha", "c_c": "alpha beta"}
        for i, (cid, text) in enumerate(docs.items()):
            conn.execute("INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?, ?, 'h1', '2024-01-01')", (cid, f"e{i}"))
            conn.execute("INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?, 'prompt', ?, '2024-01-01')", (f"ev{i}", cid))
            conn.execute("INSERT INTO event_content (id, event_id, block_index, block_type, content) VALUES (?, ?, 0, 'text', ?)", (f"ec{i}", f"ev{i}", f'{{"text":"{text}"}}'))
        rebuild_fts_index(conn)
        conn.commit()
    finally:
        conn.close()

    out = s.hybrid_search("alpha", db_path=db, mode="fts", n=10, exclude_active=False, include_derivative=True)
    assert len(out) >= 2
    scores = [c.score for c in out]
    assert scores[0] == max(scores)  # SQL-ordered best-first → max score first
    assert all(0 < sc < 1 for sc in scores)


# ---------------------------------------------------------------------------
# semantic / fts modes unaffected by fusion
# ---------------------------------------------------------------------------


def test_semantic_mode_skips_keyword_list(monkeypatch, tmp_path):
    """semantic mode is pure vector — it never touches the keyword list or bridge."""
    called = {"kw": 0, "bridge": 0}

    class _Conn:
        def close(self):
            return None

    fake_search = NS(
        resolve_candidates=lambda *a, **k: None,
        MAX_MMR_CANDIDATES=1000,
        annotate_fts5_breakdown=lambda *a, **k: None,
        mmr_rerank=lambda results, *_a, **_k: results,
        apply_temporal_weight=lambda results, *_a, **_k: results,
    )
    import sys
    monkeypatch.setitem(sys.modules, "siftd.search", fake_search)
    monkeypatch.setitem(sys.modules, "siftd.embeddings.indexer", NS(SCHEMA_VERSION=1))
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr(s, "open_embeddings_db", lambda *a, **k: _Conn())
    monkeypatch.setattr(s, "validate_index_compat", lambda *a, **k: None)
    monkeypatch.setattr(s, "fts5_search_content", lambda *a, **k: called.__setitem__("kw", called["kw"] + 1) or [])
    monkeypatch.setattr(s, "chunks_for_events", lambda *a, **k: called.__setitem__("bridge", called["bridge"] + 1) or {})
    monkeypatch.setattr(s, "search_similar", lambda *a, **k: [
        {"conversation_id": "c1", "score": 0.9, "chunk_id": "k1", "chunk_type": "exchange",
         "text": "t", "source_ids": [], "breakdown": ScoreBreakdown(embedding_sim=0.9)},
    ])

    backend = NS(name="b", model="m", dimension=1, embed_query=lambda q: [0.1])
    out = s.hybrid_search("q", db_path=tmp_path / "db", mode="semantic", embed_backend=backend, embed_db=tmp_path / "e.db")
    assert called["kw"] == 0 and called["bridge"] == 0
    assert out and out[0].score == pytest.approx(0.9)  # cosine, not fused


# ---------------------------------------------------------------------------
# Runtime degrade truthfulness (Part C)
# ---------------------------------------------------------------------------


def _seed_keyword_db(tmp_path):
    from siftd.storage.embeddings import open_embeddings_db
    from siftd.storage.fts import rebuild_fts_index
    from siftd.storage.sqlite import open_database

    db = tmp_path / "siftd.db"
    embed_db = tmp_path / "embeddings.db"
    open_embeddings_db(embed_db).close()  # empty index
    conn = open_database(db, read_only=False)
    try:
        conn.execute("INSERT INTO harnesses (id, name) VALUES ('h1', 'test')")
        for i in range(2):
            conn.execute("INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?, ?, 'h1', '2024-01-01')", (f"c_{i}", f"e{i}"))
            conn.execute("INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?, 'prompt', ?, '2024-01-01')", (f"ev_{i}", f"c_{i}"))
            conn.execute("INSERT INTO event_content (id, event_id, block_index, block_type, content) VALUES (?, ?, 0, 'text', ?)", (f"ec_{i}", f"ev_{i}", '{"text":"needle haystack"}'))
        rebuild_fts_index(conn)
        conn.commit()
    finally:
        conn.close()
    return db, embed_db


class _Transient:
    name = "remote:voyage"
    model = "m"
    dimension = 3

    def embed_query(self, _q):
        from siftd.embeddings.base import EmbeddingTransientError
        raise EmbeddingTransientError("voyage 429")


def test_search_view_degrades_transient_to_fts_and_reports_executed_mode(tmp_path):
    db, embed_db = _seed_keyword_db(tmp_path)
    sv = s.search_view(
        "needle", db_path=db, embed_db=embed_db, mode="hybrid",
        embed_backend=_Transient(), exclude_active=False, include_derivative=True,
    )
    assert sv.executed_mode == "fts"  # re-derived after the failure, not "hybrid"
    assert sv.results  # keyword hits still surfaced (degrade, not error)


def test_config_error_is_not_degraded(tmp_path):
    from siftd.embeddings.base import EmbeddingConfigError

    class _BadKey:
        name = "remote:voyage"
        model = "m"
        dimension = 3

        def embed_query(self, _q):
            raise EmbeddingConfigError("bad key")

    db, embed_db = _seed_keyword_db(tmp_path)
    with pytest.raises(EmbeddingConfigError):
        s.search_view(
            "needle", db_path=db, embed_db=embed_db, mode="hybrid",
            embed_backend=_BadKey(), exclude_active=False, include_derivative=True,
        )


def test_degrade_caveat_producer_distinguishes_unreachable_from_unconfigured():
    from siftd.api.caveats import ProducerContext, _search_degraded_unreachable_caveats

    ctx = ProducerContext(db_path="/nonexistent")
    # Runtime degrade: requested hybrid, executed fts → fires.
    op = NS(params={"mode": "hybrid"})
    result = SearchView(results=[{"conversation_id": "c1"}], view="chunks", executed_mode="fts")
    out = _search_degraded_unreachable_caveats(op, result, ctx)
    assert len(out) == 1 and out[0].check == "search-degraded-unreachable" and out[0].severity == "warning"

    # Normal hybrid run (no degrade) → silent.
    result_ok = SearchView(results=[{"c": 1}], view="chunks", executed_mode=None)
    assert _search_degraded_unreachable_caveats(op, result_ok, ctx) == []

    # "Not configured" case (resolved to fts from the start) → this producer stays silent
    # (search-mode-degraded owns that nudge).
    op_fts = NS(params={"mode": "fts"})
    result_fts = SearchView(results=[{"c": 1}], view="chunks", executed_mode=None)
    assert _search_degraded_unreachable_caveats(op_fts, result_fts, ctx) == []


def test_serve_fmt_reports_executed_mode_over_requested():
    from siftd.serialization.serve_fmt import render_search

    sv = SearchView(results=[], view="chunks", executed_mode="fts")
    out = render_search(sv, NS(depth=1), mode="hybrid")
    assert out["mode"] == "fts"  # the wire reports the engine that actually ran

    sv2 = SearchView(results=[], view="chunks", executed_mode=None)
    assert render_search(sv2, NS(depth=1), mode="hybrid")["mode"] == "hybrid"


def test_execute_for_render_surfaces_degrade_end_to_end(tmp_path, monkeypatch):
    """CLI path: execute_for_render(op) → SearchView degraded to fts + the caveat."""
    from painted import Fidelity

    from siftd.api.dispatch import Operation, execute_for_render

    db, embed_db = _seed_keyword_db(tmp_path)
    # Keep the embeddings-stale producer reading THIS tmp index (fast + isolated).
    monkeypatch.setattr("siftd.paths.embeddings_db_path", lambda: embed_db)

    op = Operation(
        path="/api/v1/search",
        method="GET",
        fn=s.search_view,
        params={
            "q": "needle", "db_path": db, "embed_db": embed_db, "n": 5, "mode": "hybrid",
            "embed_backend": _Transient(), "exclude_active": False, "include_derivative": True,
        },
        render_method="search",
        fidelity=Fidelity(),
        db=db,
    )
    result, findings = execute_for_render(op)
    assert result.executed_mode == "fts"
    assert result.results
    assert any(f.check == "search-degraded-unreachable" for f in findings)
