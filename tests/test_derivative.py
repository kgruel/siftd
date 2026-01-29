"""Tests for derivative conversation detection, tagging, and backfill."""

import json

import pytest

from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
    insert_tool_call,
    open_database,
)
from siftd.storage.tags import (
    DERIVATIVE_TAG,
    is_derivative_tool_call,
    tag_derivative_conversation,
)


# ---------------------------------------------------------------------------
# is_derivative_tool_call — pure detection logic
# ---------------------------------------------------------------------------

class TestIsDerivativeToolCall:
    """Unit tests for is_derivative_tool_call()."""

    def test_shell_siftd_ask(self):
        assert is_derivative_tool_call("shell.execute", {"command": "siftd ask 'some query'"})

    def test_shell_siftd_query(self):
        assert is_derivative_tool_call("shell.execute", {"command": "siftd query abc123"})

    def test_shell_siftd_ask_with_flags(self):
        assert is_derivative_tool_call("shell.execute", {"command": "siftd ask -w myproject 'query'"})

    def test_shell_cmd_key(self):
        """Some adapters use 'cmd' instead of 'command'."""
        assert is_derivative_tool_call("shell.execute", {"cmd": "siftd ask 'test'"})

    def test_shell_non_matching_command(self):
        assert not is_derivative_tool_call("shell.execute", {"command": "pytest tests/"})

    def test_shell_siftd_other_subcommand(self):
        """siftd ingest, siftd tag, etc. are NOT derivative."""
        assert not is_derivative_tool_call("shell.execute", {"command": "siftd ingest"})
        assert not is_derivative_tool_call("shell.execute", {"command": "siftd tag abc123 foo"})

    def test_skill_invoke_siftd(self):
        assert is_derivative_tool_call("skill.invoke", {"skill": "siftd"})

    def test_skill_invoke_other(self):
        assert not is_derivative_tool_call("skill.invoke", {"skill": "commit"})

    def test_none_input(self):
        assert not is_derivative_tool_call("shell.execute", None)

    def test_empty_input(self):
        assert not is_derivative_tool_call("shell.execute", {})

    def test_empty_command(self):
        assert not is_derivative_tool_call("shell.execute", {"command": ""})

    def test_unrelated_tool(self):
        assert not is_derivative_tool_call("file.read", {"path": "/tmp/siftd ask"})

    def test_skill_invoke_none_input(self):
        assert not is_derivative_tool_call("skill.invoke", None)


# ---------------------------------------------------------------------------
# tag_derivative_conversation — applies tag via DB
# ---------------------------------------------------------------------------

class TestTagDerivativeConversation:
    """Integration tests for tag_derivative_conversation()."""

    @pytest.fixture
    def conn(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        ws_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        self._conv_id = insert_conversation(
            conn, external_id="c1", harness_id=harness_id,
            workspace_id=ws_id, started_at="2024-01-01T00:00:00Z",
        )
        conn.commit()
        return conn

    @property
    def conv_id(self):
        return self._conv_id

    def test_tags_matching_conversation(self, conn):
        result = tag_derivative_conversation(
            conn, self.conv_id, "shell.execute", {"command": "siftd ask 'test'"},
        )
        assert result is True

        # Verify tag exists
        row = conn.execute("""
            SELECT t.name FROM conversation_tags ct
            JOIN tags t ON t.id = ct.tag_id
            WHERE ct.conversation_id = ?
        """, (self.conv_id,)).fetchone()
        assert row["name"] == DERIVATIVE_TAG

    def test_returns_false_for_non_matching(self, conn):
        result = tag_derivative_conversation(
            conn, self.conv_id, "shell.execute", {"command": "git status"},
        )
        assert result is False

        # No tag applied
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversation_tags WHERE conversation_id = ?",
            (self.conv_id,),
        ).fetchone()
        assert row["cnt"] == 0

    def test_idempotent(self, conn):
        """Tagging the same conversation twice doesn't duplicate."""
        tag_derivative_conversation(
            conn, self.conv_id, "shell.execute", {"command": "siftd ask 'q1'"},
        )
        result = tag_derivative_conversation(
            conn, self.conv_id, "shell.execute", {"command": "siftd ask 'q2'"},
        )
        # Second call returns False (already tagged)
        assert result is False

        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM conversation_tags WHERE conversation_id = ?",
            (self.conv_id,),
        ).fetchone()["cnt"]
        assert count == 1


