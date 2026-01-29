"""End-to-end integration tests.

These test the full flow: parse fixture → ingest → query → detail.
Each test creates an isolated database and runs real adapters.
"""

from pathlib import Path

from conftest import FIXTURES_DIR, make_conversation

from siftd.adapters import claude_code
from siftd.api import get_conversation, get_stats, list_conversations
from siftd.domain.models import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall, Usage
from siftd.domain.source import Source
from siftd.ingestion.orchestration import ingest_all
from siftd.storage.sqlite import (
    create_database,
    open_database,
    rebuild_fts_index,
    search_content,
    store_conversation,
)


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
