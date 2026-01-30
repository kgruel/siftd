"""End-to-end integration tests.

These test the full flow: parse fixture → ingest → query → detail.
Each test creates an isolated database and runs real adapters.
"""

from conftest import FIXTURES_DIR, make_conversation

from siftd.adapters import claude_code
from siftd.api import get_conversation, get_stats, list_conversations
from siftd.domain.models import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall, Usage
from siftd.domain.source import Source
from siftd.ingestion.orchestration import ingest_all
from siftd.storage.fts import rebuild_fts_index, search_content
from siftd.storage.sqlite import (
    create_database,
    delete_conversation,
    open_database,
    record_ingested_file,
    store_conversation,
)
from siftd.storage.tags import apply_tag, get_or_create_tag


def _make_file_adapter(dest):
    """Adapter that discovers a single file and parses with claude_code."""

    class _Adapter:
        NAME = "claude_code"
        DEDUP_STRATEGY = "file"
        HARNESS_SOURCE = "anthropic"

        @staticmethod
        def can_handle(source):
            return claude_code.can_handle(source)

        @staticmethod
        def parse(source):
            return claude_code.parse(source)

        @staticmethod
        def discover():
            yield Source(kind="file", location=dest)

    return _Adapter


class TestIngestToQueryFlow:
    """End-to-end: ingest fixture file → API queries return correct data."""

    def test_ingest_list_get_stats(self, tmp_path):
        """Full round-trip: ingest → list → get_conversation → get_stats."""
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        stats = ingest_all(conn, [_make_file_adapter(dest)])
        assert stats.files_ingested == 1
        conn.close()

        # list_conversations should find the ingested conversation
        conversations = list_conversations(db_path=db_path)
        assert len(conversations) == 1
        conv_summary = conversations[0]
        assert conv_summary.prompt_count >= 1
        assert conv_summary.response_count >= 1

        # get_conversation should return exchanges
        detail = get_conversation(conv_summary.id, db_path=db_path)
        assert detail is not None
        assert len(detail.exchanges) > 0
        assert detail.total_input_tokens > 0 or detail.total_output_tokens > 0

        # get_stats should reflect counts
        db_stats = get_stats(db_path=db_path)
        assert db_stats.counts.conversations == 1
        assert db_stats.counts.prompts >= 1


class TestStoreConversationRoundTrip:
    """store_conversation → read back via API, verify all fields."""

    def test_rich_conversation_round_trips(self, tmp_path):
        """Store a conversation with prompts, responses, tool calls, usage, and verify."""
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        conversation = Conversation(
            external_id="round-trip-1",
            workspace_path="/my/workspace",
            started_at="2024-06-15T10:00:00Z",
            ended_at="2024-06-15T10:30:00Z",
            harness=Harness(name="claude_code", source="anthropic", log_format="jsonl"),
            prompts=[
                Prompt(
                    external_id="p1",
                    timestamp="2024-06-15T10:00:00Z",
                    content=[ContentBlock(block_type="text", content={"text": "Implement caching for the API"})],
                    responses=[
                        Response(
                            external_id="r1",
                            timestamp="2024-06-15T10:00:05Z",
                            model="claude-opus-4-5-20251101",
                            usage=Usage(input_tokens=500, output_tokens=1200),
                            content=[ContentBlock(block_type="text", content={"text": "I'll add Redis caching."})],
                            tool_calls=[
                                ToolCall(
                                    tool_name="Read",
                                    external_id="tc1",
                                    input={"path": "/src/api.py"},
                                    result={"content": "file contents..."},
                                    status="success",
                                    timestamp="2024-06-15T10:00:06Z",
                                ),
                                ToolCall(
                                    tool_name="Write",
                                    external_id="tc2",
                                    input={"path": "/src/cache.py"},
                                    result={"success": True},
                                    status="success",
                                    timestamp="2024-06-15T10:00:07Z",
                                ),
                            ],
                        ),
                    ],
                ),
                Prompt(
                    external_id="p2",
                    timestamp="2024-06-15T10:15:00Z",
                    content=[ContentBlock(block_type="text", content={"text": "Now add tests"})],
                    responses=[
                        Response(
                            external_id="r2",
                            timestamp="2024-06-15T10:15:05Z",
                            model="claude-opus-4-5-20251101",
                            usage=Usage(input_tokens=800, output_tokens=600),
                            content=[ContentBlock(block_type="text", content={"text": "Adding test coverage."})],
                        ),
                    ],
                ),
            ],
        )

        store_conversation(conn, conversation, commit=True)
        conn.close()

        # Read back via API
        conversations = list_conversations(db_path=db_path)
        assert len(conversations) == 1

        summary = conversations[0]
        assert summary.workspace_path == "/my/workspace"
        assert summary.prompt_count == 2
        assert summary.response_count == 2
        assert summary.total_tokens == 500 + 1200 + 800 + 600

        detail = get_conversation(summary.id, db_path=db_path)
        assert len(detail.exchanges) == 2
        assert detail.total_input_tokens == 1300
        assert detail.total_output_tokens == 1800

        # First exchange should have tool calls
        first_exchange = detail.exchanges[0]
        assert "caching" in first_exchange.prompt_text.lower()
        assert len(first_exchange.tool_calls) == 2


