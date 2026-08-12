"""Unit tests for siftd.api.database preflight functions."""

import sqlite3

import pytest

from siftd.api.database import PreflightError, audit_db_integrity, run_preflight
from siftd.ids import ulid
from siftd.storage.sqlite import create_empty_database, remove_database


def _make_clean_db(tmp_path, name="test.db"):
    p = tmp_path / name
    create_empty_database(p)
    return p


def _make_fk_corrupt_db(tmp_path, name="corrupt.db"):
    """Create a DB with a FK violation (event with non-existent conversation_id).

    Requires PRAGMA foreign_keys = OFF to insert the dangling row; otherwise
    SQLite rejects the INSERT before foreign_key_check can detect it.
    """
    p = tmp_path / name
    create_empty_database(p)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO events (id, kind, conversation_id, external_id, timestamp) "
        "VALUES (?, 'prompt', 'nonexistent-conv', 'ext-p-1', '2024-01-01T00:00:00Z')",
        (ulid(),),
    )
    conn.commit()
    conn.close()
    return p


def _make_no_triggers_db(tmp_path, name="notriggers.db"):
    """Create a DB with blob ref-count triggers dropped."""
    p = tmp_path / name
    create_empty_database(p)
    conn = sqlite3.connect(str(p))
    conn.execute("DROP TRIGGER IF EXISTS tr_event_tool_call_delete_release_blob")
    conn.execute("DROP TRIGGER IF EXISTS tr_event_tool_call_update_release_blob")
    conn.commit()
    conn.close()
    return p


class TestAuditDbIntegrity:
    def test_healthy_db_no_findings(self, tmp_path):
        p = _make_clean_db(tmp_path)
        findings = audit_db_integrity(p)
        assert findings == []

    def test_fk_violation_detected(self, tmp_path):
        p = _make_fk_corrupt_db(tmp_path)
        findings = audit_db_integrity(p)
        assert any(f.check == "db-fk-integrity" for f in findings)
        assert any(f.severity == "error" for f in findings)

    def test_missing_trigger_detected(self, tmp_path):
        p = _make_no_triggers_db(tmp_path)
        findings = audit_db_integrity(p)
        assert any(f.check == "db-trigger-presence" for f in findings)
        assert any(f.severity == "error" for f in findings)

    def test_refcount_drift_excluded_from_checks(self, tmp_path):
        """db-blob-refcount-drift is not in the preflight set — no false alarm on slices."""
        p = _make_clean_db(tmp_path)
        conn = sqlite3.connect(str(p))
        conn.execute(
            "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
            "VALUES ('abc123abc123', 'data', 99, '2024-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        findings = audit_db_integrity(p)
        assert not any(f.check == "db-blob-refcount-drift" for f in findings)


class TestEphemeralPayloadCleanup:
    def test_auditing_then_removing_a_payload_leaves_nothing(self, tmp_path):
        """The whole point: audit a staged payload, drop it, leave no litter.

        Doctor's read connections do change detection, so auditing a WAL
        database creates ``-wal``/``-shm`` beside it and a read-only connection
        cannot remove them on close. Callers that unlink only the ``.db``
        orphaned a 32 KB ``-shm`` per `db receive`, per sync pull, per push.
        """
        p = _make_clean_db(tmp_path)
        # WAL is the only journal mode with sidecars to leak, and it is what a
        # real payload arrives in — create_empty_database leaves DELETE mode,
        # under which this test would pass no matter what the code does.
        writer = sqlite3.connect(p)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE payload (x INTEGER)")
        writer.commit()
        writer.close()

        audit_db_integrity(p)
        assert (tmp_path / f"{p.name}-shm").exists(), "audit should have made sidecars"

        remove_database(p)

        assert list(tmp_path.iterdir()) == []

    def test_remove_database_is_a_noop_on_a_missing_file(self, tmp_path):
        """Callers run it in a finally block, before the payload may exist."""
        remove_database(tmp_path / "never-created.db")


class TestRunPreflight:
    def test_healthy_passes_silently(self, tmp_path):
        p = _make_clean_db(tmp_path)
        run_preflight(p)  # must not raise

    def test_fk_violation_raises_preflight_error(self, tmp_path):
        p = _make_fk_corrupt_db(tmp_path)
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(p)
        msg = str(exc_info.value)
        assert "integrity checks" in msg
        assert "FK violation" in msg
        assert str(p) in msg

    def test_missing_trigger_raises_preflight_error(self, tmp_path):
        p = _make_no_triggers_db(tmp_path)
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(p)
        msg = str(exc_info.value)
        assert "integrity checks" in msg
        assert "trigger" in msg.lower()
        assert str(p) in msg

    def test_refcount_drift_does_not_raise(self, tmp_path):
        """Drift is not gated — a valid slice payload should not raise."""
        p = _make_clean_db(tmp_path)
        conn = sqlite3.connect(str(p))
        conn.execute(
            "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
            "VALUES ('abc123abc123', 'data', 99, '2024-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        run_preflight(p)  # must not raise

    def test_error_message_includes_source_path(self, tmp_path):
        p = _make_fk_corrupt_db(tmp_path)
        with pytest.raises(PreflightError, match="Source:"):
            run_preflight(p)

    def test_custom_label_appears_in_message(self, tmp_path):
        p = _make_fk_corrupt_db(tmp_path)
        with pytest.raises(PreflightError, match="my-label"):
            run_preflight(p, label="my-label")
