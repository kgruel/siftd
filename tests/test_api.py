"""Tests for the public API module."""

from pathlib import Path

import pytest

from conftest import make_conversation

from siftd.api import (
    ConversationDetail,
    ConversationSummary,
    DatabaseStats,
    TagUsage,
    WorkspaceTagUsage,
    get_conversation,
    get_stats,
    get_tool_tag_summary,
    get_tool_tags_by_workspace,
    list_conversations,
)
from siftd.api.search import ConversationScore, aggregate_by_conversation, first_mention
from siftd.search import SearchResult


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


class TestListConversationsToolTag:
    def test_filter_by_tool_tag(self, test_db_with_tool_tags):
        conversations = list_conversations(db_path=test_db_with_tool_tags, tool_tag="shell:test")

        assert len(conversations) == 2  # conv1 and conv3 have shell:test

    def test_filter_by_different_tool_tag(self, test_db_with_tool_tags):
        conversations = list_conversations(db_path=test_db_with_tool_tags, tool_tag="shell:vcs")

        assert len(conversations) == 1  # only conv2 has shell:vcs

    def test_no_matches_for_unknown_tag(self, test_db_with_tool_tags):
        conversations = list_conversations(db_path=test_db_with_tool_tags, tool_tag="shell:unknown")

        assert len(conversations) == 0

    def test_tool_tag_combines_with_workspace_filter(self, test_db_with_tool_tags):
        conversations = list_conversations(
            db_path=test_db_with_tool_tags,
            tool_tag="shell:test",
            workspace="other",
        )

        assert len(conversations) == 1  # only conv3 matches both

    def test_filter_by_tool_tag_prefix(self, test_db_with_tool_tags):
        conversations = list_conversations(db_path=test_db_with_tool_tags, tool_tag="shell:")

        assert len(conversations) == 3  # all conversations have shell:* tags

    def test_tool_tag_prefix_no_matches(self, test_db_with_tool_tags):
        conversations = list_conversations(db_path=test_db_with_tool_tags, tool_tag="other:")

        assert len(conversations) == 0


class TestGetToolTagSummary:
    def test_returns_tag_counts(self, test_db_with_tool_tags):
        tags = get_tool_tag_summary(db_path=test_db_with_tool_tags)

        assert len(tags) == 2
        assert all(isinstance(t, TagUsage) for t in tags)

    def test_sorted_by_count_descending(self, test_db_with_tool_tags):
        tags = get_tool_tag_summary(db_path=test_db_with_tool_tags)

        assert tags[0].name == "shell:test"
        assert tags[0].count == 2
        assert tags[1].name == "shell:vcs"
        assert tags[1].count == 1

    def test_respects_prefix_filter(self, test_db_with_tool_tags):
        tags = get_tool_tag_summary(db_path=test_db_with_tool_tags, prefix="other:")

        assert len(tags) == 0

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_tool_tag_summary(db_path=tmp_path / "nonexistent.db")


class TestGetToolTagsByWorkspace:
    def test_returns_workspace_breakdown(self, test_db_with_tool_tags):
        results = get_tool_tags_by_workspace(db_path=test_db_with_tool_tags)

        assert len(results) == 2
        assert all(isinstance(r, WorkspaceTagUsage) for r in results)

    def test_sorted_by_total_descending(self, test_db_with_tool_tags):
        results = get_tool_tags_by_workspace(db_path=test_db_with_tool_tags)

        assert results[0].total >= results[1].total

    def test_includes_tag_breakdown(self, test_db_with_tool_tags):
        results = get_tool_tags_by_workspace(db_path=test_db_with_tool_tags)

        ws_with_both = [r for r in results if r.total == 2][0]
        tag_names = [t.name for t in ws_with_both.tags]
        assert "shell:test" in tag_names
        assert "shell:vcs" in tag_names

    def test_respects_limit(self, test_db_with_tool_tags):
        results = get_tool_tags_by_workspace(db_path=test_db_with_tool_tags, limit=1)

        assert len(results) == 1

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_tool_tags_by_workspace(db_path=tmp_path / "nonexistent.db")


class TestIngestTimeShellTagging:
    """Test that shell commands are automatically tagged at ingest time."""

    def test_shell_execute_tagged_at_ingest(self, tmp_path):
        """store_conversation() auto-tags shell.execute calls with categorizable commands."""
        from siftd.domain.models import ToolCall
        from siftd.storage.sqlite import create_database, store_conversation

        db_path = tmp_path / "test_ingest_tags.db"
        conn = create_database(db_path)

        conversation = make_conversation(
            tool_calls=[
                ToolCall(
                    tool_name="shell.execute",
                    external_id="tc1",
                    input={"command": "pytest tests/"},
                    result={"output": "OK"},
                    status="success",
                    timestamp="2024-01-01T10:00:01Z",
                ),
            ],
        )

        store_conversation(conn, conversation, commit=True)

        cur = conn.execute("""
            SELECT t.name
            FROM tool_call_tags tct
            JOIN tags t ON t.id = tct.tag_id
            JOIN tool_calls tc ON tc.id = tct.tool_call_id
        """)
        tags = [row["name"] for row in cur.fetchall()]

        assert "shell:test" in tags

    def test_uncategorized_command_not_tagged(self, tmp_path):
        """Commands that don't match any category are not tagged."""
        from siftd.domain.models import ToolCall
        from siftd.storage.sqlite import create_database, store_conversation

        db_path = tmp_path / "test_no_tags.db"
        conn = create_database(db_path)

        conversation = make_conversation(
            external_id="test-conv-2",
            tool_calls=[
                ToolCall(
                    tool_name="shell.execute",
                    external_id="tc1",
                    input={"command": "myunknowncommand --flag"},
                    result={"output": "hello"},
                    status="success",
                    timestamp="2024-01-01T10:00:01Z",
                ),
            ],
        )

        store_conversation(conn, conversation, commit=True)

        cur = conn.execute("SELECT COUNT(*) as cnt FROM tool_call_tags")
        count = cur.fetchone()["cnt"]

        assert count == 0
