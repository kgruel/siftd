"""Tests for siftd.api.inbox — staged receive and inbox processing."""

import json
import threading

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
    return p


class TestStagePayload:
    def test_basic_stage(self, db, inbox, tmp_path):
        slice_path = _make_slice(tmp_path)
        data = slice_path.read_bytes()
        result = stage_payload(slice_path, db)

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
        slice_path = _make_slice(tmp_path)
        result = stage_payload(slice_path, db, source_host="alcove")

        conn = open_database(db)
        row = conn.execute(
            "SELECT source_host FROM sync_inbox WHERE id = ?",
            (result["id"],),
        ).fetchone()
        conn.close()
        assert row[0] == "alcove"


class TestProcessInbox:
    def test_process_staged_payload(self, db, inbox, tmp_path):
        slice_path = _make_slice(tmp_path)
        staged = stage_payload(slice_path, db)

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

    def test_process_claim_skips_already_processed(self, db, inbox, tmp_path):
        from siftd.api.inbox import _process_one

        slice_path = _make_slice(tmp_path)
        staged = stage_payload(slice_path, db)
        payload_path = inbox / f"{staged['id']}.db"

        first = _process_one(db, staged["id"], payload_path)
        assert first["status"] == "done"

        second = _process_one(db, staged["id"], payload_path)
        assert second["status"] == "skipped"

    def test_process_claim_skips_concurrent_contender(self, db, inbox, tmp_path, monkeypatch):
        from siftd.api.inbox import _process_one

        slice_path = _make_slice(tmp_path)
        staged = stage_payload(slice_path, db)
        payload_path = inbox / f"{staged['id']}.db"

        entered_merge = threading.Event()
        release_merge = threading.Event()
        merge_calls = []
        results: list[dict] = []

        def _fake_receive_database(source_path, target_db, rebuild_fts=True):
            merge_calls.append((source_path, target_db, rebuild_fts))
            entered_merge.set()
            assert release_merge.wait(timeout=2), "timed out waiting to release merge"
            return {"conversations": 1}

        monkeypatch.setattr("siftd.api.receive.receive_database", _fake_receive_database)

        def _run_once():
            results.append(_process_one(db, staged["id"], payload_path))

        first = threading.Thread(target=_run_once)
        second = threading.Thread(target=_run_once)

        first.start()
        assert entered_merge.wait(timeout=2), "first processor never reached merge"

        second.start()
        second.join(timeout=2)
        assert not second.is_alive(), "second processor did not finish"

        release_merge.set()
        first.join(timeout=2)
        assert not first.is_alive(), "first processor did not finish"

        statuses = sorted(result["status"] for result in results)
        assert statuses == ["done", "skipped"]
        assert len(merge_calls) == 1

        conn = open_database(db)
        row = conn.execute(
            "SELECT status, processed_at, error, conversations FROM sync_inbox WHERE id = ?",
            (staged["id"],),
        ).fetchone()
        conn.close()

        assert row[0] == "done"
        assert row[1] is not None
        assert row[2] is None
        assert row[3] == 1
        assert not payload_path.exists()

    def test_process_empty_inbox(self, db, inbox):
        results = process_inbox(db)
        assert results == []

    def test_process_missing_file(self, db, inbox, tmp_path):
        slice_path = _make_slice(tmp_path)
        staged = stage_payload(slice_path, db)

        # Delete the staged file
        for f in inbox.glob("*.db"):
            f.unlink()

        results = process_inbox(db)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "missing" in results[0]["error"].lower()

    def test_process_multiple_payloads(self, db, inbox, tmp_path):
        stage_payload(_make_slice(tmp_path), db)
        stage_payload(_make_slice(tmp_path), db)

        results = process_inbox(db)
        assert len(results) == 2
        assert all(r["status"] == "done" for r in results)


class TestGetInboxStatus:
    def test_empty_inbox(self, db, inbox):
        status = get_inbox_status(db)
        assert status["pending"] == 0
        assert status["total"] == 0

    def test_with_staged_payload(self, db, inbox, tmp_path):
        stage_payload(_make_slice(tmp_path), db)

        status = get_inbox_status(db)
        assert status["pending"] == 1
        assert status["total"] == 1
        assert "last" in status
        assert status["last"]["status"] == "staged"

    def test_after_processing(self, db, inbox, tmp_path):
        stage_payload(_make_slice(tmp_path), db)
        process_inbox(db)

        status = get_inbox_status(db)
        assert status["pending"] == 0
        assert status["total"] == 1
        assert status["last"]["status"] == "done"

    def test_nonexistent_db(self, tmp_path):
        status = get_inbox_status(tmp_path / "missing.db")
        assert status == {"pending": 0, "total": 0}
