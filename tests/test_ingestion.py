"""Tests for ingestion orchestration.

Covers:
- Hash-based change detection for file dedup
- Empty file handling
- Session-based dedup with timestamp comparison
"""

from pathlib import Path

from conftest import FIXTURES_DIR, make_conversation, make_session_adapter

from siftd.ingestion.orchestration import ingest_all
from siftd.storage.sqlite import (
    check_file_ingested,
    compute_file_hash,
    get_ingested_file_info,
    open_database,
)
from siftd.adapters import claude_code
from siftd.domain import Source


def _make_adapter(dest, name="claude_code", dedup="file", parse_fn=None):
    """Factory for test adapters."""
    class TestAdapter:
        NAME = name
        DEDUP_STRATEGY = dedup
        HARNESS_SOURCE = "anthropic"

        @staticmethod
        def can_handle(source):
            return claude_code.can_handle(source)

        @staticmethod
        def parse(source):
            if parse_fn:
                return parse_fn(source)
            return claude_code.parse(source)

        @staticmethod
        def discover():
            yield Source(kind="file", location=dest)

    return TestAdapter


class TestHashBasedDedup:
    """Tests for hash-based file change detection."""

    def test_new_file_is_ingested(self, tmp_path):
        """New file gets ingested and recorded."""
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        stats = ingest_all(conn, [_make_adapter(dest)])

        assert stats.files_ingested == 1
        assert stats.files_skipped == 0
        assert stats.files_replaced == 0
        assert check_file_ingested(conn, str(dest))

        conn.close()

    def test_unchanged_file_is_skipped(self, tmp_path):
        """File with same hash is skipped on second ingest."""
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        adapter = _make_adapter(dest)

        stats1 = ingest_all(conn, [adapter])
        assert stats1.files_ingested == 1

        stats2 = ingest_all(conn, [adapter])
        assert stats2.files_ingested == 0
        assert stats2.files_skipped == 1
        assert stats2.files_replaced == 0

        conn.close()

    def test_changed_file_is_reingested(self, tmp_path):
        """File with different hash triggers re-ingest."""
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        adapter = _make_adapter(dest)

        stats1 = ingest_all(conn, [adapter])
        assert stats1.files_ingested == 1

        original_info = get_ingested_file_info(conn, str(dest))
        original_hash = original_info["file_hash"]
        original_conv_id = original_info["conversation_id"]

        # Modify file (append new content - simulating JSONL append)
        with open(dest, "a") as f:
            f.write('{"type": "user", "sessionId": "test-session-1", "timestamp": "2024-01-15T10:00:10Z", "uuid": "msg-005", "message": {"role": "user", "content": [{"type": "text", "text": "Another question"}]}}\n')

        stats2 = ingest_all(conn, [adapter])
        assert stats2.files_ingested == 0
        assert stats2.files_skipped == 0
        assert stats2.files_replaced == 1

        # Verify hash was updated
        new_info = get_ingested_file_info(conn, str(dest))
        assert new_info["file_hash"] != original_hash
        assert new_info["file_hash"] == compute_file_hash(dest)

        # Verify old conversation was replaced
        assert new_info["conversation_id"] != original_conv_id

        # Verify old conversation no longer exists
        cur = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?",
            (original_conv_id,)
        )
        assert cur.fetchone() is None

        conn.close()

    def test_status_callback_reports_updated(self, tmp_path):
        """Status callback receives 'updated' for changed files."""
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        statuses = []

        def on_file(source, status):
            statuses.append(status)

        adapter = _make_adapter(dest)

        ingest_all(conn, [adapter], on_file=on_file)
        assert statuses == ["ingested"]

        statuses.clear()
        with open(dest, "a") as f:
            f.write('{"type": "user", "sessionId": "test-session-1", "timestamp": "2024-01-15T10:00:10Z", "uuid": "msg-005", "message": {"role": "user", "content": [{"type": "text", "text": "New content"}]}}\n')

        ingest_all(conn, [adapter], on_file=on_file)
        assert statuses == ["updated"]

        conn.close()

    def test_multiple_ingests_with_changes(self, tmp_path):
        """Multiple rounds of ingestion with file changes."""
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        adapter = _make_adapter(dest)

        # Ingest 1: new file
        stats = ingest_all(conn, [adapter])
        assert stats.files_ingested == 1

        # Ingest 2: no change
        stats = ingest_all(conn, [adapter])
        assert stats.files_skipped == 1

        # Modify and ingest 3: changed
        with open(dest, "a") as f:
            f.write('{"type": "user", "sessionId": "test-session-1", "timestamp": "2024-01-15T10:00:10Z", "uuid": "msg-005", "message": {"role": "user", "content": [{"type": "text", "text": "Change 1"}]}}\n')
        stats = ingest_all(conn, [adapter])
        assert stats.files_replaced == 1

        # Ingest 4: no change
        stats = ingest_all(conn, [adapter])
        assert stats.files_skipped == 1

        # Modify and ingest 5: changed again
        with open(dest, "a") as f:
            f.write('{"type": "user", "sessionId": "test-session-1", "timestamp": "2024-01-15T10:00:20Z", "uuid": "msg-006", "message": {"role": "user", "content": [{"type": "text", "text": "Change 2"}]}}\n')
        stats = ingest_all(conn, [adapter])
        assert stats.files_replaced == 1

        # Should have exactly one conversation in DB
        cur = conn.execute("SELECT COUNT(*) FROM conversations")
        assert cur.fetchone()[0] == 1

        conn.close()


