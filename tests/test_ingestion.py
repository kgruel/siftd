"""Tests for ingestion orchestration utility functions."""

from datetime import UTC

import pytest

from siftd.adapters.sdk import AdapterParseError
from siftd.ingestion.orchestration import (
    _compare_timestamps,
    _extract_first_text,
    _get_last_event_id,
    _get_prompt_by_index,
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
        """_get_prompt_by_index rejects exchange_index=0 (0-based index, API is 1-based)."""
        from siftd.storage.sqlite import open_database

        conn = open_database(tmp_path / "t.db")
        try:
            with pytest.raises(ValueError, match="exchange_index must be >= 1"):
                _get_prompt_by_index(conn, "any-conv-id", 0)
        finally:
            conn.close()

    def test_negative_raises_value_error(self, tmp_path):
        """_get_prompt_by_index rejects negative exchange_index."""
        from siftd.storage.sqlite import open_database

        conn = open_database(tmp_path / "t.db")
        try:
            with pytest.raises(ValueError, match="exchange_index must be >= 1"):
                _get_prompt_by_index(conn, "any-conv-id", -1)
        finally:
            conn.close()

    def test_none_returns_none(self, tmp_path):
        """_get_prompt_by_index returns None when exchange_index is None."""
        from siftd.storage.sqlite import open_database

        conn = open_database(tmp_path / "t.db")
        try:
            assert _get_prompt_by_index(conn, "any-conv-id", None) is None
        finally:
            conn.close()


class TestGetLastEventId:
    """_get_last_event_id picks the most-recent event of `kind`."""

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
            assert _get_last_event_id(conn, c, "prompt") == p2
        finally:
            conn.close()

    def test_last_response(self, tmp_path):
        conn, c, _p1, _p2, _r1, r2, *_ = self._seed(tmp_path)
        try:
            assert _get_last_event_id(conn, c, "response") == r2
        finally:
            conn.close()

    def test_last_tool_call(self, tmp_path):
        conn, c, *_, tc1, tc2 = self._seed(tmp_path)
        del tc1
        try:
            assert _get_last_event_id(conn, c, "tool_call") == tc2
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
            assert _get_last_event_id(conn, c, "response") is None
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
