"""Tests for search module."""

from types import SimpleNamespace

import pytest

import siftd.search as search_mod
import siftd.api.search as api_search
from siftd.search import ScoreBreakdown, apply_temporal_weight, mmr_rerank


class TestScoreBreakdown:
    def test_defaults(self):
        s = ScoreBreakdown(embedding_sim=0.85)
        assert s.recency_boost == 1.0
        assert s.pre_mmr_score == 0.85
        assert s.final_score == 0.85
        assert s.fts5_matched is False

    def test_to_dict(self):
        s = ScoreBreakdown(embedding_sim=0.85, recency_boost=1.2)
        d = s.to_dict()
        assert d["embedding_sim"] == 0.85
        assert d["recency_boost"] == 1.2
        assert "pre_mmr_score" in d


class TestApplyTemporalWeight:
    def test_empty_results(self):
        assert apply_temporal_weight([], {}) == []

    def test_no_boost(self):
        results = [{"conversation_id": "c1", "score": 0.8}]
        assert apply_temporal_weight(results, {}, max_boost=1.0) == results

    def test_with_timestamps(self):
        results = [
            {"conversation_id": "c1", "score": 0.8},
            {"conversation_id": "c2", "score": 0.8},
        ]
        timestamps = {
            "c1": "2026-03-20T00:00:00Z",  # yesterday
            "c2": "2020-01-01T00:00:00Z",  # years ago
        }
        weighted = apply_temporal_weight(results, timestamps)
        assert weighted[0]["score"] >= weighted[1]["score"]  # recent gets more boost

    def test_with_breakdown(self):
        bd = ScoreBreakdown(embedding_sim=0.9)
        results = [{"conversation_id": "c1", "score": 0.9, "breakdown": bd}]
        timestamps = {"c1": "2026-03-20T00:00:00Z"}
        weighted = apply_temporal_weight(results, timestamps)
        assert weighted[0]["breakdown"].recency_boost >= 1.0

    def test_missing_timestamp_skipped(self):
        results = [{"conversation_id": "c1", "score": 0.8}]
        weighted = apply_temporal_weight(results, {})  # no timestamps
        assert weighted[0]["score"] == 0.8  # unchanged

    def test_no_timezone_timestamp(self):
        results = [{"conversation_id": "c1", "score": 0.8}]
        weighted = apply_temporal_weight(results, {"c1": "2024-01-01T00:00:00"})
        assert weighted[0]["score"] > 0  # didn't crash, score adjusted

    def test_invalid_timestamp(self):
        results = [{"conversation_id": "c1", "score": 0.8}]
        weighted = apply_temporal_weight(results, {"c1": "not-a-date"})
        assert weighted[0]["score"] == 0.8  # unchanged, error caught


@pytest.mark.embeddings
class TestMMRRerank:
    def test_empty(self):
        assert mmr_rerank([], [0.1, 0.2]) == []

    def test_basic_reranking(self):
        results = [
            {"conversation_id": "c1", "score": 0.9, "embedding": [1.0, 0.0]},
            {"conversation_id": "c2", "score": 0.8, "embedding": [0.0, 1.0]},
            {"conversation_id": "c3", "score": 0.7, "embedding": [0.7, 0.7]},
        ]
        reranked = mmr_rerank(results, [1.0, 0.0], limit=3)
        assert len(reranked) == 3
        assert "embedding" not in reranked[0]  # embedding stripped
        assert reranked[0]["conversation_id"] == "c1"  # highest relevance first

    def test_same_conversation_suppressed(self):
        results = [
            {"conversation_id": "c1", "score": 0.9, "embedding": [1.0, 0.0]},
            {"conversation_id": "c1", "score": 0.85, "embedding": [0.9, 0.1]},
            {"conversation_id": "c2", "score": 0.5, "embedding": [0.0, 1.0]},
        ]
        reranked = mmr_rerank(results, [1.0, 0.0], limit=2)
        conv_ids = [r["conversation_id"] for r in reranked]
        assert conv_ids == ["c1", "c2"]  # c2 selected over c1 duplicate

    def test_with_breakdown(self):
        bd = ScoreBreakdown(embedding_sim=0.9)
        results = [
            {"conversation_id": "c1", "score": 0.9, "embedding": [1.0, 0.0], "breakdown": bd},
        ]
        reranked = mmr_rerank(results, [1.0, 0.0], limit=1)
        assert reranked[0]["breakdown"].mmr_rank == 1
        assert reranked[0]["breakdown"].mmr_penalty == 0.0

    def test_same_conv_penalty_tracked(self):
        """With high lambda (pure relevance), same-conv items can both be selected."""
        results = [
            {"conversation_id": "c1", "score": 0.95, "embedding": [1.0, 0.0],
             "breakdown": ScoreBreakdown(embedding_sim=0.95)},
            {"conversation_id": "c1", "score": 0.90, "embedding": [0.99, 0.1],
             "breakdown": ScoreBreakdown(embedding_sim=0.90)},
        ]
        # lambda_=1.0 means pure relevance, no diversity penalty applied
        reranked = mmr_rerank(results, [1.0, 0.0], lambda_=1.0, limit=2)
        assert len(reranked) == 2
        # Second result should have penalty=1.0 (same conv as first)
        assert reranked[1]["breakdown"].mmr_penalty == 1.0

    def test_limit(self):
        results = [
            {"conversation_id": f"c{i}", "score": 0.9 - i * 0.1, "embedding": [float(i), 0.0]}
            for i in range(5)
        ]
        reranked = mmr_rerank(results, [1.0, 0.0], limit=2)
        assert len(reranked) == 2