class TestFTS5SearchIntegration:
    """store_conversation → rebuild_fts_index → search_content."""

    def test_search_returns_stored_content(self, tmp_path):
        """Content stored via store_conversation is findable via FTS5 search."""
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        conversation = make_conversation(
            prompt_text="How do I implement authentication with JWT tokens?",
            response_text="You can use PyJWT library for JSON Web Token authentication.",
        )

        store_conversation(conn, conversation, commit=True)
        rebuild_fts_index(conn)
        conn.commit()

        results = search_content(conn, "authentication JWT")
        assert len(results) > 0
        assert any("authentication" in r["snippet"].lower() or "jwt" in r["snippet"].lower() for r in results)

        conn.close()

    def test_search_no_matches(self, tmp_path):
        """Search for term not in any conversation returns empty."""
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        conversation = make_conversation(
            prompt_text="Hello world",
            response_text="Hi there",
        )

        store_conversation(conn, conversation, commit=True)
        rebuild_fts_index(conn)
        conn.commit()

        results = search_content(conn, "xyznonexistentquery")
        assert results == []

        conn.close()


class TestCascadeDelete:
    """Test ON DELETE CASCADE behavior for conversation deletion."""

    def test_delete_conversation_cascades_to_children(self, tmp_path):
        """Deleting a conversation removes all child records via CASCADE."""
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        # Create a conversation with prompts, responses, tool calls, content
        conversation = Conversation(
            external_id="cascade-test-1",
            workspace_path="/test/project",
            started_at="2024-06-15T10:00:00Z",
            harness=Harness(name="test_harness", source="test", log_format="jsonl"),
            prompts=[
                Prompt(
                    external_id="p1",
                    timestamp="2024-06-15T10:00:00Z",
                    content=[ContentBlock(block_type="text", content={"text": "Test prompt"})],
                    responses=[
                        Response(
                            external_id="r1",
                            timestamp="2024-06-15T10:00:05Z",
                            model="test-model",
                            usage=Usage(input_tokens=100, output_tokens=50),
                            content=[ContentBlock(block_type="text", content={"text": "Test response"})],
                            tool_calls=[
                                ToolCall(
                                    tool_name="Read",
                                    external_id="tc1",
                                    input={"path": "/test.py"},
                                    result={"content": "..."},
                                    status="success",
                                    timestamp="2024-06-15T10:00:06Z",
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )

        conv_id = store_conversation(conn, conversation, commit=True)

        # Add a tag to the conversation
        tag_id = get_or_create_tag(conn, "test:cascade")
        apply_tag(conn, "conversation", conv_id, tag_id)

        # Record an ingested file
        record_ingested_file(conn, "/test/file.jsonl", "abc123", conv_id)
        conn.commit()

        # Verify data exists
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM prompt_content").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM response_content").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversation_tags").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 1

        # Delete the conversation
        delete_conversation(conn, conv_id)
        conn.commit()

        # All child records should be gone
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM prompt_content").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM response_content").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM conversation_tags").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0] == 0

        # FTS should also be cleaned up
        assert conn.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0] == 0

        # But the tag itself should still exist (only junction removed)
        assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1

        conn.close()

    def test_delete_one_conversation_preserves_others(self, tmp_path):
        """Deleting one conversation doesn't affect other conversations."""
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        # Create two conversations
        conv1 = make_conversation(external_id="conv1", prompt_text="First conversation")
        conv2 = make_conversation(external_id="conv2", prompt_text="Second conversation")

        conv1_id = store_conversation(conn, conv1, commit=True)
        conv2_id = store_conversation(conn, conv2, commit=True)

        # Verify both exist
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 2

        # Delete first conversation
        delete_conversation(conn, conv1_id)
        conn.commit()

        # Second should still exist
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1
        remaining = conn.execute("SELECT id FROM conversations").fetchone()[0]
        assert remaining == conv2_id

        conn.close()

    def test_direct_sql_delete_cascades(self, tmp_path):
        """Direct SQL DELETE on conversations triggers CASCADE."""
        db_path = tmp_path / "test.db"
        conn = create_database(db_path)

        conversation = make_conversation(
            external_id="direct-delete",
            tool_calls=[
                ToolCall(
                    tool_name="Bash",
                    external_id="tc1",
                    input={"command": "ls"},
                    result={"output": "..."},
                    status="success",
                    timestamp="2024-01-01T10:00:00Z",
                ),
            ],
        )

        conv_id = store_conversation(conn, conversation, commit=True)

        # Verify data exists
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1

        # Direct SQL delete (bypassing delete_conversation function)
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()

        # CASCADE should have cleaned up children
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0

        conn.close()


class TestEnsureTablesCascade:
    """Test that ensure_* functions create tables with proper ON DELETE CASCADE.

    This tests the upgrade path where tables are created via ensure_* functions
    rather than via schema.sql. The ensure_* functions must define CASCADE
    constraints that match schema.sql.
    """

    def test_ensure_tool_call_tags_has_cascade(self, tmp_path):
        """tool_call_tags created via ensure_* cascades on tool_call deletion.

        Simulates the upgrade path:
        1. DB exists without tool_call_tags table
        2. ensure_tool_call_tags_table() creates it
        3. Data inserted, then parent deleted
        4. CASCADE should remove child records without FK error
        """
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_model,
            get_or_create_tool,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
            open_database,
        )
        from siftd.storage.tags import apply_tag, get_or_create_tag

        db_path = tmp_path / "upgrade_test.db"
        conn = open_database(db_path)

        # Set up parent records
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "test-model")
        tool_id = get_or_create_tool(conn, "Bash")
        tag_id = get_or_create_tag(conn, "test:cascade")

        # Create conversation → prompt → response → tool_call chain
        conv_id = insert_conversation(
            conn, external_id="cascade-test", harness_id=harness_id,
            workspace_id=workspace_id, started_at="2024-01-01T00:00:00Z",
        )
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        response_id = insert_response(
            conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-01T00:00:01Z",
            input_tokens=10, output_tokens=5,
        )
        tool_call_id = insert_tool_call(
            conn, response_id, conv_id, tool_id, "tc1",
            '{"command": "ls"}', '{"output": "..."}', "success", "2024-01-01T00:00:02Z",
        )

        # Apply tag to tool_call (uses tool_call_tags table)
        apply_tag(conn, "tool_call", tool_call_id, tag_id)
        conn.commit()

        # Verify tool_call_tag exists
        assert conn.execute("SELECT COUNT(*) FROM tool_call_tags").fetchone()[0] == 1

        # Delete the conversation (should cascade to tool_calls → tool_call_tags)
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()

        # tool_call_tags should be gone via CASCADE, no FK error
        assert conn.execute("SELECT COUNT(*) FROM tool_call_tags").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0

        # Tag itself should still exist (only junction record removed)
        assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1

        conn.close()

    def test_ensure_tool_call_tags_cascades_on_tag_deletion(self, tmp_path):
        """tool_call_tags created via ensure_* cascades on tag deletion."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_model,
            get_or_create_tool,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
            open_database,
        )
        from siftd.storage.tags import apply_tag, get_or_create_tag

        db_path = tmp_path / "upgrade_test2.db"
        conn = open_database(db_path)

        # Set up data
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "test-model")
        tool_id = get_or_create_tool(conn, "Bash")
        tag_id = get_or_create_tag(conn, "deleteme:tag")

        conv_id = insert_conversation(
            conn, external_id="tag-cascade", harness_id=harness_id,
            workspace_id=workspace_id, started_at="2024-01-01T00:00:00Z",
        )
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        response_id = insert_response(
            conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-01T00:00:01Z",
            input_tokens=10, output_tokens=5,
        )
        tool_call_id = insert_tool_call(
            conn, response_id, conv_id, tool_id, "tc1",
            '{"command": "ls"}', '{"output": "..."}', "success", "2024-01-01T00:00:02Z",
        )

        apply_tag(conn, "tool_call", tool_call_id, tag_id)
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM tool_call_tags").fetchone()[0] == 1

        # Delete the tag (should cascade to tool_call_tags)
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        conn.commit()

        # tool_call_tags junction should be gone, tool_call remains
        assert conn.execute("SELECT COUNT(*) FROM tool_call_tags").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1

        conn.close()
