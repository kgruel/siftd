"""Tests for siftd.api.backfill."""

from __future__ import annotations

import pytest

from siftd.api import backfill as backfill_api


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_run_backfill_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backfill_api.run_backfill(db_path=tmp_path / "missing.db")


def test_run_backfill_shell_tags(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    db.touch()
    conn = _FakeConn()

    monkeypatch.setattr(backfill_api, "open_database", lambda path: conn)
    monkeypatch.setattr(backfill_api, "backfill_shell_tags", lambda _conn: {"git": 2})

    result = backfill_api.run_backfill(db_path=db, operation="shell_tags")

    assert result.operation == "shell_tags"
    assert result.shell_tag_counts == {"git": 2}
    assert result.elapsed_ms >= 0
    assert conn.closed is True


def test_run_backfill_derivative_tags(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    db.touch()
    conn = _FakeConn()

    monkeypatch.setattr(backfill_api, "open_database", lambda path: conn)
    monkeypatch.setattr(backfill_api, "backfill_derivative_tags", lambda _conn: 4)

    result = backfill_api.run_backfill(db_path=db, operation="derivative_tags")

    assert result.tagged_conversations == 4
    assert conn.closed is True


def test_run_backfill_filter_binary(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    db.touch()
    conn = _FakeConn()

    monkeypatch.setattr(backfill_api, "open_database", lambda path: conn)
    monkeypatch.setattr(
        backfill_api,
        "backfill_filter_binary",
        lambda _conn, dry_run=False: {"filtered": 3, "skipped": 2, "errors": 1},
    )

    result = backfill_api.run_backfill(db_path=db, operation="filter_binary", dry_run=True)

    assert result.dry_run is True
    assert result.filtered == 3
    assert result.skipped == 2
    assert result.errors == 1
    assert conn.closed is True


def test_run_backfill_response_attributes_default(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    db.touch()
    conn = _FakeConn()

    monkeypatch.setattr(backfill_api, "open_database", lambda path: conn)
    monkeypatch.setattr(backfill_api, "backfill_response_attributes", lambda _conn: 7)

    result = backfill_api.run_backfill(db_path=db)

    assert result.operation == "response_attributes"
    assert result.inserted_attributes == 7
    assert conn.closed is True