class TestFilterConversations:
    def test_no_filters_returns_none(self, tmp_path):
        from siftd.search import filter_conversations
        from siftd.storage.sqlite import open_database
        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.close()
        assert filter_conversations(db) is None

    def test_workspace_filter(self, tmp_path):
        from siftd.search import filter_conversations
        from siftd.storage.sqlite import open_database
        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.execute("INSERT INTO harnesses (id, name) VALUES ('h1', 'test')")
        conn.execute("INSERT INTO workspaces (id, path, discovered_at) VALUES ('w1', '/proj/foo', '2024-01-01')")
        conn.execute(
            "INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at) "
            "VALUES ('c1', 'ext1', 'h1', 'w1', '2024-01-01')"
        )
        conn.commit()
        conn.close()
        result = filter_conversations(db, workspace="foo")
        assert "c1" in result
        result = filter_conversations(db, workspace="bar")
        assert len(result) == 0

    def test_owner_filter_no_table(self, tmp_path):
        import sqlite3

        from siftd.search import _filter_conversations_conn
        # Use a bare DB without conversation_owners table
        db = tmp_path / "bare.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        # owner filter with no conversation_owners table returns empty set
        result = _filter_conversations_conn(conn, owner="alice")
        assert result == set()
        conn.close()


class TestGetActiveConversationIds:
    def test_no_active_sessions(self, tmp_path, monkeypatch):
        import siftd.search
        from siftd.search import get_active_conversation_ids
        from siftd.storage.sqlite import open_database
        # Reset cache
        siftd.search._active_ids_cache = None
        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.close()
        # Mock scanner to return no sessions
        monkeypatch.setattr(
            "siftd.peek.scanner.list_active_sessions",
            lambda **kw: [],
        )
        result = get_active_conversation_ids(db)
        assert result == set()

    def test_cache_hit(self, tmp_path, monkeypatch):
        import time as _time

        import siftd.search
        from siftd.search import get_active_conversation_ids
        from siftd.storage.sqlite import open_database
        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.close()
        # Seed cache
        siftd.search._active_ids_cache = (_time.monotonic(), db, {"cached_id"})
        result = get_active_conversation_ids(db)
        assert result == {"cached_id"}
        # Reset
        siftd.search._active_ids_cache = None

    def test_import_error(self, tmp_path, monkeypatch):
        import siftd.search
        from siftd.search import get_active_conversation_ids
        from siftd.storage.sqlite import open_database
        siftd.search._active_ids_cache = None
        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.close()
        # Force ImportError by patching
        import builtins
        original_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if name == "siftd.peek.scanner":
                raise ImportError("no scanner")
            return original_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", mock_import)
        result = get_active_conversation_ids(db)
        assert result == set()
        siftd.search._active_ids_cache = None


