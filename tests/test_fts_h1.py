"""Tests for H1: R9 (FTS5 sanitization), H11 (short tokens), H28 (exception narrowing)."""

import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from siftd.storage.fts import (
    Fts5Recall,
    SanitizedFts5Query,
    fts5_recall_details,
    sanitize_fts5_query,
    search_content,
)


class TestSanitizeFts5Query:
    """Unit tests for sanitize_fts5_query covering the R9 spec table."""

    def test_simple_words_and_mode(self):
        r = sanitize_fts5_query("foo bar")
        assert r.fts_query == '"foo" "bar"'
        assert r.tokens == ["foo", "bar"]
        assert r.raw is False

    def test_short_tokens_preserved_h11(self):
        r = sanitize_fts5_query("Go R C")
        assert r.fts_query == '"Go" "R" "C"'
        assert r.tokens == ["Go", "R", "C"]

    def test_not_operator_becomes_quoted_token(self):
        r = sanitize_fts5_query("NOT crash")
        assert r.fts_query == '"NOT" "crash"'
        assert r.tokens == ["NOT", "crash"]

    def test_wildcard_and_unterminated_quote_stripped(self):
        r = sanitize_fts5_query('foo* "bar')
        assert r.fts_query == '"foo" "bar"'
        assert r.tokens == ["foo", "bar"]

    def test_empty_input_returns_none(self):
        r = sanitize_fts5_query("")
        assert r.fts_query is None
        assert r.tokens == []

    def test_punctuation_only_returns_none(self):
        r = sanitize_fts5_query('* " " ! + - ???')
        assert r.fts_query is None
        assert r.tokens == []

    def test_or_operator_join(self):
        r = sanitize_fts5_query("foo bar", operator="or")
        assert r.fts_query == '"foo" OR "bar"'
        assert r.tokens == ["foo", "bar"]

    def test_raw_mode_passthrough(self):
        r = sanitize_fts5_query('foo* NOT bar', raw=True)
        assert r.fts_query == 'foo* NOT bar'
        assert r.tokens == []
        assert r.raw is True

    def test_raw_mode_whitespace_only_returns_none(self):
        r = sanitize_fts5_query("   ", raw=True)
        assert r.fts_query is None

    def test_raw_mode_empty_returns_none(self):
        r = sanitize_fts5_query("", raw=True)
        assert r.fts_query is None

    def test_single_word(self):
        r = sanitize_fts5_query("python")
        assert r.fts_query == '"python"'
        assert r.tokens == ["python"]

    def test_one_letter_token(self):
        r = sanitize_fts5_query("R")
        assert r.fts_query == '"R"'
        assert r.tokens == ["R"]


