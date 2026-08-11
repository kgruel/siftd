"""Integration tests for live session tagging flow.

Tests the full workflow: register → queue tag → ingest → verify tag applied.
"""

import json
import logging

import pytest

from siftd.adapters import claude_code
from siftd.domain.models import ContentBlock, Conversation, Harness, Prompt, Response, Usage
from siftd.domain.source import Source
from siftd.ingestion import ingest_all
from siftd.storage.events import get_prompt_by_index
from siftd.storage.sessions import (
    get_pending_tags,
    get_session_info,
    get_stale_sessions_count,
    is_session_registered,
    queue_tag,
    register_session,
    resolve_session_conversation,
)
from siftd.storage.tags import apply_tag, delete_tag, get_or_create_tag, rename_tag
from siftd.storage.sqlite import create_database, open_database
from conftest import (
    FIXTURES_DIR,
    conversation_id as _conversation_id,
    make_conversation,
    make_test_adapter,
    tag_names,
)


def make_live_adapter(source_path, conversation):
    """Create a test adapter with SUPPORTS_LIVE_REGISTRATION."""
    return make_test_adapter(
        source_path, name="live_test", harness_log_format="jsonl",
        supports_live_registration=True,
        parse_fn=lambda source: iter([conversation]),
    )


def make_non_live_adapter(source_path, conversation):
    """Create a test adapter WITHOUT SUPPORTS_LIVE_REGISTRATION.

    The negative half of the flag: ingest must not drain a queued tag for an
    adapter that never claimed to track live sessions.
    """
    return make_test_adapter(
        source_path, name="non_live_test", harness_log_format="jsonl",
        parse_fn=lambda source: iter([conversation]),
    )