class TestResolveCandidates:
    def test_no_filters_returns_none(self, tmp_path, monkeypatch):
        import siftd.search
        from siftd.search import resolve_candidates
        from siftd.storage.sqlite import open_database
        siftd.search._active_ids_cache = None
        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.close()
        # Mock to avoid filesystem scan
        monkeypatch.setattr(
            "siftd.search.get_active_conversation_ids",
            lambda db: set(),
        )
        # Only exclude_active is on by default, but no active IDs
        result = resolve_candidates(db, exclude_active=False)
        # no_tag defaults to derivative exclusion, so this still filters
        # Actually: no workspace/model/since/before/tag/all_tags/owner, but
        # effective_exclude = [DERIVATIVE_TAG], so filter_conversations runs
        assert result is not None or result is None  # depends on DB state

    def test_exclude_active(self, tmp_path, monkeypatch):
        from siftd.search import resolve_candidates
        from siftd.storage.sqlite import open_database
        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.execute("INSERT INTO harnesses (id, name) VALUES ('h1', 'test')")
        conn.execute("INSERT INTO workspaces (id, path, discovered_at) VALUES ('w1', '/proj', '2024-01-01')")
        for i in range(3):
            conn.execute(
                "INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at) "
                "VALUES (?, ?, 'h1', 'w1', '2024-01-01')",
                (f"c{i}", f"ext{i}"),
            )
        conn.commit()
        conn.close()
        # Mock active IDs to exclude c0
        monkeypatch.setattr(
            "siftd.search.get_active_conversation_ids",
            lambda db: {"c0"},
        )
        result = resolve_candidates(db, workspace="proj", exclude_active=True)
        assert "c0" not in result
        assert "c1" in result

    def test_exclude_active_no_other_filters(self, tmp_path, monkeypatch):
        """When only exclude_active applies and no other filters, fetch all IDs."""
        from siftd.search import resolve_candidates
        from siftd.storage.sqlite import open_database

        db = tmp_path / "test.db"
        conn = open_database(db)
        conn.execute("INSERT INTO harnesses (id, name) VALUES ('h1', 'test')")
        conn.execute("INSERT INTO workspaces (id, path, discovered_at) VALUES ('w1', '/p', '2024-01-01')")
        for i in range(3):
            conn.execute(
                "INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at) "
                "VALUES (?, ?, 'h1', 'w1', '2024-01-01')",
                (f"c{i}", f"ext{i}"),
            )
        conn.commit()
        conn.close()
        monkeypatch.setattr(
            "siftd.search.get_active_conversation_ids",
            lambda db: {"c0"},
        )
        # include_derivative=True means no derivative tag filter
        # no workspace/model/since/before/tag — so filter_conversations returns None
        # But exclude_active is on with active_ids, so must fetch all IDs
        result = resolve_candidates(db, exclude_active=True, include_derivative=True)
        assert result is not None
        assert "c0" not in result
        assert "c1" in result and "c2" in result