@pytest.fixture
def fts_conn():
    """In-memory SQLite with content_fts and test content."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE VIRTUAL TABLE content_fts USING fts5(
            text_content, content_id UNINDEXED, side UNINDEXED, conversation_id UNINDEXED,
            tokenize='porter unicode61 remove_diacritics 1'
        )
    """)
    rows = [
        ("Python function error crash debug", "c1", "prompt", "conv1"),
        ("Go language performance benchmark", "c2", "response", "conv2"),
        ("R programming statistics analysis", "c3", "prompt", "conv3"),
    ]
    conn.executemany("INSERT INTO content_fts VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return conn


class TestFts5RecallDetails:
    def test_normal_query_and_mode(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "Python crash", min_and_hits=1)
        assert recall.mode == "and"
        assert "conv1" in recall.conversation_ids

    def test_malformed_input_no_exception(self, fts_conn):
        recall = fts5_recall_details(fts_conn, 'foo* "bar')
        assert isinstance(recall, Fts5Recall)

    def test_not_operator_input_no_exception(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "NOT crash")
        assert isinstance(recall, Fts5Recall)

    def test_empty_input_returns_none_mode(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "")
        assert recall.mode == "none"
        assert recall.fts_query is None

    def test_punctuation_only_returns_none_mode(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "*** ??? ---")
        assert recall.mode == "none"

    def test_short_tokens_preserved_go(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "Go", min_and_hits=1)
        assert "conv2" in recall.conversation_ids

    def test_short_tokens_preserved_r(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "R", min_and_hits=1)
        assert "conv3" in recall.conversation_ids

    def test_or_fallback_default_mode(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "Python crash", min_and_hits=999)
        assert recall.mode in ("or", "none")

    def test_raw_mode_skips_phase2(self, fts_conn):
        with patch("siftd.storage.fts._fts5_conversation_ids_ordered") as mock_ids:
            mock_ids.return_value = []
            recall = fts5_recall_details(fts_conn, "something", raw_fts=True, min_and_hits=1)
            assert mock_ids.call_count == 1  # phase 2 not invoked
            assert recall.mode == "none"

    def test_raw_mode_uses_query_unchanged(self, fts_conn):
        recall = fts5_recall_details(fts_conn, "Python", raw_fts=True, min_and_hits=1)
        assert recall.fts_query == "Python"

    def test_h28_operational_error_caught_returns_none_mode(self, fts_conn):
        with patch("siftd.storage.fts._fts5_conversation_ids_ordered") as mock_ids:
            mock_ids.side_effect = sqlite3.OperationalError("fts5: syntax error near ...")
            recall = fts5_recall_details(fts_conn, "test query")
            assert recall.mode == "none"

    def test_h28_operational_error_logged(self, fts_conn, caplog):
        with patch("siftd.storage.fts._fts5_conversation_ids_ordered") as mock_ids:
            mock_ids.side_effect = sqlite3.OperationalError("bad syntax")
            with caplog.at_level(logging.WARNING, logger="siftd.storage.fts"):
                fts5_recall_details(fts_conn, "test query")
        assert "bad syntax" in caplog.text

    def test_h28_runtime_error_surfaces(self, fts_conn):
        with patch("siftd.storage.fts._fts5_conversation_ids_ordered") as mock_ids:
            mock_ids.side_effect = RuntimeError("programming bug")
            with pytest.raises(RuntimeError, match="programming bug"):
                fts5_recall_details(fts_conn, "test query")


class TestSearchContent:
    def test_basic_search(self, fts_conn):
        results = search_content(fts_conn, "Python")
        assert len(results) > 0
        assert "snippet" in results[0]
        assert "conversation_id" in results[0]

    def test_malformed_input_no_exception(self, fts_conn):
        results = search_content(fts_conn, 'foo* "bar')
        assert isinstance(results, list)

    def test_not_operator_no_exception(self, fts_conn):
        results = search_content(fts_conn, "NOT crash")
        assert isinstance(results, list)

    def test_empty_input_returns_empty_list(self, fts_conn):
        assert search_content(fts_conn, "") == []

    def test_punctuation_only_returns_empty_list(self, fts_conn):
        assert search_content(fts_conn, "*** ???") == []

    def test_raw_fts_passthrough(self, fts_conn):
        results = search_content(fts_conn, "Python", raw_fts=True)
        assert len(results) > 0

    def test_short_token_search_sanitized(self, fts_conn):
        results = search_content(fts_conn, "Go")
        assert len(results) > 0


class TestTryServeH10:
    """H10: unexpected exceptions in try_serve are logged, not silently swallowed."""

    def test_unexpected_exception_logged(self, monkeypatch, tmp_path, caplog):
        from siftd.serve import delegation

        db = tmp_path / "siftd.db"
        db.touch()
        monkeypatch.setattr(
            "siftd.serve.delegation.try_delegate",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        op = SimpleNamespace(
            method="GET",
            path="/api/v1/search",
            params={"db_path": db, "q": "test"},
            db=db,
        )
        with caplog.at_level(logging.WARNING, logger="siftd.serve.delegation"):
            result = delegation.try_serve(op)

        assert result is None
        assert "boom" in caplog.text

    def test_expected_failure_still_returns_none(self, monkeypatch, tmp_path):
        from siftd.serve import delegation

        db = tmp_path / "siftd.db"
        db.touch()
        monkeypatch.setattr(
            "siftd.serve.delegation.try_delegate",
            lambda *a, **k: None,
        )
        op = SimpleNamespace(
            method="GET",
            path="/api/v1/search",
            params={"db_path": db, "q": "test"},
            db=db,
        )
        result = delegation.try_serve(op)
        assert result is None
