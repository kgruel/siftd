"""Integration tests for live session tagging flow.

Tests the full workflow: register → queue tag → ingest → verify tag applied.
"""

import pytest

from siftd.domain.models import ContentBlock, Conversation, Harness, Prompt, Response, Usage
from siftd.domain.source import Source
from siftd.ingestion import ingest_all
from siftd.storage.sessions import (
    get_pending_tags,
    get_session_info,
    get_stale_sessions_count,
    is_session_registered,
    queue_tag,
    register_session,
)
from siftd.storage.sqlite import create_database, open_database
from conftest import make_conversation


def make_live_adapter(source_path, conversation):
    """Create a test adapter with SUPPORTS_LIVE_REGISTRATION."""

    class _LiveAdapter:
        ADAPTER_INTERFACE_VERSION = 1
        NAME = "live_test"
        DEFAULT_LOCATIONS = []
        DEDUP_STRATEGY = "file"
        HARNESS_SOURCE = "test"
        HARNESS_LOG_FORMAT = "jsonl"
        SUPPORTS_LIVE_REGISTRATION = True

        @staticmethod
        def can_handle(source):
            return True

        @staticmethod
        def parse(source):
            yield conversation

        @staticmethod
        def discover():
            yield Source(kind="file", location=source_path)

    return _LiveAdapter


def make_non_live_adapter(source_path, conversation):
    """Create a test adapter WITHOUT SUPPORTS_LIVE_REGISTRATION."""

    class _NonLiveAdapter:
        ADAPTER_INTERFACE_VERSION = 1
        NAME = "non_live_test"
        DEFAULT_LOCATIONS = []
        DEDUP_STRATEGY = "file"
        HARNESS_SOURCE = "test"
        HARNESS_LOG_FORMAT = "jsonl"
        # No SUPPORTS_LIVE_REGISTRATION

        @staticmethod
        def can_handle(source):
            return True

        @staticmethod
        def parse(source):
            yield conversation

        @staticmethod
        def discover():
            yield Source(kind="file", location=source_path)

    return _NonLiveAdapter


@pytest.fixture
def live_db(tmp_path):
    """Create a test database."""
    db_path = tmp_path / "live_test.db"
    conn = create_database(db_path)
    return {"path": db_path, "conn": conn}


