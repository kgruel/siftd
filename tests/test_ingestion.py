"""Tests for ingestion orchestration utility functions."""

from datetime import UTC

import pytest

from siftd.adapters.sdk import AdapterParseError
from siftd.storage.events import get_last_event_id, get_prompt_by_index
from siftd.ingestion.orchestration import (
    _compare_timestamps,
    _extract_first_text,
    _get_single_conversation,
    _normalize_status,
    _parse_timestamp,
    _summarize_conversation,
    _truncate_summary,
)


class TestParseTimestamp:
    def test_zulu(self):
        dt = _parse_timestamp("2024-01-15T10:30:00Z")
        assert dt.tzinfo is not None
        assert dt.year == 2024

    def test_offset(self):
        dt = _parse_timestamp("2024-01-15T10:30:00+00:00")
        assert dt.tzinfo is not None

    def test_naive_assumed_utc(self):
        dt = _parse_timestamp("2024-01-15T10:30:00")
        assert dt.tzinfo == UTC

    def test_fallback_parse(self):
        # A format that fails the first fromisoformat but succeeds on fallback
        dt = _parse_timestamp("2024-01-15")
        assert dt.year == 2024


class TestCompareTimestamps:
    def test_newer(self):
        assert _compare_timestamps("2024-02-01T00:00:00Z", "2024-01-01T00:00:00Z") is True

    def test_older(self):
        assert _compare_timestamps("2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z") is False

    def test_new_none(self):
        assert _compare_timestamps(None, "2024-01-01T00:00:00Z") is False

    def test_existing_none(self):
        assert _compare_timestamps("2024-01-01T00:00:00Z", None) is True


class TestGetSingleConversation:
    def test_empty(self):
        assert _get_single_conversation([], "test.jsonl") is None

    def test_single(self):
        assert _get_single_conversation(["conv1"], "test.jsonl") == "conv1"

    def test_multiple_raises(self):
        with pytest.raises(AdapterParseError, match="yielded 2 conversations"):
            _get_single_conversation(["conv1", "conv2"], "test.jsonl")


class TestNormalizeStatus:
    def test_error(self):
        assert _normalize_status("error: bad parse") == ("error", "bad parse")

    def test_skipped_bare(self):
        assert _normalize_status("skipped") == ("skipped", "unchanged")

    def test_skipped_paren(self):
        assert _normalize_status("skipped (unchanged)") == ("skipped", "unchanged")

    def test_skipped_space_paren(self):
        # "skipped (older)" variant
        kind, reason = _normalize_status("skipped (older)")
        assert kind == "skipped"
        assert reason == "older"

    def test_skipped_other_format(self):
        # "skipped: reason" — doesn't match "skipped (" pattern
        kind, reason = _normalize_status("skipped: duplicate")
        assert kind == "skipped"
        assert reason == ": duplicate"

    def test_skipped_with_inner_parens(self):
        # Hits L147-149: reason starts/ends with parens
        kind, reason = _normalize_status("skipped(empty)")
        assert kind == "skipped"
        assert reason == "empty"

    def test_other_status(self):
        assert _normalize_status("ingested") == ("ingested", None)


class TestExtractFirstText:
    def test_empty(self):
        assert _extract_first_text([]) is None

    def test_non_text_block(self):
        class Block:
            block_type = "image"
            content = "data"
        assert _extract_first_text([Block()]) is None

    def test_text_block(self):
        class Block:
            block_type = "text"
            content = {"text": "hello world"}
        assert _extract_first_text([Block()]) == "hello world"

    def test_empty_text_skipped(self):
        class Block:
            block_type = "text"
            content = {"text": "   "}
        assert _extract_first_text([Block()]) is None


class TestTruncateSummary:
    def test_short(self):
        assert _truncate_summary("hello", 80) == "hello"

    def test_long(self):
        text = "a" * 100
        result = _truncate_summary(text, 80)
        assert len(result) == 80
        assert result.endswith("...")

    def test_tiny_limit(self):
        assert _truncate_summary("hello", 3) == "hel"


class _MockBlock:
    def __init__(self, block_type="text", content=None):
        self.block_type = block_type
        self.content = content


class _MockResponse:
    def __init__(self, content=None, model=None):
        self.content = content or []
        self.model = model


class _MockPrompt:
    def __init__(self, content=None, responses=None):
        self.content = content or []
        self.responses = responses or []


class _MockConversation:
    def __init__(self, prompts=None, workspace_path="/proj"):
        self.prompts = prompts or []
        self.workspace_path = workspace_path