class TestEmptyFileHandling:
    """Tests for empty file tracking."""

    def test_empty_file_tracked_with_null_conversation(self, tmp_path):
        """Empty files are tracked in ingested_files with conversation_id=NULL."""
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text("")  # Empty file

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        # Adapter that yields no conversations for empty file
        def empty_parse(source):
            return []

        adapter = _make_adapter(dest, parse_fn=empty_parse)

        statuses = []
        def on_file(source, status):
            statuses.append(status)

        stats = ingest_all(conn, [adapter], on_file=on_file)

        # File should be tracked as ingested
        assert check_file_ingested(conn, str(dest))

        # But with NULL conversation
        info = get_ingested_file_info(conn, str(dest))
        assert info is not None
        assert info["conversation_id"] is None

        conn.close()

    def test_empty_file_skipped_on_reingest_if_unchanged(self, tmp_path):
        """Empty file with same hash is skipped on second ingest."""
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text("")

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        def empty_parse(source):
            return []

        adapter = _make_adapter(dest, parse_fn=empty_parse)

        stats1 = ingest_all(conn, [adapter])
        stats2 = ingest_all(conn, [adapter])

        assert stats2.files_skipped == 1
        assert stats2.files_ingested == 0

        conn.close()


class TestSessionBasedDedup:
    """Tests for session-based dedup with timestamp comparison."""

    def _conv(self, external_id, ended_at, workspace="/test"):
        """Create a minimal conversation for testing."""
        return make_conversation(
            external_id=external_id,
            ended_at=ended_at,
            workspace_path=workspace,
        )

    def test_newer_conversation_replaces_older(self, tmp_path):
        """Session with newer ended_at replaces existing conversation."""
        dest = tmp_path / "session.jsonl"
        dest.write_text("dummy")

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        older_conv = self._conv("test_harness::session-1", "2024-01-01T10:00:00Z")
        OlderAdapter = make_session_adapter(dest, parse_fn=lambda s: [older_conv])

        stats1 = ingest_all(conn, [OlderAdapter])
        assert stats1.files_ingested == 1

        cur = conn.execute("SELECT COUNT(*) FROM conversations")
        assert cur.fetchone()[0] == 1

        newer_conv = self._conv("test_harness::session-1", "2024-01-01T12:00:00Z")
        NewerAdapter = make_session_adapter(dest, parse_fn=lambda s: [newer_conv])

        stats2 = ingest_all(conn, [NewerAdapter])
        assert stats2.files_replaced == 1

        cur = conn.execute("SELECT COUNT(*) FROM conversations")
        assert cur.fetchone()[0] == 1

        cur = conn.execute("SELECT ended_at FROM conversations")
        assert cur.fetchone()[0] == "2024-01-01T12:00:00Z"

        conn.close()

    def test_older_conversation_skipped(self, tmp_path):
        """Session with older ended_at is skipped."""
        dest = tmp_path / "session.jsonl"
        dest.write_text("dummy")

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        newer_conv = self._conv("test_harness::session-1", "2024-01-01T12:00:00Z")
        NewerAdapter = make_session_adapter(dest, parse_fn=lambda s: [newer_conv])

        stats1 = ingest_all(conn, [NewerAdapter])
        assert stats1.files_ingested == 1

        older_conv = self._conv("test_harness::session-1", "2024-01-01T10:00:00Z")
        OlderAdapter = make_session_adapter(dest, parse_fn=lambda s: [older_conv])

        stats2 = ingest_all(conn, [OlderAdapter])
        assert stats2.files_skipped == 1
        assert stats2.files_replaced == 0

        cur = conn.execute("SELECT ended_at FROM conversations")
        assert cur.fetchone()[0] == "2024-01-01T12:00:00Z"

        conn.close()

    def test_same_timestamp_skipped(self, tmp_path):
        """Session with same ended_at is skipped (not replaced)."""
        dest = tmp_path / "session.jsonl"
        dest.write_text("dummy")

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        conv = self._conv("test_harness::session-1", "2024-01-01T10:00:00Z")
        Adapter = make_session_adapter(dest, parse_fn=lambda s: [conv])

        stats1 = ingest_all(conn, [Adapter])
        assert stats1.files_ingested == 1

        cur = conn.execute("SELECT id FROM conversations")
        original_id = cur.fetchone()[0]

        stats2 = ingest_all(conn, [Adapter])
        assert stats2.files_skipped == 1

        cur = conn.execute("SELECT id FROM conversations")
        assert cur.fetchone()[0] == original_id

        conn.close()