class TestLiveTaggingFlow:
    """Integration tests for the live tagging workflow."""

    def test_register_queue_ingest_applies_conversation_tag(self, live_db, tmp_path):
        """Full flow: register → queue → ingest → verify conversation tag applied."""
        session_id = "test-session-123"
        tag_name = "decision:auth"

        # Create test file for the adapter
        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        # Create conversation with matching external_id
        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
        )

        # 1. Register the session
        register_session(live_db["conn"], session_id, "live_test", "/test/project", commit=True)
        assert is_session_registered(live_db["conn"], session_id)

        # 2. Queue a tag
        queue_tag(live_db["conn"], session_id, tag_name, commit=True)
        pending = get_pending_tags(live_db["conn"], session_id)
        assert len(pending) == 1

        # 3. Ingest with a live-enabled adapter
        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        # 4. Verify tag was applied
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN conversation_tags ct ON ct.tag_id = t.id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE c.external_id = ?
        """, (session_id,))
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name in tags

        # 5. Verify pending tags consumed
        pending = get_pending_tags(live_db["conn"], session_id)
        assert len(pending) == 0

        # 6. Verify session unregistered
        assert not is_session_registered(live_db["conn"], session_id)

    def test_register_queue_ingest_applies_exchange_tag(self, live_db, tmp_path):
        """Full flow for exchange-level tagging."""
        session_id = "test-session-456"
        tag_name = "key-insight"
        exchange_index = 0

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        # Create conversation with external_id matching session_id
        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
        )

        # 1. Register
        register_session(live_db["conn"], session_id, "live_test", commit=True)

        # 2. Queue exchange tag
        queue_tag(live_db["conn"], session_id, tag_name, entity_type="exchange", exchange_index=exchange_index, commit=True)

        # 3. Ingest
        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        # 4. Verify tag was applied to the prompt
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN prompt_tags pt ON pt.tag_id = t.id
            JOIN prompts p ON p.id = pt.prompt_id
            JOIN conversations c ON c.id = p.conversation_id
            WHERE c.external_id = ?
        """, (session_id,))
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name in tags

    def test_non_live_adapter_ignores_pending_tags(self, live_db, tmp_path):
        """Adapters without SUPPORTS_LIVE_REGISTRATION don't apply pending tags."""
        session_id = "test-session-789"
        tag_name = "should-not-apply"

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
            harness_name="non_live_test",
        )

        # Register and queue
        register_session(live_db["conn"], session_id, "non_live_test", commit=True)
        queue_tag(live_db["conn"], session_id, tag_name, commit=True)

        # Ingest with non-live adapter
        adapter = make_non_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        # Tag should NOT be applied
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN conversation_tags ct ON ct.tag_id = t.id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE c.external_id = ?
        """, (session_id,))
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name not in tags

        # Pending tags should still exist
        pending = get_pending_tags(live_db["conn"], session_id)
        assert len(pending) == 1

    def test_queue_tag_without_register(self, live_db, tmp_path):
        """Tags queued for unregistered sessions are still applied at ingest."""
        session_id = "unregistered-session"
        tag_name = "queued-without-register"

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
        )

        # Queue without registering first
        queue_tag(live_db["conn"], session_id, tag_name, commit=True)

        # Ingest
        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        # Tag should be applied
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN conversation_tags ct ON ct.tag_id = t.id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE c.external_id = ?
        """, (session_id,))
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name in tags

    def test_exchange_index_out_of_range(self, live_db, tmp_path):
        """Exchange tag with invalid index is skipped gracefully."""
        session_id = "test-session-oob"
        tag_name = "out-of-bounds"

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        # Conversation has only 1 prompt (index 0)
        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
        )

        register_session(live_db["conn"], session_id, "live_test", commit=True)
        # Queue tag for exchange index 10 (doesn't exist)
        queue_tag(live_db["conn"], session_id, tag_name, entity_type="exchange", exchange_index=10, commit=True)

        # Ingest
        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        # Tag should NOT be applied (prompt at index 10 doesn't exist)
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN prompt_tags pt ON pt.tag_id = t.id
        """)
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name not in tags

        # Pending tags should be consumed (even though application failed)
        pending = get_pending_tags(live_db["conn"], session_id)
        assert len(pending) == 0

    def test_multiple_tags_single_session(self, live_db, tmp_path):
        """Multiple tags queued for the same session are all applied."""
        session_id = "multi-tag-session"
        tags_to_queue = ["tag1", "tag2", "tag3"]

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
        )

        register_session(live_db["conn"], session_id, "live_test", commit=True)
        for tag in tags_to_queue:
            queue_tag(live_db["conn"], session_id, tag, commit=True)

        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN conversation_tags ct ON ct.tag_id = t.id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE c.external_id = ?
        """, (session_id,))
        applied_tags = [row[0] for row in cur.fetchall()]

        for tag in tags_to_queue:
            assert tag in applied_tags

    def test_namespaced_session_id_matches_adapter_format(self, live_db, tmp_path):
        """Verify namespaced session IDs work end-to-end.

        This test uses the real claude_code adapter's external_id format:
        `claude_code::{raw_session_id}` to ensure the hook and ingest match.

        Previously, the hook registered raw IDs but the adapter namespaced them,
        causing pending tags to never be found at ingest time.
        """
        raw_session_id = "abc123def456"
        namespaced_session_id = f"claude_code::{raw_session_id}"
        tag_name = "decision:architecture"

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        # Conversation external_id uses namespaced format (as real claude_code adapter does)
        conversation = make_conversation(
            external_id=namespaced_session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
            harness_name="claude_code",
            harness_source="anthropic",
        )

        # Register with namespaced ID (as the fixed hook now does)
        register_session(live_db["conn"], namespaced_session_id, "claude_code", "/test/project", commit=True)
        assert is_session_registered(live_db["conn"], namespaced_session_id)

        # Queue tag with namespaced ID
        queue_tag(live_db["conn"], namespaced_session_id, tag_name, commit=True)
        pending = get_pending_tags(live_db["conn"], namespaced_session_id)
        assert len(pending) == 1

        # Ingest with live-enabled adapter
        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        # Verify tag was applied
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN conversation_tags ct ON ct.tag_id = t.id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE c.external_id = ?
        """, (namespaced_session_id,))
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name in tags

        # Verify pending tags consumed
        pending = get_pending_tags(live_db["conn"], namespaced_session_id)
        assert len(pending) == 0

        # Verify session unregistered
        assert not is_session_registered(live_db["conn"], namespaced_session_id)

    def test_reregister_refreshes_last_seen_at(self, live_db):
        """Re-registering a session updates last_seen_at but keeps started_at."""
        session_id = "reregister-session"

        # First registration
        register_session(live_db["conn"], session_id, "live_test", "/project", commit=True)
        info1 = get_session_info(live_db["conn"], session_id)
        assert info1 is not None
        original_started_at = info1["started_at"]
        original_last_seen_at = info1["last_seen_at"]

        # Re-register (simulate hook firing again on resume/compact)
        import time
        time.sleep(0.01)  # Ensure timestamp difference
        register_session(live_db["conn"], session_id, "live_test", "/project", commit=True)

        info2 = get_session_info(live_db["conn"], session_id)
        assert info2 is not None

        # started_at should be unchanged (keeps original session start time)
        assert info2["started_at"] == original_started_at

        # last_seen_at should be updated (session is still active)
        assert info2["last_seen_at"] >= original_last_seen_at

    def test_stale_sessions_use_last_seen_at(self, live_db):
        """Staleness check uses last_seen_at, not started_at."""
        from datetime import datetime, timedelta

        session_id = "stale-check-session"

        # Register session with old started_at
        old_time = (datetime.now() - timedelta(hours=100)).isoformat()
        live_db["conn"].execute(
            """
            INSERT INTO active_sessions (harness_session_id, adapter_name, workspace_path, started_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, "live_test", "/project", old_time, datetime.now().isoformat()),
        )
        live_db["conn"].commit()

        # Session was started 100h ago but last_seen_at is now
        # Should NOT be considered stale (48h threshold)
        stale_count = get_stale_sessions_count(live_db["conn"], max_age_hours=48)
        assert stale_count == 0

        # Now make last_seen_at old too
        very_old_time = (datetime.now() - timedelta(hours=100)).isoformat()
        live_db["conn"].execute(
            "UPDATE active_sessions SET last_seen_at = ? WHERE harness_session_id = ?",
            (very_old_time, session_id),
        )
        live_db["conn"].commit()

        # Now it should be stale
        stale_count = get_stale_sessions_count(live_db["conn"], max_age_hours=48)
        assert stale_count == 1