# ---------------------------------------------------------------------------
# backfill_derivative_tags — retroactive tagging
# ---------------------------------------------------------------------------

class TestBackfillDerivativeTags:
    """Integration tests for backfill_derivative_tags()."""

    @pytest.fixture
    def db_with_tool_calls(self, tmp_path):
        """DB with 3 conversations: one derivative (siftd ask), one derivative
        (siftd query), one normal."""
        from siftd.storage.tags import apply_tag, get_or_create_tag

        db_path = tmp_path / "backfill.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        ws_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "test-model")
        shell_tool_id = get_or_create_tool(conn, "shell.execute")

        # Conv 1: derivative (siftd ask)
        conv1 = insert_conversation(
            conn, external_id="c1", harness_id=harness_id,
            workspace_id=ws_id, started_at="2024-01-01T00:00:00Z",
        )
        p1 = insert_prompt(conn, conv1, "p1", "2024-01-01T00:00:00Z")
        insert_prompt_content(conn, p1, 0, "text", '{"text": "search"}')
        r1 = insert_response(conn, conv1, p1, model_id, None, "r1", "2024-01-01T00:00:01Z",
                             input_tokens=10, output_tokens=5)
        insert_tool_call(conn, r1, conv1, shell_tool_id, "tc1",
                         json.dumps({"command": "siftd ask 'some query'"}),
                         '{"output": "results"}', "success", "2024-01-01T00:00:01Z")

        # Conv 2: derivative (siftd query)
        conv2 = insert_conversation(
            conn, external_id="c2", harness_id=harness_id,
            workspace_id=ws_id, started_at="2024-01-02T00:00:00Z",
        )
        p2 = insert_prompt(conn, conv2, "p2", "2024-01-02T00:00:00Z")
        insert_prompt_content(conn, p2, 0, "text", '{"text": "query"}')
        r2 = insert_response(conn, conv2, p2, model_id, None, "r2", "2024-01-02T00:00:01Z",
                             input_tokens=10, output_tokens=5)
        insert_tool_call(conn, r2, conv2, shell_tool_id, "tc2",
                         json.dumps({"command": "siftd query abc123"}),
                         '{"output": "details"}', "success", "2024-01-02T00:00:01Z")

        # Conv 3: normal (pytest)
        conv3 = insert_conversation(
            conn, external_id="c3", harness_id=harness_id,
            workspace_id=ws_id, started_at="2024-01-03T00:00:00Z",
        )
        p3 = insert_prompt(conn, conv3, "p3", "2024-01-03T00:00:00Z")
        insert_prompt_content(conn, p3, 0, "text", '{"text": "run tests"}')
        r3 = insert_response(conn, conv3, p3, model_id, None, "r3", "2024-01-03T00:00:01Z",
                             input_tokens=10, output_tokens=5)
        insert_tool_call(conn, r3, conv3, shell_tool_id, "tc3",
                         json.dumps({"command": "pytest tests/"}),
                         '{"output": "OK"}', "success", "2024-01-03T00:00:01Z")

        conn.commit()
        conn.close()
        return db_path

    def test_backfill_tags_derivative_conversations(self, db_with_tool_calls):
        from siftd.backfill import backfill_derivative_tags

        conn = open_database(db_with_tool_calls)
        count = backfill_derivative_tags(conn)
        assert count == 2

        # Verify which conversations are tagged
        rows = conn.execute("""
            SELECT c.external_id FROM conversation_tags ct
            JOIN tags t ON t.id = ct.tag_id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE t.name = ?
            ORDER BY c.external_id
        """, (DERIVATIVE_TAG,)).fetchall()
        assert [r["external_id"] for r in rows] == ["c1", "c2"]
        conn.close()

    def test_backfill_skips_already_tagged(self, db_with_tool_calls):
        from siftd.backfill import backfill_derivative_tags

        conn = open_database(db_with_tool_calls)
        # First run
        backfill_derivative_tags(conn)
        # Second run should find nothing new
        count = backfill_derivative_tags(conn)
        assert count == 0
        conn.close()

    def test_backfill_returns_zero_when_no_tools(self, tmp_path):
        """Empty DB with no tool calls returns 0."""
        from siftd.backfill import backfill_derivative_tags

        db_path = tmp_path / "empty.db"
        conn = create_database(db_path)
        conn.commit()
        count = backfill_derivative_tags(conn)
        assert count == 0
        conn.close()