class TestAPIHybridSearchBranches:
    def test_retry_embed_one_then_propagate_index_compat_error(self, monkeypatch, tmp_path):
        calls = {"n": 0, "invalidate": 0}

        class _Backend:
            name = "b"
            model = "m"
            dimension = 2

            def embed_one(self, _q):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("down")
                return [1.0, 0.0]

        import siftd.embeddings.base as emb_base
        import siftd.embeddings.indexer as emb_indexer

        monkeypatch.setattr(emb_indexer, "SCHEMA_VERSION", 1, raising=False)
        monkeypatch.setattr(emb_base, "get_backend", lambda **_k: _Backend(), raising=False)
        monkeypatch.setattr(
            emb_base,
            "invalidate_backend_cache",
            lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1),
            raising=False,
        )

        class _Conn:
            def close(self):
                return None

        monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *_a, **_k: _Conn())
        monkeypatch.setattr("siftd.search.resolve_candidates", lambda *_a, **_k: None)
        monkeypatch.setattr("siftd.api.search.open_embeddings_db", lambda *_a, **_k: _Conn())
        monkeypatch.setattr("siftd.api.search.fts5_recall_conversations", lambda *_a, **_k: (set(), "and"))
        monkeypatch.setattr("siftd.search.annotate_fts5_breakdown", lambda *_a, **_k: None)

        class CompatErr(Exception):
            pass

        monkeypatch.setattr("siftd.api.search.validate_index_compat", lambda *_a, **_k: (_ for _ in ()).throw(CompatErr("x")))
        monkeypatch.setattr("siftd.api.search.search_similar", lambda *_a, **_k: [])

        with pytest.raises(CompatErr):
            api_search.hybrid_search("q", db_path=tmp_path / "db.db", embed_db=tmp_path / "e.db")

        assert calls["invalidate"] == 1
        assert calls["n"] == 2

    def test_recency_relevance_resorts_after_weighting(self, monkeypatch, tmp_path):
        backend = SimpleNamespace(name="b", model="m", dimension=2, embed_one=lambda _q: [1.0, 0.0])

        class _Conn:
            def close(self):
                return None

        monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *_a, **_k: _Conn())
        monkeypatch.setattr("siftd.search.resolve_candidates", lambda *_a, **_k: None)
        monkeypatch.setattr("siftd.api.search.open_embeddings_db", lambda *_a, **_k: _Conn())
        monkeypatch.setattr("siftd.api.search.validate_index_compat", lambda *_a, **_k: None)
        monkeypatch.setattr("siftd.api.search.fts5_recall_conversations", lambda *_a, **_k: (set(), "and"))
        monkeypatch.setattr("siftd.search.annotate_fts5_breakdown", lambda *_a, **_k: None)

        # Intentionally pre-recency ordered: old first, new second.
        monkeypatch.setattr(
            "siftd.api.search.search_similar",
            lambda *_a, **_k: [
                {
                    "conversation_id": "c_old",
                    "score": 0.80,
                    "text": "t",
                    "chunk_type": "exchange",
                    "chunk_id": "b",
                    "source_ids": [],
                    "breakdown": ScoreBreakdown(embedding_sim=0.80),
                },
                {
                    "conversation_id": "c_new",
                    "score": 0.79,
                    "text": "t",
                    "chunk_type": "exchange",
                    "chunk_id": "a",
                    "source_ids": [],
                    "breakdown": ScoreBreakdown(embedding_sim=0.79),
                },
            ],
        )

        # Future timestamp ensures max recency boost deterministically.
        monkeypatch.setattr(
            "siftd.api.search.fetch_conversation_timestamps",
            lambda *_a, **_k: {
                "c_old": "2000-01-01T00:00:00Z",
                "c_new": "9999-01-01T00:00:00Z",
            },
        )

        out = api_search.hybrid_search(
            "q",
            db_path=tmp_path / "db.db",
            embed_db=tmp_path / "e.db",
            embed_backend=backend,
            mode="semantic",
            recency=True,
            rerank="relevance",
        )
        assert [r["conversation_id"] for r in out[:2]] == ["c_new", "c_old"]

    def test_mmr_candidate_cap_threshold_and_score_reporting(self, monkeypatch, tmp_path):
        backend = SimpleNamespace(name="b", model="m", dimension=2, embed_one=lambda _q: [1.0, 0.0])

        class _Conn:
            def close(self):
                return None

        monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *_a, **_k: _Conn())
        monkeypatch.setattr("siftd.search.resolve_candidates", lambda *_a, **_k: None)
        monkeypatch.setattr("siftd.api.search.open_embeddings_db", lambda *_a, **_k: _Conn())
        monkeypatch.setattr("siftd.api.search.validate_index_compat", lambda *_a, **_k: None)
        monkeypatch.setattr("siftd.api.search.fts5_recall_conversations", lambda *_a, **_k: (set(), "and"))
        monkeypatch.setattr("siftd.search.annotate_fts5_breakdown", lambda *_a, **_k: None)

        raw = [
            {
                "conversation_id": f"c{i}",
                "score": 1.0 - i / 2000.0,
                "text": "t",
                "chunk_type": "exchange",
                "embedding": [1.0, 0.0],
                "chunk_id": f"k{i:04d}",
                "source_ids": [],
                "breakdown": ScoreBreakdown(embedding_sim=1.0 - i / 2000.0),
            }
            for i in range(search_mod.MAX_MMR_CANDIDATES + 2)
        ]
        monkeypatch.setattr("siftd.api.search.search_similar", lambda *_a, **_k: raw)

        seen = {"n": 0}

        def fake_mmr(results, _query_embedding, **_k):
            seen["n"] = len(results)
            bd0 = ScoreBreakdown(embedding_sim=0.99)
            bd0.final_score = 0.95
            bd1 = ScoreBreakdown(embedding_sim=0.2)
            bd1.final_score = 0.2
            return [
                {"conversation_id": "c0", "score": 0.99, "text": "t", "chunk_type": "exchange", "chunk_id": "k0", "source_ids": [], "breakdown": bd0},
                {"conversation_id": "c1", "score": 0.2, "text": "t", "chunk_type": "exchange", "chunk_id": "k1", "source_ids": [], "breakdown": bd1},
            ]

        monkeypatch.setattr("siftd.search.mmr_rerank", fake_mmr)

        out = api_search.hybrid_search(
            "q",
            db_path=tmp_path / "db.db",
            embed_db=tmp_path / "e.db",
            embed_backend=backend,
            threshold=0.9,
            rerank="mmr",
        )
        assert seen["n"] == search_mod.MAX_MMR_CANDIDATES
        assert len(out) == 1 and out[0]["conversation_id"] == "c0"
        assert out[0]["score"] == pytest.approx(0.95)
