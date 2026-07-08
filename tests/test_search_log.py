"""Tests for search-log storage: search_events + search_opens side tables."""

import sqlite3

import pytest

from siftd.storage.search_log import (
    SearchEventFingerprint,
    ensure_search_log_tables,
    find_open_link,
    has_search_log_table,
    record_open,
    record_search,
    recent_searches,
)
from siftd.storage.sqlite import create_database


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)
    try:
        yield conn
    finally:
        conn.close()


class TestEnsureSearchLogTables:
    def test_idempotent(self, db):
        ensure_search_log_tables(db, commit=True)
        ensure_search_log_tables(db, commit=True)  # no error on rerun
        assert has_search_log_table(db)

    def test_created_by_open_database(self, db):
        # create_database() already ran the ensure_* block.
        assert has_search_log_table(db)

    def test_has_search_log_table_false_on_tableless_db(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        try:
            assert has_search_log_table(raw) is False
        finally:
            raw.close()


class TestRecordSearch:
    def test_round_trip(self, db):
        fp = SearchEventFingerprint(
            fp_backend="fastembed", fp_model="bge-small", fp_dimension=384,
            fp_strategy="narrow", fp_preset="fastembed", fp_recall=40,
            fp_mmr_lambda=0.7, fp_mode="hybrid",
        )
        sid = record_search(
            db, query="how does auth work", issuer="cli", fingerprint=fp,
            executed_mode="hybrid", result_ids=["conv-1", "conv-2"], result_count=2,
            owner="", workspace="/proj", session_id="sess-1", commit=True,
        )
        row = db.execute("SELECT * FROM search_events WHERE id = ?", (sid,)).fetchone()
        assert row["query"] == "how does auth work"
        assert row["issuer"] == "cli"
        assert row["fp_backend"] == "fastembed"
        assert row["fp_strategy"] == "narrow"
        assert row["executed_mode"] == "hybrid"
        assert row["result_count"] == 2
        assert row["session_id"] == "sess-1"
        import json

        assert json.loads(row["result_ids"]) == ["conv-1", "conv-2"]

    def test_result_ids_capped_at_max(self, db):
        fp = SearchEventFingerprint()
        ids = [f"conv-{i}" for i in range(100)]
        sid = record_search(
            db, query="q", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=ids, result_count=100, commit=True,
        )
        row = db.execute("SELECT result_ids FROM search_events WHERE id = ?", (sid,)).fetchone()
        import json

        stored = json.loads(row["result_ids"])
        assert len(stored) == 50
        assert stored == ids[:50]

    def test_owner_scoping(self, db):
        fp = SearchEventFingerprint()
        record_search(
            db, query="q1", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=[], result_count=0, owner="alice", commit=True,
        )
        record_search(
            db, query="q2", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=[], result_count=0, owner="bob", commit=True,
        )
        alice_rows = recent_searches(db, owner="alice", limit=10)
        assert len(alice_rows) == 1
        assert alice_rows[0]["query"] == "q1"


class TestRecentSearches:
    def test_orders_most_recent_first(self, db):
        fp = SearchEventFingerprint()
        record_search(
            db, query="first", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=[], result_count=0, commit=True,
        )
        record_search(
            db, query="second", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=[], result_count=0, commit=True,
        )
        rows = recent_searches(db, limit=10)
        assert [r["query"] for r in rows] == ["second", "first"]

    def test_no_table_returns_empty(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        try:
            assert recent_searches(raw) == []
        finally:
            raw.close()


class TestRecordOpen:
    def test_round_trip(self, db):
        fp = SearchEventFingerprint()
        sid = record_search(
            db, query="q", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=["conv-a", "conv-b"], result_count=2, commit=True,
        )
        open_id = record_open(
            db, search_event_id=sid, conversation_id="conv-b", rank=2,
            surface="cli-heuristic", commit=True,
        )
        row = db.execute("SELECT * FROM search_opens WHERE id = ?", (open_id,)).fetchone()
        assert row["search_event_id"] == sid
        assert row["conversation_id"] == "conv-b"
        assert row["rank"] == 2
        assert row["surface"] == "cli-heuristic"


class TestFindOpenLink:
    def test_binds_via_session_id(self, db):
        fp = SearchEventFingerprint()
        sid = record_search(
            db, query="q", issuer="agent", fingerprint=fp, executed_mode="fts",
            result_ids=["conv-a", "conv-b"], result_count=2, session_id="sess-1", commit=True,
        )
        link = find_open_link(db, conversation_id="conv-b", session_id="sess-1")
        assert link == (sid, 2)

    def test_binds_via_time_window_without_session(self, db):
        fp = SearchEventFingerprint()
        sid = record_search(
            db, query="q", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=["conv-a"], result_count=1, commit=True,
        )
        link = find_open_link(db, conversation_id="conv-a", session_id=None)
        assert link == (sid, 1)

    def test_unrelated_id_not_in_results_returns_none(self, db):
        fp = SearchEventFingerprint()
        record_search(
            db, query="q", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=["conv-a"], result_count=1, commit=True,
        )
        assert find_open_link(db, conversation_id="conv-zzz", session_id=None) is None

    def test_most_recent_matching_search_wins(self, db):
        fp = SearchEventFingerprint()
        record_search(
            db, query="older", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=["conv-a"], result_count=1, commit=True,
        )
        sid2 = record_search(
            db, query="newer", issuer="cli", fingerprint=fp, executed_mode="fts",
            result_ids=["conv-a"], result_count=1, commit=True,
        )
        link = find_open_link(db, conversation_id="conv-a", session_id=None)
        assert link == (sid2, 1)

    def test_no_table_returns_none(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        try:
            assert find_open_link(raw, conversation_id="x") is None
        finally:
            raw.close()
