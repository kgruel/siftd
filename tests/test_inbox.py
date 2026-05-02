"""Tests for siftd.api.inbox — staged receive and inbox processing."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from siftd.api.inbox import (
    STALE_PROCESSING_MINUTES,
    get_inbox_status,
    process_inbox,
    stage_payload,
)
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


class TestStaleProcessingReclaim:
    """C2: Rows stuck in 'processing' are reclaimed after timeout."""

    def test_stale_processing_row_is_reclaimed(self, db, inbox, tmp_path):
        """A row stuck in 'processing' past the timeout gets reclaimed and reprocessed."""
        slice_path = _make_slice(tmp_path)
        staged = stage_payload(slice_path, db)

        # Manually set status to 'processing' with a stale timestamp
        stale_time = (
            datetime.now(UTC) - timedelta(minutes=STALE_PROCESSING_MINUTES + 1)
        ).isoformat()
        conn = open_database(db)
        conn.execute(
            """UPDATE sync_inbox
               SET status = 'processing', processing_started_at = ?
               WHERE id = ?""",
            (stale_time, staged["id"]),
        )
        conn.commit()
        conn.close()

        # process_inbox should reclaim and reprocess it
        results = process_inbox(db)
        assert len(results) == 1
        assert results[0]["status"] == "done"
        assert results[0]["id"] == staged["id"]

    def test_recent_processing_row_not_reclaimed(self, db, inbox, tmp_path):
        """A row recently claimed for processing is NOT reclaimed."""
        slice_path = _make_slice(tmp_path)
        staged = stage_payload(slice_path, db)

        # Set status to 'processing' with a recent timestamp
        recent_time = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
        conn = open_database(db)
        conn.execute(
            """UPDATE sync_inbox
               SET status = 'processing', processing_started_at = ?
               WHERE id = ?""",
            (recent_time, staged["id"]),
        )
        conn.commit()
        conn.close()

        # process_inbox should NOT reclaim it (still within timeout)
        results = process_inbox(db)
        assert results == []

    def test_processing_without_timestamp_not_reclaimed(self, db, inbox, tmp_path):
        """A 'processing' row without processing_started_at is left alone.

        This covers pre-migration rows that lack the column value.
        """
        slice_path = _make_slice(tmp_path)
        staged = stage_payload(slice_path, db)

        conn = open_database(db)
        conn.execute(
            """UPDATE sync_inbox
               SET status = 'processing', processing_started_at = NULL
               WHERE id = ?""",
            (staged["id"],),
        )
        conn.commit()
        conn.close()

        results = process_inbox(db)
        assert results == []


class TestPreflightGate:
    def test_inbox_preflight_rejects_corrupt_payload(self, db, inbox, tmp_path):
        """Corrupt payload (FK violation) lands in status=error; staged file preserved; not retried."""
        import sqlite3

        from siftd.ids import ulid
        from siftd.storage.sqlite import create_empty_database

        corrupt = tmp_path / "corrupt.db"
        create_empty_database(corrupt)
        conn = sqlite3.connect(str(corrupt))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO prompts (id, conversation_id, external_id, timestamp) "
            "VALUES (?, 'nonexistent-conv', 'ext-p-1', '2024-01-01T00:00:00Z')",
            (ulid(),),
        )
        conn.commit()
        conn.close()

        staged = stage_payload(corrupt, db)
        payload_file = inbox / f"{staged['id']}.db"
        assert payload_file.exists()

        results = process_inbox(db)
        assert len(results) == 1
        assert results[0]["status"] == "error"
        assert "integrity" in results[0]["error"].lower() or "FK" in results[0]["error"]

        # Staged file is preserved on error (only cleaned up on success)
        assert payload_file.exists()

        # status='error' is permanently quarantined — not retried
        results2 = process_inbox(db)
        assert results2 == []
