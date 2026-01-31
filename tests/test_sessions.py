"""Tests for live session tracking and pending tag storage."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from siftd.storage.sessions import (
    PendingTag,
    cleanup_stale_sessions,
    consume_pending_tags,
    ensure_prompt_tags_table,
    ensure_session_tables,
    get_orphaned_pending_tags_count,
    get_pending_tags,
    get_session_info,
    get_stale_sessions_count,
    is_session_registered,
    queue_tag,
    register_session,
    unregister_session,
)
from siftd.storage.sqlite import create_database


@pytest.fixture
def db(tmp_path):
    """Create a test database with session tables."""
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)
    return conn


class TestRegisterSession:
    """Tests for register_session()."""

    def test_register_new_session(self, db):
        """Registering a new session creates an entry."""
        sid = register_session(db, "session-123", "claude_code", "/path/to/project", commit=True)

        assert sid == "session-123"
        assert is_session_registered(db, "session-123")

    def test_register_session_upserts(self, db):
        """Registering same session twice updates existing entry."""
        register_session(db, "session-123", "claude_code", "/path/v1", commit=True)
        register_session(db, "session-123", "claude_code", "/path/v2", commit=True)

        # Should still have only one session
        count = db.execute("SELECT COUNT(*) FROM active_sessions").fetchone()[0]
        assert count == 1

        # Should have updated path
        info = get_session_info(db, "session-123")
        assert info["workspace_path"] == "/path/v2"

    def test_register_session_without_workspace(self, db):
        """Registering a session without workspace is allowed."""
        register_session(db, "session-123", "claude_code", commit=True)

        info = get_session_info(db, "session-123")
        assert info["workspace_path"] is None


class TestUnregisterSession:
    """Tests for unregister_session()."""

    def test_unregister_existing_session(self, db):
        """Unregistering an existing session returns True."""
        register_session(db, "session-123", "claude_code", commit=True)

        result = unregister_session(db, "session-123", commit=True)

        assert result is True
        assert not is_session_registered(db, "session-123")

    def test_unregister_nonexistent_session(self, db):
        """Unregistering a nonexistent session returns False."""
        result = unregister_session(db, "session-456", commit=True)

        assert result is False


class TestQueueTag:
    """Tests for queue_tag()."""

    def test_queue_conversation_tag(self, db):
        """Queueing a conversation tag creates an entry."""
        register_session(db, "session-123", "claude_code", commit=True)

        result = queue_tag(db, "session-123", "decision:auth", commit=True)

        assert result is not None  # Returns ULID
        tags = get_pending_tags(db, "session-123")
        assert len(tags) == 1
        assert tags[0].tag_name == "decision:auth"
        assert tags[0].entity_type == "conversation"
        assert tags[0].exchange_index is None

    def test_queue_exchange_tag(self, db):
        """Queueing an exchange tag includes the index."""
        register_session(db, "session-123", "claude_code", commit=True)

        result = queue_tag(db, "session-123", "key-insight", entity_type="exchange", exchange_index=5, commit=True)

        assert result is not None
        tags = get_pending_tags(db, "session-123")
        assert len(tags) == 1
        assert tags[0].tag_name == "key-insight"
        assert tags[0].entity_type == "exchange"
        assert tags[0].exchange_index == 5

    def test_queue_duplicate_tag_returns_none(self, db):
        """Queueing the same tag twice returns None on second call."""
        register_session(db, "session-123", "claude_code", commit=True)

        result1 = queue_tag(db, "session-123", "decision:auth", commit=True)
        result2 = queue_tag(db, "session-123", "decision:auth", commit=True)

        assert result1 is not None
        assert result2 is None

        # Should still have only one tag
        tags = get_pending_tags(db, "session-123")
        assert len(tags) == 1

    def test_queue_same_tag_different_entity_types(self, db):
        """Same tag can be queued for both conversation and exchange."""
        register_session(db, "session-123", "claude_code", commit=True)

        result1 = queue_tag(db, "session-123", "important", entity_type="conversation", commit=True)
        result2 = queue_tag(db, "session-123", "important", entity_type="exchange", exchange_index=0, commit=True)

        assert result1 is not None
        assert result2 is not None

        tags = get_pending_tags(db, "session-123")
        assert len(tags) == 2

    def test_queue_tag_for_unregistered_session(self, db):
        """Queueing a tag for an unregistered session still works."""
        # Don't register the session first
        result = queue_tag(db, "session-456", "decision:auth", commit=True)

        assert result is not None
        tags = get_pending_tags(db, "session-456")
        assert len(tags) == 1


class TestConsumePendingTags:
    """Tests for consume_pending_tags()."""

    def test_consume_returns_and_deletes_tags(self, db):
        """Consuming pending tags returns them and removes from DB."""
        register_session(db, "session-123", "claude_code", commit=True)
        queue_tag(db, "session-123", "tag1", commit=True)
        queue_tag(db, "session-123", "tag2", commit=True)

        tags = consume_pending_tags(db, "session-123", commit=True)

        assert len(tags) == 2
        assert {t.tag_name for t in tags} == {"tag1", "tag2"}

        # Tags should be gone
        remaining = get_pending_tags(db, "session-123")
        assert len(remaining) == 0

    def test_consume_preserves_entity_type_and_index(self, db):
        """Consumed tags include entity_type and exchange_index."""
        register_session(db, "session-123", "claude_code", commit=True)
        queue_tag(db, "session-123", "conv-tag", commit=True)
        queue_tag(db, "session-123", "exch-tag", entity_type="exchange", exchange_index=3, commit=True)

        tags = consume_pending_tags(db, "session-123", commit=True)

        conv_tag = next(t for t in tags if t.tag_name == "conv-tag")
        exch_tag = next(t for t in tags if t.tag_name == "exch-tag")

        assert conv_tag.entity_type == "conversation"
        assert conv_tag.exchange_index is None
        assert exch_tag.entity_type == "exchange"
        assert exch_tag.exchange_index == 3

    def test_consume_empty_returns_empty_list(self, db):
        """Consuming from a session with no tags returns empty list."""
        tags = consume_pending_tags(db, "session-999", commit=True)

        assert tags == []


class TestCleanupStaleSessions:
    """Tests for cleanup_stale_sessions()."""

    def test_cleanup_deletes_old_sessions(self, db):
        """Sessions older than max_age_hours are deleted."""
        # Insert a session with old started_at
        old_time = (datetime.now() - timedelta(hours=72)).isoformat()
        db.execute(
            "INSERT INTO active_sessions (harness_session_id, adapter_name, started_at) VALUES (?, ?, ?)",
            ("old-session", "claude_code", old_time),
        )
        db.commit()

        sessions_deleted, tags_deleted = cleanup_stale_sessions(db, max_age_hours=48, commit=True)

        assert sessions_deleted == 1
        assert not is_session_registered(db, "old-session")

    def test_cleanup_preserves_recent_sessions(self, db):
        """Sessions younger than max_age_hours are preserved."""
        register_session(db, "new-session", "claude_code", commit=True)

        sessions_deleted, tags_deleted = cleanup_stale_sessions(db, max_age_hours=48, commit=True)

        assert sessions_deleted == 0
        assert is_session_registered(db, "new-session")

    def test_cleanup_deletes_orphaned_tags(self, db):
        """Tags for sessions not in active_sessions are deleted if old."""
        # Queue tags for a session that was never registered
        old_time = (datetime.now() - timedelta(hours=72)).isoformat()
        db.execute(
            "INSERT INTO pending_tags (id, harness_session_id, tag_name, entity_type, created_at) VALUES (?, ?, ?, ?, ?)",
            ("tag-1", "orphan-session", "orphan-tag", "conversation", old_time),
        )
        db.commit()

        sessions_deleted, tags_deleted = cleanup_stale_sessions(db, max_age_hours=48, commit=True)

        # Should delete the orphaned tag
        assert tags_deleted == 1

        count = db.execute("SELECT COUNT(*) FROM pending_tags").fetchone()[0]
        assert count == 0


class TestOrphanedAndStaleCounts:
    """Tests for get_orphaned_pending_tags_count() and get_stale_sessions_count()."""

    def test_orphaned_count(self, db):
        """Count tags for sessions not in active_sessions."""
        # Register one session with a tag
        register_session(db, "registered", "claude_code", commit=True)
        queue_tag(db, "registered", "tag1", commit=True)

        # Add tag for unregistered session
        queue_tag(db, "unregistered", "tag2", commit=True)

        count = get_orphaned_pending_tags_count(db)
        assert count == 1

    def test_stale_sessions_count(self, db):
        """Count sessions older than max_age_hours."""
        # Insert an old session
        old_time = (datetime.now() - timedelta(hours=72)).isoformat()
        db.execute(
            "INSERT INTO active_sessions (harness_session_id, adapter_name, started_at) VALUES (?, ?, ?)",
            ("old-session", "claude_code", old_time),
        )

        # Register a new session
        register_session(db, "new-session", "claude_code", commit=True)

        db.commit()

        count = get_stale_sessions_count(db, max_age_hours=48)
        assert count == 1


class TestPromptTagsTable:
    """Tests for ensure_prompt_tags_table()."""

    def test_prompt_tags_table_created(self, db):
        """ensure_prompt_tags_table creates the table."""
        # Table should already exist from create_database
        cur = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prompt_tags'"
        )
        assert cur.fetchone() is not None

    def test_prompt_tags_table_is_idempotent(self, db):
        """Calling ensure_prompt_tags_table multiple times is safe."""
        ensure_prompt_tags_table(db)
        ensure_prompt_tags_table(db)

        cur = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prompt_tags'"
        )
        assert cur.fetchone() is not None
