"""Tests for siftd.api.inbox — staged receive and inbox processing."""

import json

import pytest

from siftd.api.inbox import get_inbox_status, process_inbox, stage_payload
from siftd.storage.sqlite import open_database


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "test.db"
    open_database(p).close()
    return p


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    inbox_path = tmp_path / "inbox"
    inbox_path.mkdir()
    monkeypatch.setattr("siftd.paths.inbox_dir", lambda: inbox_path)
    return inbox_path


def _make_slice(tmp_path):
    """Create a minimal valid slice database (empty, with schema)."""
    from siftd.storage.sqlite import create_empty_database

    p = tmp_path / "slice.db"
    create_empty_database(p)
    return p.read_bytes()


class TestStagePayload:
    def test_basic_stage(self, db, inbox, tmp_path):
        data = _make_slice(tmp_path)
        result = stage_payload(data, db)

        assert result["status"] == "staged"
        assert "id" in result

        # Verify the payload file was written
        payload_files = list(inbox.glob("*.db"))
        assert len(payload_files) == 1
        assert payload_files[0].read_bytes() == data

        # Verify the inbox row was created
        conn = open_database(db)
        row = conn.execute(
            "SELECT status, size_bytes FROM sync_inbox WHERE id = ?",
            (result["id"],),
        ).fetchone()
        conn.close()
        assert row[0] == "staged"
        assert row[1] == len(data)

    def test_stage_with_source_host(self, db, inbox, tmp_path):
        data = _make_slice(tmp_path)
        result = stage_payload(data, db, source_host="alcove")

        conn = open_database(db)
        row = conn.execute(
            "SELECT source_host FROM sync_inbox WHERE id = ?",
            (result["id"],),
        ).fetchone()
        conn.close()
        assert row[0] == "alcove"


class TestProcessInbox:
    def test_process_staged_payload(self, db, inbox, tmp_path):
        data = _make_slice(tmp_path)
        staged = stage_payload(data, db)

        results = process_inbox(db)
        assert len(results) == 1
        assert results[0]["status"] == "done"
        assert results[0]["id"] == staged["id"]

        # Verify the staged file was cleaned up
        assert not list(inbox.glob("*.db"))

        # Verify the inbox row was updated
        conn = open_database(db)
        row = conn.execute(
            "SELECT status, processed_at FROM sync_inbox WHERE id = ?",
            (staged["id"],),
        ).fetchone()
        conn.close()
        assert row[0] == "done"
        assert row[1] is not None

    def test_process_empty_inbox(self, db, inbox):
        results = process_inbox(db)
        assert results == []

    def test_process_missing_file(self, db, inbox, tmp_path):
        data = _make_slice(tmp_path)
        staged = stage_payload(data, db)

        # Delete the staged file
        for f in inbox.glob("*.db"):
            f.unlink()

        results = process_inbox(db)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "missing" in results[0]["error"].lower()

    def test_process_multiple_payloads(self, db, inbox, tmp_path):
        data = _make_slice(tmp_path)
        stage_payload(data, db)
        stage_payload(data, db)

        results = process_inbox(db)
        assert len(results) == 2
        assert all(r["status"] == "done" for r in results)


class TestGetInboxStatus:
    def test_empty_inbox(self, db, inbox):
        status = get_inbox_status(db)
        assert status["pending"] == 0
        assert status["total"] == 0

    def test_with_staged_payload(self, db, inbox, tmp_path):
        data = _make_slice(tmp_path)
        stage_payload(data, db)

        status = get_inbox_status(db)
        assert status["pending"] == 1
        assert status["total"] == 1
        assert "last" in status
        assert status["last"]["status"] == "staged"

    def test_after_processing(self, db, inbox, tmp_path):
        data = _make_slice(tmp_path)
        stage_payload(data, db)
        process_inbox(db)

        status = get_inbox_status(db)
        assert status["pending"] == 0
        assert status["total"] == 1
        assert status["last"]["status"] == "done"

    def test_nonexistent_db(self, tmp_path):
        status = get_inbox_status(tmp_path / "missing.db")
        assert status == {"pending": 0, "total": 0}