@pytest.fixture
def live_db(tmp_path):
    """Create a test database."""
    db_path = tmp_path / "live_test.db"
    conn = create_database(db_path)
    try:
        yield {"path": db_path, "conn": conn}
    finally:
        conn.close()


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
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
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
        exchange_index = 1

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

        # 4. Verify tag was applied to the exchange (anchor = prompt event)
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN events e ON e.id = ta.target_id
            JOIN conversations c ON c.id = e.conversation_id
            WHERE ta.target_kind = 'exchange' AND c.external_id = ?
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
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
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
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
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
            JOIN tag_assignments ta ON ta.tag_id = t.id
            WHERE ta.target_kind = 'exchange'
        """)
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name not in tags

        # The row is KEPT: the target may exist by the next ingest (the
        # transcript is still growing), and `doctor fix --pending-tags` can
        # still reach it. Consuming a row nothing was applied from is data loss.
        pending = get_pending_tags(live_db["conn"], session_id)
        assert len(pending) == 1

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
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
        """, (session_id,))
        applied_tags = [row[0] for row in cur.fetchall()]

        for tag in tags_to_queue:
            assert tag in applied_tags

    def test_renamed_pending_tags_apply_under_new_name(self, live_db, tmp_path):
        """Renaming a queued tag updates pending rows instead of forking names."""
        session_id = "renamed-pending-session"
        old_name = "decision:legacy"
        new_name = "decision:current"

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
        )

        register_session(live_db["conn"], session_id, "live_test", commit=True)
        get_or_create_tag(live_db["conn"], old_name)
        queue_tag(live_db["conn"], session_id, old_name, commit=True)
        queue_tag(live_db["conn"], session_id, new_name, commit=True)

        assert rename_tag(live_db["conn"], old_name, new_name, commit=True)

        pending = get_pending_tags(live_db["conn"], session_id)
        assert [tag.tag_name for tag in pending] == [new_name]

        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
        """, (session_id,))
        applied_tags = [row[0] for row in cur.fetchall()]
        assert applied_tags == [new_name]
        assert live_db["conn"].execute("SELECT COUNT(*) FROM tags WHERE name = ?", (old_name,)).fetchone()[0] == 0

    def test_deleted_pending_tags_are_not_resurrected_at_ingest(self, live_db, tmp_path):
        """Deleting a tag removes queued pending rows before ingest can recreate it."""
        session_id = "deleted-pending-session"
        tag_name = "decision:remove-me"

        test_file = tmp_path / "session.jsonl"
        test_file.write_text("{}")

        conversation = make_conversation(
            external_id=session_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
        )

        register_session(live_db["conn"], session_id, "live_test", commit=True)
        get_or_create_tag(live_db["conn"], tag_name)
        queue_tag(live_db["conn"], session_id, tag_name, commit=True)

        assert delete_tag(live_db["conn"], tag_name, commit=True) == 0
        assert get_pending_tags(live_db["conn"], session_id) == []

        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
        """, (session_id,))
        assert cur.fetchall() == []
        assert live_db["conn"].execute("SELECT COUNT(*) FROM tags WHERE name = ?", (tag_name,)).fetchone()[0] == 0

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
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
        """, (namespaced_session_id,))
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name in tags

        # Verify pending tags consumed
        pending = get_pending_tags(live_db["conn"], namespaced_session_id)
        assert len(pending) == 0

        # Verify session unregistered
        assert not is_session_registered(live_db["conn"], namespaced_session_id)

    def test_subagent_inherits_parent_session_tags(self, live_db, tmp_path):
        """Tags queued against a parent session apply to subagent conversations.

        When a user tags during a subagent session, the tag targets the parent
        session ID (claude_code::<session>), but the subagent conversation has
        external_id = claude_code::<session>::agent::<agent_id>.

        The tag should still be applied when the subagent conversation is ingested.
        """
        parent_session_id = "claude_code::parent-session-abc"
        subagent_external_id = f"{parent_session_id}::agent::agent-xyz-123"
        tag_name = "decision:api-design"

        test_file = tmp_path / "subagent.jsonl"
        test_file.write_text("{}")

        # Subagent conversation uses the extended external_id
        conversation = make_conversation(
            external_id=subagent_external_id,
            workspace_path="/test/project",
            started_at="2024-01-15T10:00:00Z",
            harness_name="claude_code",
            harness_source="anthropic",
        )

        # Register and queue tag against the *parent* session (as the hook does)
        register_session(live_db["conn"], parent_session_id, "claude_code", "/test/project", commit=True)
        queue_tag(live_db["conn"], parent_session_id, tag_name, commit=True)

        # Verify tag is pending on the parent session
        pending = get_pending_tags(live_db["conn"], parent_session_id)
        assert len(pending) == 1

        # Ingest the subagent conversation (parent file may have been skipped)
        adapter = make_live_adapter(str(test_file), conversation)
        ingest_all(live_db["conn"], [adapter])

        # Tag should be applied to the subagent conversation
        cur = live_db["conn"].execute("""
            SELECT t.name FROM tags t
            JOIN tag_assignments ta ON ta.tag_id = t.id
            JOIN conversations c ON c.id = ta.target_id
            WHERE ta.target_kind = 'conversation' AND c.external_id = ?
        """, (subagent_external_id,))
        tags = [row[0] for row in cur.fetchall()]
        assert tag_name in tags

        # Pending tags consumed
        pending = get_pending_tags(live_db["conn"], parent_session_id)
        assert len(pending) == 0

        # Parent session unregistered
        assert not is_session_registered(live_db["conn"], parent_session_id)

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


# ---------------------------------------------------------------------------
# Real-adapter regression coverage
#
# The tests above fabricate conversations whose external_id IS the queue key,
# so the adapter's own namespacing never enters the tested path — which is
# exactly where the "tags queued, never applied" bug hid. The tests below drive
# the real claude_code adapter over a real transcript, so external_id comes out
# as `claude_code::<uuid>` (or `claude_code::<uuid>::agent::<id>`) while the
# queue/registration keys stay bare, as `siftd tag --session` writes them.
# ---------------------------------------------------------------------------


def _write_claude_transcript(dest, session_uuid, *, agent_id=None):
    """Write a realistic claude_code transcript for `session_uuid` at `dest`."""
    lines = (FIXTURES_DIR / "claude_code_minimal.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    for record in records:
        record["sessionId"] = session_uuid
        if agent_id:
            record["agentId"] = agent_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return dest


def _append_turn(path, session_uuid, *, agent_id=None):
    """Append a turn, so the file hash (and size) changes like a live session."""
    record = {
        "type": "user",
        "sessionId": session_uuid,
        "timestamp": "2024-01-15T10:05:00Z",
        "uuid": "msg-appended",
        "message": {"role": "user", "content": [{"type": "text", "text": "One more thing"}]},
    }
    if agent_id:
        record["agentId"] = agent_id
    with path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def _tag_target_kinds(conn, tag_name):
    """(target_kind, count) pairs for a tag — the shape a loss shows up in."""
    return [
        (row["target_kind"], row["n"])
        for row in conn.execute(
            "SELECT ta.target_kind, COUNT(*) AS n FROM tag_assignments ta "
            "JOIN tags t ON t.id = ta.tag_id WHERE t.name = ? "
            "GROUP BY ta.target_kind ORDER BY ta.target_kind",
            (tag_name,),
        ).fetchall()
    ]


def _tag_target(conn, tag_name):
    row = conn.execute(
        "SELECT ta.target_id FROM tag_assignments ta "
        "JOIN tags t ON t.id = ta.tag_id WHERE t.name = ?",
        (tag_name,),
    ).fetchone()
    return row["target_id"] if row else None


# Every DEDUP_STRATEGY a replacement path exists for. Shrink-only: a new
# strategy must be added here (and so asserted on) rather than skipped.
_KNOWN_DEDUP_STRATEGIES = frozenset({"file", "session"})


def _conversation_tag_names(conn, external_id):
    """Tag names on the conversation an external id resolves to."""
    return tag_names(conn, "conversation", _conversation_id(conn, external_id))


@pytest.fixture
def claude_root(tmp_path, monkeypatch):
    """Point the real claude_code adapter at a temp transcript directory."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(claude_code, "DEFAULT_LOCATIONS", [str(root)])
    return root


