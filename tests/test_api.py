"""Tests for the public API module."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from tbd.api import (
    ConversationDetail,
    ConversationSummary,
    DatabaseStats,
    get_conversation,
    get_stats,
    list_conversations,
)
from tbd.api.search import ConversationScore, aggregate_by_conversation, first_mention
from tbd.search import SearchResult
from tbd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)


@pytest.fixture
def test_db(tmp_path):
    """Create a test database with sample data."""
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    # Create harness and workspace
    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "claude-3-opus-20240229")

    # Create conversations
    conv1_id = insert_conversation(
        conn,
        external_id="conv1",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )

    conv2_id = insert_conversation(
        conn,
        external_id="conv2",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-16T10:00:00Z",
    )

    # Add prompts and responses for conv1
    prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, prompt1_id, 0, "text", '{"text": "Hello, how are you?"}')

    response1_id = insert_response(
        conn, conv1_id, prompt1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50
    )
    insert_response_content(conn, response1_id, 0, "text", '{"text": "I am doing well, thank you!"}')

    # Add prompts and responses for conv2
    prompt2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
    insert_prompt_content(conn, prompt2_id, 0, "text", '{"text": "What is Python?"}')

    response2_id = insert_response(
        conn, conv2_id, prompt2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
        input_tokens=200, output_tokens=150
    )
    insert_response_content(conn, response2_id, 0, "text", '{"text": "Python is a programming language."}')

    conn.commit()
    conn.close()

    return db_path


class TestGetStats:
    def test_returns_database_stats(self, test_db):
        stats = get_stats(db_path=test_db)

        assert isinstance(stats, DatabaseStats)
        assert stats.db_path == test_db
        assert stats.db_size_bytes > 0

    def test_counts_are_correct(self, test_db):
        stats = get_stats(db_path=test_db)

        assert stats.counts.conversations == 2
        assert stats.counts.prompts == 2
        assert stats.counts.responses == 2
        assert stats.counts.harnesses == 1
        assert stats.counts.workspaces == 1
        assert stats.counts.models == 1

    def test_harnesses_populated(self, test_db):
        stats = get_stats(db_path=test_db)

        assert len(stats.harnesses) == 1
        assert stats.harnesses[0].name == "test_harness"
        assert stats.harnesses[0].source == "test"

    def test_workspaces_populated(self, test_db):
        stats = get_stats(db_path=test_db)

        assert len(stats.top_workspaces) == 1
        assert stats.top_workspaces[0].path == "/test/project"
        assert stats.top_workspaces[0].conversation_count == 2

    def test_models_populated(self, test_db):
        stats = get_stats(db_path=test_db)

        assert len(stats.models) == 1
        assert "claude-3-opus" in stats.models[0]

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_stats(db_path=tmp_path / "nonexistent.db")


class TestListConversations:
    def test_returns_conversations(self, test_db):
        conversations = list_conversations(db_path=test_db)

        assert len(conversations) == 2
        assert all(isinstance(c, ConversationSummary) for c in conversations)

    def test_default_sort_newest_first(self, test_db):
        conversations = list_conversations(db_path=test_db)

        # Should be sorted by started_at descending
        assert conversations[0].started_at > conversations[1].started_at

    def test_oldest_first_sort(self, test_db):
        conversations = list_conversations(db_path=test_db, oldest_first=True)

        assert conversations[0].started_at < conversations[1].started_at

    def test_limit_parameter(self, test_db):
        conversations = list_conversations(db_path=test_db, limit=1)

        assert len(conversations) == 1

    def test_workspace_filter(self, test_db):
        conversations = list_conversations(db_path=test_db, workspace="project")
        assert len(conversations) == 2

        conversations = list_conversations(db_path=test_db, workspace="nonexistent")
        assert len(conversations) == 0

    def test_model_filter(self, test_db):
        conversations = list_conversations(db_path=test_db, model="opus")
        assert len(conversations) == 2

        conversations = list_conversations(db_path=test_db, model="haiku")
        assert len(conversations) == 0

    def test_since_filter(self, test_db):
        conversations = list_conversations(db_path=test_db, since="2024-01-16")
        assert len(conversations) == 1
        assert "2024-01-16" in conversations[0].started_at

    def test_before_filter(self, test_db):
        conversations = list_conversations(db_path=test_db, before="2024-01-16")
        assert len(conversations) == 1
        assert "2024-01-15" in conversations[0].started_at

    def test_conversation_summary_fields(self, test_db):
        conversations = list_conversations(db_path=test_db, limit=1)
        conv = conversations[0]

        assert conv.id is not None
        assert conv.workspace_path == "/test/project"
        assert conv.model is not None
        assert conv.started_at is not None
        assert conv.prompt_count == 1
        assert conv.response_count == 1
        assert conv.total_tokens > 0

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            list_conversations(db_path=tmp_path / "nonexistent.db")


class TestGetConversation:
    def test_returns_conversation_detail(self, test_db):
        # First get a conversation ID
        conversations = list_conversations(db_path=test_db, limit=1)
        conv_id = conversations[0].id

        detail = get_conversation(conv_id, db_path=test_db)

        assert isinstance(detail, ConversationDetail)
        assert detail.id == conv_id

    def test_supports_prefix_match(self, test_db):
        conversations = list_conversations(db_path=test_db, limit=1)
        conv_id = conversations[0].id
        # Use enough prefix characters to be unique
        prefix = conv_id[:12]

        detail = get_conversation(prefix, db_path=test_db)

        assert detail is not None
        assert detail.id == conv_id

    def test_returns_none_for_missing(self, test_db):
        detail = get_conversation("nonexistent_id", db_path=test_db)
        assert detail is None

    def test_detail_has_exchanges(self, test_db):
        conversations = list_conversations(db_path=test_db, limit=1)
        detail = get_conversation(conversations[0].id, db_path=test_db)

        assert len(detail.exchanges) > 0
        exchange = detail.exchanges[0]
        assert exchange.prompt_text is not None or exchange.response_text is not None

    def test_detail_token_counts(self, test_db):
        conversations = list_conversations(db_path=test_db, limit=1)
        detail = get_conversation(conversations[0].id, db_path=test_db)

        assert detail.total_input_tokens > 0
        assert detail.total_output_tokens > 0

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_conversation("some_id", db_path=tmp_path / "nonexistent.db")


class TestAggregateByConversation:
    def test_groups_by_conversation(self):
        results = [
            SearchResult("conv1", 0.9, "text1", "prompt", "/ws", "2024-01-01"),
            SearchResult("conv1", 0.8, "text2", "response", "/ws", "2024-01-01"),
            SearchResult("conv2", 0.7, "text3", "prompt", "/ws", "2024-01-02"),
        ]

        aggregated = aggregate_by_conversation(results)

        assert len(aggregated) == 2
        assert all(isinstance(c, ConversationScore) for c in aggregated)

    def test_calculates_max_score(self):
        results = [
            SearchResult("conv1", 0.9, "text1", "prompt", "/ws", "2024-01-01"),
            SearchResult("conv1", 0.8, "text2", "response", "/ws", "2024-01-01"),
        ]

        aggregated = aggregate_by_conversation(results)

        assert aggregated[0].max_score == 0.9

    def test_calculates_mean_score(self):
        results = [
            SearchResult("conv1", 0.9, "text1", "prompt", "/ws", "2024-01-01"),
            SearchResult("conv1", 0.7, "text2", "response", "/ws", "2024-01-01"),
        ]

        aggregated = aggregate_by_conversation(results)

        assert aggregated[0].mean_score == 0.8

    def test_sorts_by_max_score_descending(self):
        results = [
            SearchResult("conv1", 0.7, "text1", "prompt", "/ws", "2024-01-01"),
            SearchResult("conv2", 0.9, "text2", "prompt", "/ws", "2024-01-02"),
        ]

        aggregated = aggregate_by_conversation(results)

        assert aggregated[0].conversation_id == "conv2"
        assert aggregated[1].conversation_id == "conv1"

    def test_respects_limit(self):
        results = [
            SearchResult("conv1", 0.9, "text1", "prompt", "/ws", "2024-01-01"),
            SearchResult("conv2", 0.8, "text2", "prompt", "/ws", "2024-01-02"),
            SearchResult("conv3", 0.7, "text3", "prompt", "/ws", "2024-01-03"),
        ]

        aggregated = aggregate_by_conversation(results, limit=2)

        assert len(aggregated) == 2

    def test_empty_results(self):
        aggregated = aggregate_by_conversation([])
        assert aggregated == []

    def test_includes_best_excerpt(self):
        results = [
            SearchResult("conv1", 0.9, "best text", "prompt", "/ws", "2024-01-01"),
            SearchResult("conv1", 0.7, "other text", "response", "/ws", "2024-01-01"),
        ]

        aggregated = aggregate_by_conversation(results)

        assert aggregated[0].best_excerpt == "best text"


class TestFirstMention:
    def test_returns_earliest_above_threshold(self, test_db):
        results = [
            SearchResult("conv1", 0.9, "text1", "prompt", "/ws", "2024-01-02"),
            SearchResult("conv2", 0.8, "text2", "prompt", "/ws", "2024-01-01"),
        ]

        # Need to use conversation IDs from the test DB
        conversations = list_conversations(db_path=test_db)
        results = [
            SearchResult(conversations[0].id, 0.9, "text1", "prompt", "/ws", conversations[0].started_at),
            SearchResult(conversations[1].id, 0.8, "text2", "prompt", "/ws", conversations[1].started_at),
        ]

        earliest = first_mention(results, threshold=0.65, db_path=test_db)

        assert earliest is not None
        # Earlier conversation should be returned
        assert earliest.conversation_id == conversations[1].id

    def test_returns_none_below_threshold(self, test_db):
        conversations = list_conversations(db_path=test_db)
        results = [
            SearchResult(conversations[0].id, 0.5, "text1", "prompt", "/ws", "2024-01-01"),
        ]

        earliest = first_mention(results, threshold=0.7, db_path=test_db)

        assert earliest is None

    def test_empty_results(self, test_db):
        earliest = first_mention([], threshold=0.65, db_path=test_db)
        assert earliest is None