class TestSummarizeConversation:
    def test_summary_from_prompt(self):
        conv = _MockConversation(prompts=[
            _MockPrompt(content=[_MockBlock("text", {"text": "Hello AI"})]),
        ])
        result = _summarize_conversation(conv)
        assert result["summary"] == "Hello AI"

    def test_summary_from_response(self):
        """L186-191: no prompt text, falls back to response text."""
        conv = _MockConversation(prompts=[
            _MockPrompt(
                content=[_MockBlock("image", {"url": "..."})],
                responses=[_MockResponse(
                    content=[_MockBlock("text", {"text": "Here's my analysis"})],
                    model="claude-3",
                )],
            ),
        ])
        result = _summarize_conversation(conv)
        assert result["summary"] == "Here's my analysis"
        assert result["model"] == "claude-3"

    def test_no_summary(self):
        conv = _MockConversation(prompts=[])
        result = _summarize_conversation(conv)
        assert result["summary"] is None
        assert result["exchange_count"] == 0


class TestGetPromptByIndex:
    def test_zero_raises_value_error(self, tmp_path):
        """get_prompt_by_index rejects exchange_index=0 (0-based index, API is 1-based)."""
        from siftd.storage.sqlite import open_database

        conn = open_database(tmp_path / "t.db")
        try:
            with pytest.raises(ValueError, match="exchange_index must be >= 1"):
                get_prompt_by_index(conn, "any-conv-id", 0)
        finally:
            conn.close()

    def test_negative_raises_value_error(self, tmp_path):
        """get_prompt_by_index rejects negative exchange_index."""
        from siftd.storage.sqlite import open_database

        conn = open_database(tmp_path / "t.db")
        try:
            with pytest.raises(ValueError, match="exchange_index must be >= 1"):
                get_prompt_by_index(conn, "any-conv-id", -1)
        finally:
            conn.close()

    def test_none_returns_none(self, tmp_path):
        """get_prompt_by_index returns None when exchange_index is None."""
        from siftd.storage.sqlite import open_database

        conn = open_database(tmp_path / "t.db")
        try:
            assert get_prompt_by_index(conn, "any-conv-id", None) is None
        finally:
            conn.close()


class TestGetLastEventId:
    """get_last_event_id picks the most-recent event of `kind`."""

    def _seed(self, tmp_path):
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_model,
            get_or_create_tool, get_or_create_workspace, insert_conversation,
            insert_prompt, insert_response, insert_tool_call,
        )
        db_path = tmp_path / "events.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        m = get_or_create_model(conn, "m1")
        t = get_or_create_tool(conn, "shell.execute")
        c = insert_conversation(conn, external_id="c", harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")
        # Two prompts/responses/tool_calls in chronological order
        p1 = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
        r1 = insert_response(conn, c, p1, m, None, "r1", "2024-01-15T10:00:01Z")
        tc1 = insert_tool_call(conn, r1, c, t, "tc1", "{}", "{}", "success",
                               "2024-01-15T10:00:01Z")
        p2 = insert_prompt(conn, c, "p2", "2024-01-15T11:00:00Z")
        r2 = insert_response(conn, c, p2, m, None, "r2", "2024-01-15T11:00:01Z")
        tc2 = insert_tool_call(conn, r2, c, t, "tc2", "{}", "{}", "success",
                               "2024-01-15T11:00:01Z")
        conn.commit()
        return conn, c, p1, p2, r1, r2, tc1, tc2

    def test_last_prompt(self, tmp_path):
        conn, c, _p1, p2, *_ = self._seed(tmp_path)
        try:
            assert get_last_event_id(conn, c, "prompt") == p2
        finally:
            conn.close()

    def test_last_response(self, tmp_path):
        conn, c, _p1, _p2, _r1, r2, *_ = self._seed(tmp_path)
        try:
            assert get_last_event_id(conn, c, "response") == r2
        finally:
            conn.close()

    def test_last_tool_call(self, tmp_path):
        conn, c, *_, tc1, tc2 = self._seed(tmp_path)
        del tc1
        try:
            assert get_last_event_id(conn, c, "tool_call") == tc2
        finally:
            conn.close()

    def test_empty_returns_none(self, tmp_path):
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_workspace,
            insert_conversation,
        )
        db_path = tmp_path / "empty.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        c = insert_conversation(conn, external_id="c", harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")
        try:
            assert get_last_event_id(conn, c, "response") is None
        finally:
            conn.close()