class TestClaudeCodeSessionTagging:
    """Regression tests for the bare-uuid vs adapter-prefixed key mismatch."""

    def test_bare_queued_tag_applies_to_prefixed_conversation(self, live_db, claude_root):
        """`tag --session <uuid>` queues bare; the conversation is prefixed."""
        session_uuid = "0039a352-b40d-43c8-8c04-ef440fd54841"
        _write_claude_transcript(claude_root / f"{session_uuid}.jsonl", session_uuid)

        queue_tag(live_db["conn"], session_uuid, "decision:auth", commit=True)

        ingest_all(live_db["conn"], [claude_code])

        external_id = f"claude_code::{session_uuid}"
        assert _conversation_id(live_db["conn"], external_id) is not None
        assert "decision:auth" in _conversation_tag_names(live_db["conn"], external_id)
        assert get_pending_tags(live_db["conn"], session_uuid) == []

    def test_bare_queued_tag_applies_to_subagent_conversation(self, live_db, claude_root):
        """Tags queued against the bare parent uuid reach a subagent transcript."""
        session_uuid = "9f0f4b7c-1111-4222-8333-444455556666"
        agent_id = "agent-xyz-123"
        _write_claude_transcript(
            claude_root / "subagents" / f"{session_uuid}-{agent_id}.jsonl",
            session_uuid,
            agent_id=agent_id,
        )

        queue_tag(live_db["conn"], session_uuid, "decision:api-design", commit=True)

        ingest_all(live_db["conn"], [claude_code])

        external_id = f"claude_code::{session_uuid}::agent::{agent_id}"
        assert _conversation_id(live_db["conn"], external_id) is not None
        assert "decision:api-design" in _conversation_tag_names(live_db["conn"], external_id)
        assert get_pending_tags(live_db["conn"], session_uuid) == []

    def test_ingest_unregisters_bare_registered_session(self, live_db, claude_root):
        """`siftd register` records the bare uuid; ingest must clear that row."""
        session_uuid = "5c1d8e2a-7777-4888-9999-aaaabbbbcccc"
        _write_claude_transcript(claude_root / f"{session_uuid}.jsonl", session_uuid)

        register_session(
            live_db["conn"], session_uuid, "claude_code", "/test/workspace", commit=True
        )
        assert is_session_registered(live_db["conn"], session_uuid)

        ingest_all(live_db["conn"], [claude_code])

        assert not is_session_registered(live_db["conn"], session_uuid)


