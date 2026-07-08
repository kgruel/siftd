"""Search-log capture at the api/search.py::search_view Operation (S2)."""

import sqlite3

import pytest

from siftd.api.search import search_view
from siftd.storage.search_log import has_search_log_table
from siftd.storage.sqlite import open_database


def _events(test_db):
    conn = open_database(test_db, read_only=True)
    try:
        assert has_search_log_table(conn)
        return conn.execute("SELECT * FROM search_events ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _fts_ready(test_db):
    """These tests need FTS-searchable content — test_db's fixture data isn't
    guaranteed to be indexed yet."""
    from siftd.storage.fts import rebuild_fts_index

    conn = open_database(test_db, read_only=False)
    try:
        rebuild_fts_index(conn)
        conn.commit()
    finally:
        conn.close()


class TestCaptureOnFtsSearch:
    def test_records_one_row_with_fingerprint(self, test_db):
        sv = search_view("Python", db_path=test_db, mode="fts")
        assert sv.results  # sanity: the search actually found something

        rows = _events(test_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["query"] == "Python"
        assert row["fp_mode"] == "fts"
        assert row["executed_mode"] == "fts"
        assert row["fp_strategy"] == "fts"
        assert row["fp_backend"] is None
        assert row["result_count"] == len(sv.results)
        assert row["issuer"] in ("cli", "agent")  # session-registration-derived (OJ-7)

    def test_empty_query_facet_search_records_nothing(self, test_db):
        # No query text, no tag facet → falls through the engine as usual, but
        # with q='' there is nothing for the capture path to key on (OJ-6).
        search_view("", db_path=test_db, mode="fts")
        assert _events(test_db) == []

    def test_zero_results_still_captured(self, test_db):
        search_view("zzznonexistentqueryzzz", db_path=test_db, mode="fts")
        rows = _events(test_db)
        assert len(rows) == 1
        assert rows[0]["result_count"] == 0

    def test_opt_out_disables_capture(self, test_db, monkeypatch):
        monkeypatch.setattr("siftd.config.get_search_log_enabled", lambda: False)
        search_view("Python", db_path=test_db, mode="fts")
        assert _events(test_db) == []

    def test_capture_failure_never_fails_the_search(self, test_db, monkeypatch):
        def _boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("siftd.storage.search_log.record_search", _boom)
        sv = search_view("Python", db_path=test_db, mode="fts")
        assert sv.results  # the search itself still succeeded
        assert _events(test_db) == []

    def test_web_issuer_override(self, test_db):
        search_view("Python", db_path=test_db, mode="fts", issuer="web")
        rows = _events(test_db)
        assert rows[0]["issuer"] == "web"

    def test_env_issuer_overrides_everything(self, test_db, monkeypatch):
        monkeypatch.setenv("SIFTD_ISSUER", "agent")
        search_view("Python", db_path=test_db, mode="fts", issuer="web")
        rows = _events(test_db)
        assert rows[0]["issuer"] == "agent"