class TestApplyPendingTagsLastMarkers:
    """End-to-end: pending tags with last_marker apply to the right event at ingest."""

    def test_last_response_lands_on_most_recent_response(self, tmp_path):
        from siftd.api.sessions import queue_tag as api_queue_tag
        from siftd.ingestion.orchestration import _apply_pending_tags
        from siftd.storage.sessions import register_session
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_model,
            get_or_create_workspace, insert_conversation, insert_prompt,
            insert_response,
        )

        db_path = tmp_path / "lm.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "claude_code", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        m = get_or_create_model(conn, "m1")

        sid = "session-xyz"
        register_session(conn, sid, "claude_code", "/p")
        c = insert_conversation(conn, external_id=sid, harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")
        p1 = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
        r1 = insert_response(conn, c, p1, m, None, "r1", "2024-01-15T10:00:01Z")
        p2 = insert_prompt(conn, c, "p2", "2024-01-15T11:00:00Z")
        r2 = insert_response(conn, c, p2, m, None, "r2", "2024-01-15T11:00:01Z")
        del p1, p2, r1  # silence unused

        api_queue_tag(conn, sid, "review-me",
                      entity_type="response", last_marker="last_response")

        # Build a minimal adapter stub recognized by _apply_pending_tags
        class _Adapter:
            SUPPORTS_LIVE_REGISTRATION = True

        class _Conv:
            external_id = sid

        applied = _apply_pending_tags(conn, _Adapter(), _Conv(), c)
        conn.commit()

        assert applied == 1
        # Tag should be on r2 (the most recent response), not r1
        row = conn.execute(
            "SELECT ta.target_kind, ta.target_id FROM tag_assignments ta "
            "JOIN tags t ON t.id = ta.tag_id WHERE t.name = 'review-me'",
        ).fetchone()
        assert row is not None
        assert row["target_kind"] == "response"
        assert row["target_id"] == r2
        conn.close()

    def test_last_prompt_lands_on_most_recent_prompt(self, tmp_path):
        from siftd.api.sessions import queue_tag as api_queue_tag
        from siftd.ingestion.orchestration import _apply_pending_tags
        from siftd.storage.sessions import register_session
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_workspace,
            insert_conversation, insert_prompt,
        )

        db_path = tmp_path / "lm2.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "claude_code", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        sid = "s2"
        register_session(conn, sid, "claude_code", "/p")
        c = insert_conversation(conn, external_id=sid, harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")
        p1 = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
        p2 = insert_prompt(conn, c, "p2", "2024-01-15T11:00:00Z")
        del p1

        api_queue_tag(conn, sid, "decision:auth",
                      entity_type="prompt", last_marker="last_prompt")

        class _Adapter:
            SUPPORTS_LIVE_REGISTRATION = True

        class _Conv:
            external_id = sid

        applied = _apply_pending_tags(conn, _Adapter(), _Conv(), c)
        conn.commit()
        assert applied == 1
        row = conn.execute(
            "SELECT ta.target_kind, ta.target_id FROM tag_assignments ta "
            "JOIN tags t ON t.id = ta.tag_id WHERE t.name = 'decision:auth'",
        ).fetchone()
        assert row["target_kind"] == "prompt"
        assert row["target_id"] == p2
        conn.close()

    def test_last_exchange_anchors_on_most_recent_prompt(self, tmp_path):
        """last_exchange resolves to the most recent prompt event but tags it
        as target_kind='exchange' (the polymorphic exchange anchor)."""
        from siftd.api.sessions import queue_tag as api_queue_tag
        from siftd.ingestion.orchestration import _apply_pending_tags
        from siftd.storage.sessions import register_session
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_workspace,
            insert_conversation, insert_prompt,
        )

        db_path = tmp_path / "lm_exchange.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "claude_code", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        sid = "s-exchange"
        register_session(conn, sid, "claude_code", "/p")
        c = insert_conversation(conn, external_id=sid, harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")
        p1 = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
        p2 = insert_prompt(conn, c, "p2", "2024-01-15T11:00:00Z")
        del p1

        api_queue_tag(conn, sid, "key-insight",
                      entity_type="exchange", last_marker="last_exchange")

        class _Adapter:
            SUPPORTS_LIVE_REGISTRATION = True

        class _Conv:
            external_id = sid

        applied = _apply_pending_tags(conn, _Adapter(), _Conv(), c)
        conn.commit()

        assert applied == 1
        # Tag should land on p2 (most recent prompt) with target_kind='exchange'
        row = conn.execute(
            "SELECT ta.target_kind, ta.target_id FROM tag_assignments ta "
            "JOIN tags t ON t.id = ta.tag_id WHERE t.name = 'key-insight'",
        ).fetchone()
        assert row is not None
        assert row["target_kind"] == "exchange"
        assert row["target_id"] == p2
        conn.close()

    def test_no_matching_event_skips_tag(self, tmp_path):
        """Tag with last_tool_call but no tool_calls in the conversation: skip."""
        from siftd.api.sessions import queue_tag as api_queue_tag
        from siftd.ingestion.orchestration import _apply_pending_tags
        from siftd.storage.sessions import register_session
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_workspace,
            insert_conversation, insert_prompt,
        )

        db_path = tmp_path / "lm3.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "claude_code", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        sid = "s3"
        register_session(conn, sid, "claude_code", "/p")
        c = insert_conversation(conn, external_id=sid, harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")
        insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")

        api_queue_tag(conn, sid, "slow",
                      entity_type="tool_call", last_marker="last_tool_call")

        class _Adapter:
            SUPPORTS_LIVE_REGISTRATION = True

        class _Conv:
            external_id = sid

        applied = _apply_pending_tags(conn, _Adapter(), _Conv(), c)
        conn.commit()
        assert applied == 0
        # No tag assignment created
        row = conn.execute(
            "SELECT 1 FROM tag_assignments ta "
            "JOIN tags t ON t.id = ta.tag_id WHERE t.name = 'slow'",
        ).fetchone()
        assert row is None
        conn.close()


class TestSessionKeyCandidates:
    """Key forms a pending tag / registered session may be stored under."""

    class _ClaudeCode:
        NAME = "claude_code"

    def test_prefixed_conversation_falls_back_to_bare(self):
        from siftd.ingestion.orchestration import _session_key_candidates

        assert _session_key_candidates(self._ClaudeCode(), "claude_code::abc") == [
            "claude_code::abc",
            "abc",
        ]

    def test_subagent_tries_bare_parent_not_bare_subagent(self):
        from siftd.ingestion.orchestration import _session_key_candidates

        # `<uuid>::agent::<id>` is not a key any write path produces, so it must
        # not appear: the queue only ever holds the bare parent uuid.
        assert _session_key_candidates(
            self._ClaudeCode(), "claude_code::abc::agent::a1"
        ) == ["claude_code::abc::agent::a1", "claude_code::abc", "abc"]

    def test_bare_external_id_is_its_own_only_candidate(self):
        from siftd.ingestion.orchestration import _session_key_candidates

        assert _session_key_candidates(self._ClaudeCode(), "abc") == ["abc"]

    def test_other_adapters_prefix_is_left_alone(self):
        from siftd.ingestion.orchestration import _session_key_candidates

        class _Other:
            NAME = "other"

        # `::` that isn't this adapter's name prefix is not stripped.
        assert _session_key_candidates(_Other(), "claude_code::abc") == ["claude_code::abc"]

    def test_adapter_without_name_attribute(self):
        from siftd.ingestion.orchestration import _session_key_candidates

        class _Nameless:
            pass

        assert _session_key_candidates(_Nameless(), "::abc") == ["::abc"]


class TestBookkeepingRecovery:
    """The ingested_files row must never lose a valid conversation pointer.

    Two concurrent ingests both parse the same changed transcript and both
    insert the same conversation; the loser hits
    ``UNIQUE(harness_id, external_id)``. Discarding the winner's pointer there
    (the pre-0.12.1 behavior) is a fixed point: the NULL makes the next
    re-ingest skip its delete, so it collides again, forever
    (kgruel/siftd#29). These tests pin both halves — never write the NULL, and
    self-heal a row already carrying one.
    """

    @staticmethod
    def _adapter(source_path, state, *, name="poison_test"):
        from siftd.domain.source import Source

        from conftest import make_conversation

        class _Adapter:
            ADAPTER_INTERFACE_VERSION = 1
            NAME = name
            DEFAULT_LOCATIONS = []
            DEDUP_STRATEGY = "file"
            HARNESS_SOURCE = "test"
            HARNESS_LOG_FORMAT = "jsonl"

            @staticmethod
            def can_handle(source):
                return True

            @staticmethod
            def parse(source):
                if state.get("raise"):
                    raise ValueError(state["raise"])
                yield make_conversation(
                    external_id=state["external_id"],
                    harness_name=name,
                    prompt_text=state["text"],
                    ended_at=state["ended_at"],
                )

            @staticmethod
            def discover():
                yield Source(kind="file", location=str(source_path))

        return _Adapter

    @staticmethod
    def _copies_adapter(entries, *, name="poison_test"):
        """Adapter over several paths that all parse to ONE external_id.

        Two paths carrying one session is not exotic: ``external_id`` is derived
        from the transcript's own session id, not its path, and the
        duplicate-collision repair itself links a second path at an existing
        conversation. A restored backup under a second scanned root, or a
        ``--path`` overlapping a default location, produces it with no
        concurrency at all.
        """
        from siftd.domain.source import Source

        from conftest import make_conversation

        class _Adapter:
            ADAPTER_INTERFACE_VERSION = 1
            NAME = name
            DEFAULT_LOCATIONS = []
            DEDUP_STRATEGY = "file"
            HARNESS_SOURCE = "test"
            HARNESS_LOG_FORMAT = "jsonl"

            @staticmethod
            def can_handle(source):
                return True

            @staticmethod
            def parse(source):
                entry = entries[str(source.as_path)]
                yield make_conversation(
                    external_id=entry["external_id"],
                    harness_name=name,
                    prompt_text=entry["text"],
                    ended_at=entry["ended_at"],
                )

            @staticmethod
            def discover():
                for path in entries:
                    yield Source(kind="file", location=path)

        return _Adapter

    @staticmethod
    def _indexed(conn, needle):
        return conn.execute(
            "SELECT COUNT(*) FROM content_fts WHERE text_content LIKE ?",
            (f"%{needle}%",),
        ).fetchone()[0]

    @staticmethod
    def _orphans(conn):
        return conn.execute(
            "SELECT COUNT(*) FROM conversations c WHERE NOT EXISTS "
            "(SELECT 1 FROM ingested_files f WHERE f.conversation_id = c.id)"
        ).fetchone()[0]

    @staticmethod
    def _conversation_id(conn, external_id):
        row = conn.execute(
            "SELECT id FROM conversations WHERE external_id = ?", (external_id,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _tag_names(conn, conversation_id):
        return {
            r[0]
            for r in conn.execute(
                "SELECT t.name FROM tag_assignments a JOIN tags t ON t.id = a.tag_id "
                "WHERE a.target_kind = 'conversation' AND a.target_id = ?",
                (conversation_id,),
            )
        }

    def _seed(self, tmp_path, state):
        """Ingest one transcript normally; return (conn, source, adapter)."""
        from siftd.ingestion import ingest_all
        from siftd.storage.sqlite import create_database

        conn = create_database(tmp_path / "recovery.db")
        source = tmp_path / "session.jsonl"
        source.write_text("{}")
        adapter = self._adapter(source, state)
        ingest_all(conn, [adapter])
        return conn, source, adapter

    def test_null_pointer_row_self_heals_and_carries_tags(self, tmp_path):
        """The report's regression recipe: a field-poisoned row re-ingests cleanly.

        The tags live on the ORPHAN here, not on anything the bookkeeping
        knows about — so a fix that deletes the orphan without snapshotting
        would mass-destroy tags on the first ingest after upgrade.
        """
        from siftd.ingestion import ingest_all
        from siftd.storage.sqlite import get_ingested_file_info
        from siftd.storage.tags import apply_tag, get_or_create_tag

        state = {
            "external_id": "poison_test::S1",
            "text": "first",
            "ended_at": "2024-01-15T11:00:00Z",
        }
        conn, source, adapter = self._seed(tmp_path, state)

        orphan_id = self._conversation_id(conn, state["external_id"])
        assert orphan_id is not None
        apply_tag(conn, "conversation", orphan_id, get_or_create_tag(conn, "keeper"), commit=True)

        # The poisoned steady state: pointer discarded, conversation left behind.
        conn.execute(
            "UPDATE ingested_files SET conversation_id = NULL, error = ? WHERE path = ?",
            ("UNIQUE constraint failed: conversations.harness_id, conversations.external_id",
             str(source)),
        )
        conn.commit()
        assert self._orphans(conn) == 1

        # The session keeps growing, so the hash changes and the file is
        # eligible for re-ingest again.
        source.write_text('{"more": 1}')
        state["text"] = "second"
        state["ended_at"] = "2024-01-15T12:00:00Z"
        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 0
        assert stats.files_replaced == 1

        info = get_ingested_file_info(conn, str(source))
        assert info["error"] is None
        new_id = self._conversation_id(conn, state["external_id"])
        assert new_id is not None and new_id != orphan_id
        assert info["conversation_id"] == new_id, "bookkeeping still points at nothing"
        assert self._orphans(conn) == 0

        # New content is indexed...
        indexed = conn.execute(
            "SELECT COUNT(*) FROM content_fts WHERE conversation_id = ? "
            "AND text_content LIKE '%second%'",
            (new_id,),
        ).fetchone()[0]
        assert indexed == 1
        # ...and the orphan's tags rode across.
        assert "keeper" in self._tag_names(conn, new_id)

    def test_lost_race_keeps_the_pointer_and_leaves_no_orphan(self, tmp_path, monkeypatch):
        """The race loser repairs bookkeeping instead of NULLing it.

        Simulates the winner-committed state: the conversation and a correct
        ingested_files row already exist, and this run's insert collides. The
        rollback resurrects the row the re-ingest branch had deleted, so the
        handler must UPSERT rather than insert.
        """
        import sqlite3

        from siftd.ingestion import ingest_all, orchestration
        from siftd.storage.sqlite import compute_file_hash, get_ingested_file_info
        from siftd.storage.tags import apply_tag, get_or_create_tag

        state = {
            "external_id": "poison_test::S2",
            "text": "first",
            "ended_at": "2024-01-15T11:00:00Z",
        }
        conn, source, adapter = self._seed(tmp_path, state)
        winner_id = self._conversation_id(conn, state["external_id"])
        apply_tag(conn, "conversation", winner_id, get_or_create_tag(conn, "keeper"), commit=True)

        real_store = orchestration.store_conversation
        calls = {"n": 0}

        def _colliding_store(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: conversations.harness_id, "
                    "conversations.external_id"
                )
            return real_store(*args, **kwargs)

        monkeypatch.setattr(orchestration, "store_conversation", _colliding_store)

        source.write_text('{"more": 1}')
        state["text"] = "second"
        stats = ingest_all(conn, [adapter])

        assert calls["n"] == 1, "the collision path must not re-store the conversation"
        assert stats.files_skipped == 1
        assert stats.files_errored == 0

        info = get_ingested_file_info(conn, str(source))
        assert info["conversation_id"] == winner_id
        assert info["error"] is None
        assert self._orphans(conn) == 0
        # A lost race is a no-op: the winner's conversation and its tags stand.
        assert "keeper" in self._tag_names(conn, winner_id)
        # ...but the row must NOT claim the bytes this run hashed were ingested.
        # The winner stored some other read of them; stamping the current hash
        # here is what froze the transcript silently and forever.
        assert info["file_hash"] != compute_file_hash(source)

        # So the next run re-hashes, takes the re-ingest branch (the pointer is
        # non-NULL now, so the delete actually runs) and converges on the
        # current content — carrying the tags across, as any replacement must.
        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 0
        assert stats.files_replaced == 1
        assert self._indexed(conn, "second") == 1
        converged_id = self._conversation_id(conn, state["external_id"])
        info = get_ingested_file_info(conn, str(source))
        assert info["conversation_id"] == converged_id
        assert info["file_hash"] == compute_file_hash(source)
        assert self._orphans(conn) == 0
        assert "keeper" in self._tag_names(conn, converged_id)

    def test_poisoned_row_heals_without_the_file_changing(self, tmp_path):
        """A finished transcript heals too — the population that had gone quiet.

        The self-heal is only reachable through the re-ingest branch, which sits
        behind two skips keyed on stat and hash. ``_record_file_error`` stamps
        the failing file's own stat, so a poisoned row whose transcript has
        stopped growing matched both skips on every later run and never healed —
        and that is most of the field damage, not a corner of it. A row carrying
        an error is therefore re-examined whatever its stat says.
        """
        from siftd.ingestion import ingest_all
        from siftd.storage.sqlite import get_ingested_file_info
        from siftd.storage.tags import apply_tag, get_or_create_tag

        state = {
            "external_id": "poison_test::S5",
            "text": "first",
            "ended_at": "2024-01-15T11:00:00Z",
        }
        conn, source, adapter = self._seed(tmp_path, state)
        orphan_id = self._conversation_id(conn, state["external_id"])
        apply_tag(conn, "conversation", orphan_id, get_or_create_tag(conn, "keeper"), commit=True)

        # Exactly what an older siftd left behind, stat included: the file is
        # never touched again from here.
        conn.execute(
            "UPDATE ingested_files SET conversation_id = NULL, error = ? WHERE path = ?",
            ("UNIQUE constraint failed: conversations.harness_id, conversations.external_id",
             str(source)),
        )
        conn.commit()
        assert self._orphans(conn) == 1

        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 0
        info = get_ingested_file_info(conn, str(source))
        assert info["error"] is None
        healed_id = self._conversation_id(conn, state["external_id"])
        assert info["conversation_id"] == healed_id
        assert self._orphans(conn) == 0
        assert "keeper" in self._tag_names(conn, healed_id)

    def test_a_second_copy_never_deletes_the_tracked_conversation(self, tmp_path):
        """A NULL pointer on one path is not proof the conversation is unowned.

        ``ingested_files.conversation_id`` is ON DELETE CASCADE and carries no
        uniqueness constraint, so deleting a conversation another path owns
        destroys its events *and* that path's bookkeeping row — silently, with
        the run reporting success.
        """
        from siftd.ingestion import ingest_all
        from siftd.storage.sqlite import create_database, get_ingested_file_info

        live = tmp_path / "live.jsonl"
        stale = tmp_path / "stale-copy.jsonl"
        live.write_text("{}")
        stale.write_text("{}")
        entries = {
            str(live): {"external_id": "poison_test::S6", "text": "live-content",
                        "ended_at": "2024-01-15T12:00:00Z"},
            str(stale): {"external_id": "poison_test::S6", "text": "stale-content",
                         "ended_at": "2024-01-15T11:00:00Z"},
        }
        adapter = self._copies_adapter(entries)

        conn = create_database(tmp_path / "copies.db")
        ingest_all(conn, [adapter])
        conv_id = self._conversation_id(conn, "poison_test::S6")
        assert get_ingested_file_info(conn, str(live))["conversation_id"] == conv_id

        # The copy's row loses its pointer (any failure path used to write this)
        # and its bytes then change, which is what arms the destructive delete.
        conn.execute(
            "UPDATE ingested_files SET conversation_id = NULL WHERE path = ?", (str(stale),)
        )
        conn.commit()
        stale.write_text('{"more": 1}')

        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 0
        # The live conversation, its events, and its bookkeeping all survive.
        assert self._conversation_id(conn, "poison_test::S6") == conv_id
        assert self._indexed(conn, "live-content") == 1
        assert self._indexed(conn, "stale-content") == 0
        assert get_ingested_file_info(conn, str(live))["conversation_id"] == conv_id
        # ...and the copy is linked at it rather than left pointing at nothing.
        assert get_ingested_file_info(conn, str(stale))["conversation_id"] == conv_id
        assert self._orphans(conn) == 0

    def test_two_copies_of_one_session_settle_instead_of_churning(self, tmp_path):
        """Only one path can hold a session's slot, so the loser settles quietly.

        Repeated ingests must not have the copies take turns replacing each
        other's conversation — that would delete and re-mint events every run.
        The collision repair stamps the duplicate's own hash for exactly this
        case (and only this case), so it stops re-entering.
        """
        from siftd.ingestion import ingest_all
        from siftd.storage.sqlite import create_database

        first = tmp_path / "a.jsonl"
        second = tmp_path / "b.jsonl"
        first.write_text("{}")
        second.write_text("{}")
        entries = {
            str(first): {"external_id": "poison_test::S7", "text": "from-a",
                         "ended_at": "2024-01-15T11:00:00Z"},
            str(second): {"external_id": "poison_test::S7", "text": "from-b",
                          "ended_at": "2024-01-15T11:00:00Z"},
        }
        adapter = self._copies_adapter(entries)

        conn = create_database(tmp_path / "settle.db")
        ingest_all(conn, [adapter])
        conv_id = self._conversation_id(conn, "poison_test::S7")

        for _ in range(3):
            stats = ingest_all(conn, [adapter])
            assert stats.files_errored == 0
            assert stats.files_replaced == 0
            assert self._conversation_id(conn, "poison_test::S7") == conv_id
        assert self._orphans(conn) == 0

    def test_failed_repair_leaves_the_bookkeeping_untouched(self, tmp_path, monkeypatch):
        """The collision path must never reach the pointer-clearing writer.

        If the repair itself fails — the transcript was truncated or rotated
        between the store attempt and the re-parse — the run is reported as an
        error and the row is left exactly as it was. Writing NULL here would
        re-create the #29 fixed point the whole branch exists to prevent.
        """
        import sqlite3

        from siftd.ingestion import ingest_all, orchestration
        from siftd.storage.sqlite import get_ingested_file_info

        state = {
            "external_id": "poison_test::S8",
            "text": "first",
            "ended_at": "2024-01-15T11:00:00Z",
        }
        conn, source, adapter = self._seed(tmp_path, state)
        winner_id = self._conversation_id(conn, state["external_id"])

        real_store = orchestration.store_conversation

        def _colliding_store(*args, **kwargs):
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: conversations.harness_id, "
                "conversations.external_id"
            )

        monkeypatch.setattr(orchestration, "store_conversation", _colliding_store)
        # The repair's own re-parse is the second parse of this run; fail that
        # one, so the collision is reached and the repair is not.
        state["parses"] = 0
        real_parse = adapter.parse

        def _parse(source_arg):
            state["parses"] += 1
            if state["parses"] == 2:
                raise ValueError("transcript rotated mid-repair")
            yield from real_parse(source_arg)

        monkeypatch.setattr(adapter, "parse", staticmethod(_parse))

        source.write_text('{"more": 1}')
        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 1
        info = get_ingested_file_info(conn, str(source))
        assert info["conversation_id"] == winner_id, "the pointer was discarded"
        assert info["error"] is None, "the row was rewritten"
        assert self._orphans(conn) == 0
        assert real_store is not orchestration.store_conversation

    def test_session_strategy_marker_is_never_repointed(self, tmp_path, monkeypatch):
        """A session marker is conversation_id=NULL by design; the repair skips it.

        One file, many sessions: pointing its marker at a single conversation
        would give the whole file's bookkeeping that conversation's ON DELETE
        CASCADE, so replacing that one session would silently drop the marker
        for every other session in the file.
        """
        import sqlite3

        from siftd.ingestion import ingest_all, orchestration
        from siftd.storage.sqlite import get_ingested_file_info

        state = {
            "external_id": "poison_test::S9",
            "text": "first",
            "ended_at": "2024-01-15T11:00:00Z",
        }
        from siftd.storage.sqlite import create_database

        conn = create_database(tmp_path / "session.db")
        source = tmp_path / "sessions.db"
        source.write_text("{}")
        adapter = self._adapter(source, state, name="session_test")
        adapter.DEDUP_STRATEGY = "session"

        ingest_all(conn, [adapter])
        assert get_ingested_file_info(conn, str(source))["conversation_id"] is None

        def _colliding_store(*args, **kwargs):
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: conversations.harness_id, "
                "conversations.external_id"
            )

        monkeypatch.setattr(orchestration, "store_conversation", _colliding_store)
        source.write_text('{"more": 1}')
        state["ended_at"] = "2024-01-15T12:00:00Z"
        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 1
        info = get_ingested_file_info(conn, str(source))
        assert info["conversation_id"] is None, "a session marker was re-pointed"
        assert "UNIQUE constraint failed" in info["error"]

    def test_parse_failure_still_records_null_and_error(self, tmp_path):
        """Unchanged for genuine failures: nothing parsed, so nothing to point at."""
        from siftd.ingestion import ingest_all
        from siftd.storage.sqlite import create_database, get_ingested_file_info

        from siftd.domain.source import Source  # noqa: F401  (adapter factory uses it)

        conn = create_database(tmp_path / "failure.db")
        source = tmp_path / "broken.jsonl"
        source.write_text("{}")
        state = {
            "external_id": "poison_test::S3",
            "text": "first",
            "ended_at": None,
            "raise": "malformed transcript",
        }
        adapter = self._adapter(source, state)

        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 1
        info = get_ingested_file_info(conn, str(source))
        assert info["conversation_id"] is None
        assert "malformed transcript" in info["error"]

    def test_parse_failure_after_success_keeps_the_live_pointer(self, tmp_path):
        """A failure after a success does not strand what the file produced.

        The re-ingest branch deletes before it parses, so the handler's
        ``conn.rollback()`` has resurrected that conversation by the time the
        error is recorded: a valid conversation *does* belong to this path, and
        NULLing the pointer would orphan it — the corruption, not the report.
        The error is still recorded, which is what makes the file eligible for
        another attempt next run.
        """
        from siftd.ingestion import ingest_all
        from siftd.storage.sqlite import get_ingested_file_info

        state = {
            "external_id": "poison_test::S4",
            "text": "first",
            "ended_at": "2024-01-15T11:00:00Z",
        }
        conn, source, adapter = self._seed(tmp_path, state)
        conv_id = self._conversation_id(conn, state["external_id"])

        source.write_text('{"more": 1}')
        state["raise"] = "malformed transcript"
        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 1
        info = get_ingested_file_info(conn, str(source))
        assert info["conversation_id"] == conv_id
        assert "malformed transcript" in info["error"]
        assert self._orphans(conn) == 0

        # The recorded error keeps the file eligible: once it parses again the
        # row heals on its own, with no further change to the file.
        del state["raise"]
        state["text"] = "second"
        stats = ingest_all(conn, [adapter])

        assert stats.files_errored == 0
        info = get_ingested_file_info(conn, str(source))
        assert info["error"] is None
        assert self._indexed(conn, "second") == 1