class TestReingestPreservesConversationTags:
    """Regression tests for tag loss when a changed transcript is re-ingested."""

    def test_direct_tag_survives_reingest(self, live_db, claude_root):
        """A tag applied to the conversation row survives its replacement."""
        session_uuid = "2b7c9d10-1234-4567-89ab-cdef01234567"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        ingest_all(live_db["conn"], [claude_code])

        external_id = f"claude_code::{session_uuid}"
        first_id = _conversation_id(live_db["conn"], external_id)
        tag_id = get_or_create_tag(live_db["conn"], "keeper")
        apply_tag(live_db["conn"], "conversation", first_id, tag_id, commit=True)

        # The session keeps going: the transcript grows, so the hash changes and
        # ingest replaces the conversation row.
        _append_turn(transcript, session_uuid)
        ingest_all(live_db["conn"], [claude_code])

        second_id = _conversation_id(live_db["conn"], external_id)
        assert second_id is not None and second_id != first_id
        assert "keeper" in _conversation_tag_names(live_db["conn"], external_id)
        # The stale assignment is gone, not merely shadowed.
        orphans = live_db["conn"].execute(
            "SELECT COUNT(*) FROM tag_assignments WHERE target_kind = 'conversation' AND target_id = ?",
            (first_id,),
        ).fetchone()[0]
        assert orphans == 0

    def test_queued_tag_survives_later_reingest(self, live_db, claude_root):
        """queue → ingest (applies) → transcript grows → re-ingest keeps the tag.

        This is the composition case: the drain consumes a queue row once it
        applies, so the second ingest has nothing left to replay — only
        carrying the assignment across the replacement keeps the tag.
        """
        session_uuid = "8e4f0a55-2222-4333-8444-555566667777"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        queue_tag(live_db["conn"], session_uuid, "decision:queued", commit=True)

        ingest_all(live_db["conn"], [claude_code])
        external_id = f"claude_code::{session_uuid}"
        assert "decision:queued" in _conversation_tag_names(live_db["conn"], external_id)

        _append_turn(transcript, session_uuid)
        ingest_all(live_db["conn"], [claude_code])

        assert "decision:queued" in _conversation_tag_names(live_db["conn"], external_id)

    def test_emptied_transcript_drops_snapshot(self, live_db, claude_root):
        """A transcript that goes empty has no replacement row to carry tags to."""
        session_uuid = "3a1b2c3d-9999-4888-8777-666655554444"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        ingest_all(live_db["conn"], [claude_code])

        external_id = f"claude_code::{session_uuid}"
        first_id = _conversation_id(live_db["conn"], external_id)
        tag_id = get_or_create_tag(live_db["conn"], "vanishing")
        apply_tag(live_db["conn"], "conversation", first_id, tag_id, commit=True)

        transcript.write_text("")
        ingest_all(live_db["conn"], [claude_code])

        # The conversation is gone, and its assignments went with it — the
        # snapshot is deliberately dropped rather than re-pointed at nothing.
        assert _conversation_id(live_db["conn"], external_id) is None
        orphans = live_db["conn"].execute(
            "SELECT COUNT(*) FROM tag_assignments WHERE target_kind = 'conversation' AND target_id = ?",
            (first_id,),
        ).fetchone()[0]
        assert orphans == 0

    def test_emptied_transcript_names_the_dropped_tags(self, live_db, claude_root, caplog):
        """A dropped snapshot is reported, not silently discarded.

        A transcript rewritten in place can transiently parse to zero, so the
        loss window is not only "the user emptied the file". The snapshot is in
        hand at that moment; at minimum say what went with it.
        """
        session_uuid = "7d5e4c3b-1010-4020-8030-404050506060"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        ingest_all(live_db["conn"], [claude_code])

        first_id = _conversation_id(live_db["conn"], f"claude_code::{session_uuid}")
        tag_id = get_or_create_tag(live_db["conn"], "vanishing")
        apply_tag(live_db["conn"], "conversation", first_id, tag_id, commit=True)

        transcript.write_text("")
        with caplog.at_level(logging.WARNING, logger="siftd.ingestion.orchestration"):
            ingest_all(live_db["conn"], [claude_code])

        assert any(
            "1 conversation tag(s)" in r.message and "no longer parses" in r.message
            for r in caplog.records
        )

    def test_emptied_transcript_names_a_block_only_loss(self, live_db, claude_root, caplog):
        """A snapshot holding *only* unrestorable assignments still reports.

        Block tags are counted, never carried (re-pointing them is deferred),
        so a conversation tagged only at block level produced a snapshot with
        empty carry lists — which read as "nothing here" and skipped the
        warning outright, losing the tag in silence. The counters are part of
        what the snapshot holds, so they are part of whether it has anything
        to say.
        """
        session_uuid = "5f6a7b8c-3030-4040-8050-606070708080"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        ingest_all(live_db["conn"], [claude_code])

        conversation_id = _conversation_id(live_db["conn"], f"claude_code::{session_uuid}")
        block_id = live_db["conn"].execute(
            "SELECT ec.id FROM event_content ec JOIN events e ON e.id = ec.event_id "
            "WHERE e.conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()["id"]
        apply_tag(
            live_db["conn"], "block", block_id,
            get_or_create_tag(live_db["conn"], "block-only"), commit=True,
        )

        transcript.write_text("")
        with caplog.at_level(logging.WARNING, logger="siftd.ingestion.orchestration"):
            ingest_all(live_db["conn"], [claude_code])

        assert any(
            "1 block tag(s)" in r.message and "no longer parses" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]


class TestReingestPreservesEventTags:
    """Event-level assignments must survive a conversation replacement too.

    Events get fresh ULIDs on re-ingest and tr_polymorphic_events_cleanup takes
    their assignments, so without an explicit carry a `--last-response` tag is
    applied once and destroyed by the next ingest of a still-growing transcript
    — with its queue row already consumed, so nothing can recover it.
    """

    def test_queued_marker_tag_survives_reingest(self, live_db, claude_root):
        session_uuid = "6c2a1f88-3333-4444-8555-666677778888"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        queue_tag(
            live_db["conn"], session_uuid, "marked",
            entity_type="response", last_marker="last_response", commit=True,
        )

        ingest_all(live_db["conn"], [claude_code])
        assert _tag_target_kinds(live_db["conn"], "marked") == [("response", 1)]
        assert get_pending_tags(live_db["conn"], session_uuid) == []

        _append_turn(transcript, session_uuid)
        ingest_all(live_db["conn"], [claude_code])

        # Still exactly one response assignment — carried, not duplicated.
        assert _tag_target_kinds(live_db["conn"], "marked") == [("response", 1)]

    def test_manual_exchange_tag_survives_reingest(self, live_db, claude_root):
        """The 0.11 element-tagging surface holds across re-ingest as well."""
        session_uuid = "4b8d7e66-5555-4666-8777-888899990000"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        ingest_all(live_db["conn"], [claude_code])

        conv_id = _conversation_id(live_db["conn"], f"claude_code::{session_uuid}")
        prompt_id = get_prompt_by_index(live_db["conn"], conv_id, 1)
        tag_id = get_or_create_tag(live_db["conn"], "key-insight")
        apply_tag(live_db["conn"], "exchange", prompt_id, tag_id, commit=True)

        _append_turn(transcript, session_uuid)
        ingest_all(live_db["conn"], [claude_code])

        new_conv_id = _conversation_id(live_db["conn"], f"claude_code::{session_uuid}")
        assert new_conv_id != conv_id
        assert _tag_target_kinds(live_db["conn"], "key-insight") == [("exchange", 1)]
        # ...and on the *same* turn, not re-resolved to the appended one.
        assert _tag_target(live_db["conn"], "key-insight") == get_prompt_by_index(
            live_db["conn"], new_conv_id, 1
        )


class TestDrainAcrossKeyForms:
    """Both queue key forms are written in one session; both must drain."""

    def test_prefixed_and_bare_keys_both_apply(self, live_db, claude_root):
        """`/siftd:tag` queues prefixed (the hook registers that form);
        `siftd tag --session <uuid>` queues bare. One ingest, both applied."""
        session_uuid = "1a2b3c4d-1111-4222-8333-444455556666"
        _write_claude_transcript(claude_root / f"{session_uuid}.jsonl", session_uuid)

        queue_tag(live_db["conn"], f"claude_code::{session_uuid}", "agent-tag", commit=True)
        queue_tag(live_db["conn"], session_uuid, "human-tag", commit=True)

        ingest_all(live_db["conn"], [claude_code])

        external_id = f"claude_code::{session_uuid}"
        assert _conversation_tag_names(live_db["conn"], external_id) == {
            "agent-tag", "human-tag",
        }
        assert get_pending_tags(live_db["conn"], session_uuid) == []
        assert get_pending_tags(live_db["conn"], f"claude_code::{session_uuid}") == []

    def test_unresolvable_row_is_kept_not_consumed(self, live_db, claude_root):
        """A target that doesn't exist yet stays queued for the next ingest.

        The drain used to delete the session's whole queue before resolving
        anything, so a row targeting a turn the transcript had not reached was
        destroyed with only a log line — doing the opposite of what the
        recovery path documents ("deleting a queued tag is data loss").
        """
        session_uuid = "0f0e0d0c-7777-4888-8999-aaaabbbbcccc"
        transcript = _write_claude_transcript(
            claude_root / f"{session_uuid}.jsonl", session_uuid
        )
        # Exchange 2 hasn't happened yet — the transcript has one turn.
        queue_tag(
            live_db["conn"], session_uuid, "later-turn",
            entity_type="exchange", exchange_index=2, commit=True,
        )

        ingest_all(live_db["conn"], [claude_code])
        assert _tag_target_kinds(live_db["conn"], "later-turn") == []
        assert [p.tag_name for p in get_pending_tags(live_db["conn"], session_uuid)] == [
            "later-turn"
        ]

        # The session reaches that turn; the still-queued row lands.
        _append_turn(transcript, session_uuid)
        ingest_all(live_db["conn"], [claude_code])

        assert _tag_target_kinds(live_db["conn"], "later-turn") == [("exchange", 1)]
        assert get_pending_tags(live_db["conn"], session_uuid) == []


class TestSubagentDrainTargetsParent:
    """The drain and `resolve_session_conversation` must pick the same target."""

    def test_subagent_leaves_session_tag_for_the_parent(self, live_db, claude_root):
        """A subagent re-ingest must not take a tag meant for the session.

        The parent transcript is byte-stable while the subagent keeps writing
        (the tool-result line lands only when the Agent finishes), so the parent
        hits the stat-skip fast path and only the subagent reaches the drain.
        """
        session_uuid = "fede84ef-2222-4333-8444-555566667777"
        agent_id = "ace11e819fbf467d5"
        _write_claude_transcript(claude_root / f"{session_uuid}.jsonl", session_uuid)
        subagent = _write_claude_transcript(
            claude_root / "subagents" / f"agent-{agent_id}.jsonl",
            session_uuid,
            agent_id=agent_id,
        )
        ingest_all(live_db["conn"], [claude_code])

        queue_tag(live_db["conn"], session_uuid, "decision:x", commit=True)

        # Only the subagent changes.
        _append_turn(subagent, session_uuid, agent_id=agent_id)
        ingest_all(live_db["conn"], [claude_code])

        parent_external = f"claude_code::{session_uuid}"
        subagent_external = f"{parent_external}::agent::{agent_id}"
        assert _conversation_tag_names(live_db["conn"], subagent_external) == set()
        # Left queued for the parent's own ingest / `doctor fix --pending-tags`,
        # which resolve_session_conversation points at the parent row.
        assert [p.tag_name for p in get_pending_tags(live_db["conn"], session_uuid)] == [
            "decision:x"
        ]
        assert resolve_session_conversation(live_db["conn"], session_uuid) == (
            _conversation_id(live_db["conn"], parent_external)
        )

    def test_subagent_ingest_keeps_the_parent_registered(self, live_db, claude_root):
        """Unregister what was drained, not what was skipped.

        The narrowing above leaves the parent's queued rows for the parent's
        own ingest — but unregistering every key form anyway told the recovery
        path those rows were orphaned. `doctor fix --pending-tags` would then
        apply a `--last-*` marker against whatever the parent transcript held
        mid-flight, which is the exact loss the registration exists to
        prevent. The subagent's own key still clears: it really was ingested.
        """
        session_uuid = "c0ffee11-2222-4333-8444-555566667777"
        agent_id = "b0bb1e5d00dad0000"
        _write_claude_transcript(claude_root / f"{session_uuid}.jsonl", session_uuid)
        subagent = _write_claude_transcript(
            claude_root / "subagents" / f"agent-{agent_id}.jsonl",
            session_uuid,
            agent_id=agent_id,
        )
        ingest_all(live_db["conn"], [claude_code])

        parent_key = f"claude_code::{session_uuid}"
        subagent_key = f"{parent_key}::agent::{agent_id}"
        # The shipped session-start hook registers the prefixed form.
        register_session(live_db["conn"], parent_key, "claude_code")
        register_session(live_db["conn"], subagent_key, "claude_code")
        queue_tag(live_db["conn"], session_uuid, "decision:final", last_marker="last_response",
                  commit=True)

        # Only the subagent changes, so only it reaches the drain.
        _append_turn(subagent, session_uuid, agent_id=agent_id)
        ingest_all(live_db["conn"], [claude_code])

        assert is_session_registered(live_db["conn"], parent_key)
        assert not is_session_registered(live_db["conn"], subagent_key)
        assert [p.tag_name for p in get_pending_tags(live_db["conn"], session_uuid)] == [
            "decision:final"
        ]

        # And the round-1 liveness guard holds because of it: recovery treats
        # the parent as live and leaves its row alone.
        from siftd.storage.sessions import recover_pending_tags

        result = recover_pending_tags(live_db["conn"], max_age_hours=48, commit=True)
        assert result.applied == []
        assert [p.tag_name for p in get_pending_tags(live_db["conn"], session_uuid)] == [
            "decision:final"
        ]


class TestReplacementPreservesTagsPerDedupStrategy:
    """Ratchet: every replacement path carries the conversation's tags.

    The fix landed on the file-dedup branch first and the session-dedup branch
    kept destroying tags, invisible because every regression test drove
    claude_code (file strategy). Enumerate the strategies instead, so a fourth
    replacement path cannot regress silently.
    """

    @pytest.mark.parametrize("dedup_strategy", sorted(_KNOWN_DEDUP_STRATEGIES))
    def test_tag_survives_replacement(self, live_db, tmp_path, dedup_strategy):
        conn = live_db["conn"]
        source = tmp_path / "sess.jsonl"
        source.write_text("{}")
        state = {"ended_at": "2024-01-15T11:00:00Z"}
        external_id = "strategy_test::S1"

        def parse(_source):
            yield make_conversation(
                external_id=external_id,
                workspace_path="/test/project",
                started_at="2024-01-15T10:00:00Z",
                ended_at=state["ended_at"],
                harness_name="strategy_test",
            )

        _Adapter = make_test_adapter(
            source, name="strategy_test", dedup=dedup_strategy,
            harness_log_format="jsonl", supports_live_registration=True,
            parse_fn=parse,
        )

        ingest_all(conn, [_Adapter])
        first_id = _conversation_id(conn, external_id)
        assert first_id is not None
        apply_tag(conn, "conversation", first_id, get_or_create_tag(conn, "keeper"),
                  commit=True)

        # The session keeps going: new bytes and a later ended_at, which is what
        # each strategy keys its replacement on.
        source.write_text('{"more": 1}')
        state["ended_at"] = "2024-01-15T12:00:00Z"
        ingest_all(conn, [_Adapter])

        second_id = _conversation_id(conn, external_id)
        assert second_id is not None and second_id != first_id, "no replacement happened"
        assert "keeper" in _conversation_tag_names(conn, external_id)


def test_every_adapter_dedup_strategy_is_covered():
    """The ratchet above is only a ratchet if it enumerates every strategy."""
    from siftd.adapters.registry import load_builtin_adapters

    live = {
        getattr(plugin.module, "DEDUP_STRATEGY", "file")
        for plugin in load_builtin_adapters()
    }
    assert live <= _KNOWN_DEDUP_STRATEGIES, (
        f"unenumerated dedup strategy: {sorted(live - _KNOWN_DEDUP_STRATEGIES)} — "
        "add it to _KNOWN_DEDUP_STRATEGIES so tag survival is asserted for it"
    )
