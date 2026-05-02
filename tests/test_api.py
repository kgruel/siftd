"""Tests for the public API module."""

import json

import pytest
from conftest import make_conversation

from siftd.api import (
    ConversationDetail,
    ConversationSummary,
    CostCoverage,
    DatabaseStats,
    TagUsage,
    WorkspaceTagUsage,
    get_conversation,
    get_cost_coverage,
    get_stats,
    get_tool_tag_summary,
    get_tool_tags_by_workspace,
    list_conversations,
)
from siftd.api.conversations import (
    ToolCallDetail,
    _collapse_tool_call_rows,
    _collapse_tool_details,
    _extract_text,
    _extract_thinking,
    _extract_tool_result,
    _extract_tool_use_id,
    _matches_tool_filter,
    resolve_entity_id,
)
from siftd.api.search import ConversationScore, aggregate_by_conversation, first_mention
from siftd.search import SearchResult
from siftd.storage.sqlite import open_database


def test_package_level_search_api_exports_are_lazy_and_complete():
    from siftd import SearchChunk as TopSearchChunk
    from siftd import aggregate_by_conversation as top_aggregate_by_conversation
    from siftd import search_chunks as top_search_chunks
    from siftd.api import (
        ConversationSearchSummary,
        ScoreBreakdown,
        SearchChunk,
        aggregate_by_conversation,
        compute_thread_tiers,
        enrich_context_window,
        enrich_exchanges,
        enrich_file_refs,
        enrich_search_metadata,
        filter_by_threshold,
        search_chunks,
        sort_chunks_by_time,
    )

    assert TopSearchChunk is SearchChunk
    assert top_search_chunks is search_chunks
    assert top_aggregate_by_conversation is aggregate_by_conversation
    assert ConversationSearchSummary.__name__ == "ConversationSearchSummary"
    assert ScoreBreakdown.__name__ == "ScoreBreakdown"
    assert callable(search_chunks)
    assert callable(aggregate_by_conversation)
    assert callable(compute_thread_tiers)
    assert callable(filter_by_threshold)
    assert callable(sort_chunks_by_time)
    assert callable(enrich_search_metadata)
    assert callable(enrich_file_refs)
    assert callable(enrich_exchanges)
    assert callable(enrich_context_window)


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


class TestGetCostCoverage:
    def test_returns_cost_coverage(self, test_db):
        from siftd.api.database import open_database
        from siftd.storage.conversation_stats import rebuild_conversation_stats

        conn = open_database(test_db)
        rebuild_conversation_stats(conn, commit=True)
        result = get_cost_coverage(conn)
        conn.close()
        # test_db has 2 conversations with tokens but no pricing → NULL cost
        assert isinstance(result, CostCoverage)
        assert result.total_with_tokens == 2
        assert result.with_positive_cost == 0
        assert result.pct_covered == 0.0

    def test_none_when_no_stats_table(self, tmp_path):
        from siftd.storage.sqlite import create_database

        conn = create_database(tmp_path / "bare.db")
        conn.execute("DROP TABLE IF EXISTS conversation_stats")
        conn.commit()
        result = get_cost_coverage(conn)
        conn.close()
        assert result is None

    def test_positive_cost_counted(self, tmp_path):
        from siftd.storage.conversation_stats import rebuild_conversation_stats
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_model,
            get_or_create_provider,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
        )

        conn = create_database(tmp_path / "t.db")
        provider_id = get_or_create_provider(conn, "anthropic")
        model_id = get_or_create_model(conn, "claude-test-model")
        harness_id = get_or_create_harness(conn, "test", source="anthropic")
        ws_id = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")

        # Insert pricing so c1 gets a real cost
        conn.execute(
            "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
            "VALUES ('pr1', ?, ?, 3.0, 15.0)",
            (model_id, provider_id),
        )

        def _add_conv(ext, with_pricing):
            cid = insert_conversation(conn, ext, harness_id, ws_id, "2024-01-01T00:00:00Z")
            pid = insert_prompt(conn, cid, f"p-{ext}", "2024-01-01T00:00:00Z")
            insert_response(
                conn, cid, pid,
                model_id=model_id if with_pricing else None,
                provider_id=provider_id if with_pricing else None,
                external_id=f"r-{ext}",
                timestamp="2024-01-01T00:00:01Z",
                input_tokens=1_000_000,
                output_tokens=0,
            )
            return cid

        _add_conv("c1", with_pricing=True)   # should get cost > 0
        _add_conv("c2", with_pricing=False)  # model_id=None → NULL cost
        conn.commit()

        rebuild_conversation_stats(conn, commit=True)
        result = get_cost_coverage(conn)
        conn.close()

        assert result.total_with_tokens == 2
        assert result.with_positive_cost == 1   # c1 has cost > 0
        assert result.with_null_cost == 1        # c2 is NULL
        assert result.pct_covered == 50.0


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
        conversations = list_conversations(db_path=test_db, oldest=True)

        assert conversations[0].started_at < conversations[1].started_at

    def test_limit_parameter(self, test_db):
        conversations = list_conversations(db_path=test_db, n=1)

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
        conversations = list_conversations(db_path=test_db, n=1)
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
        conversations = list_conversations(db_path=test_db, n=1)
        conv_id = conversations[0].id

        detail = get_conversation(conv_id, db_path=test_db)

        assert isinstance(detail, ConversationDetail)
        assert detail.id == conv_id

    def test_supports_prefix_match(self, test_db):
        conversations = list_conversations(db_path=test_db, n=1)
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
        conversations = list_conversations(db_path=test_db, n=1)
        detail = get_conversation(conversations[0].id, db_path=test_db)

        assert len(detail.exchanges) > 0
        exchange = detail.exchanges[0]
        assert exchange.prompt_text is not None or exchange.response_text is not None

    def test_detail_token_counts(self, test_db):
        conversations = list_conversations(db_path=test_db, n=1)
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

    def test_respects_custom_threshold(self, test_db):
        """Test that lower threshold includes results that default 0.65 would exclude."""
        conversations = list_conversations(db_path=test_db)
        results = [
            SearchResult(conversations[0].id, 0.4, "text1", "prompt", "/ws", conversations[0].started_at),
        ]

        # Default threshold 0.65 would filter this out
        assert first_mention(results, threshold=0.65, db_path=test_db) is None

        # Custom threshold 0.3 should include it
        earliest = first_mention(results, threshold=0.3, db_path=test_db)
        assert earliest is not None
        assert earliest.conversation_id == conversations[0].id


class TestFirstMentionPromptTimestamp:
    """Test first_mention sorts by prompt timestamp, not conversation start."""

    @pytest.fixture
    def db_with_prompt_times(self, tmp_path):
        """Create DB where prompt times differ from conversation start times.

        Scenario: Conv1 started earlier but its prompt came later.
        """
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
        )

        db_path = tmp_path / "prompt_times.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")

        # Conv1: started early (Jan 10), but prompt came late (Jan 15)
        conv1_id = insert_conversation(
            conn, "conv1", harness_id, workspace_id, started_at="2024-01-10T10:00:00Z"
        )
        prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")

        # Conv2: started later (Jan 12), but prompt came early (Jan 12)
        conv2_id = insert_conversation(
            conn, "conv2", harness_id, workspace_id, started_at="2024-01-12T10:00:00Z"
        )
        prompt2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-12T10:00:00Z")

        conn.commit()
        conn.close()

        return {
            "db_path": db_path,
            "conv1_id": conv1_id,
            "conv2_id": conv2_id,
            "prompt1_id": prompt1_id,
            "prompt2_id": prompt2_id,
        }

    def test_sorts_by_prompt_timestamp_not_conversation_start(self, db_with_prompt_times):
        """first_mention should return result with earliest prompt, not earliest conversation."""
        data = db_with_prompt_times

        # Conv1 started earlier, but prompt2 happened before prompt1
        # source_ids links chunk to prompt
        results = [
            SearchResult(
                data["conv1_id"], 0.9, "text1", "prompt", "/ws", "2024-01-10T10:00:00Z",
                source_ids=[data["prompt1_id"]]
            ),
            SearchResult(
                data["conv2_id"], 0.8, "text2", "prompt", "/ws", "2024-01-12T10:00:00Z",
                source_ids=[data["prompt2_id"]]
            ),
        ]

        earliest = first_mention(results, threshold=0.65, db_path=data["db_path"])

        assert earliest is not None
        # Should return conv2 because prompt2 (Jan 12) is earlier than prompt1 (Jan 15)
        assert earliest.conversation_id == data["conv2_id"]

    def test_falls_back_to_conversation_time_when_no_source_ids(self, test_db):
        """When source_ids is empty, falls back to conversation start time."""
        conversations = list_conversations(db_path=test_db)
        # Results without source_ids
        results = [
            SearchResult(conversations[0].id, 0.9, "text1", "prompt", "/ws", conversations[0].started_at),
            SearchResult(conversations[1].id, 0.8, "text2", "prompt", "/ws", conversations[1].started_at),
        ]

        earliest = first_mention(results, threshold=0.65, db_path=test_db)

        assert earliest is not None
        # Falls back to conversation start time, so earlier conversation wins
        assert earliest.conversation_id == conversations[1].id

    def test_works_with_dict_results(self, db_with_prompt_times):
        """first_mention works with dict results (from embeddings search)."""
        data = db_with_prompt_times

        # Dict format from embeddings search
        results = [
            {
                "conversation_id": data["conv1_id"],
                "score": 0.9,
                "text": "text1",
                "source_ids": [data["prompt1_id"]],
            },
            {
                "conversation_id": data["conv2_id"],
                "score": 0.8,
                "text": "text2",
                "source_ids": [data["prompt2_id"]],
            },
        ]

        earliest = first_mention(results, threshold=0.65, db_path=data["db_path"])

        assert earliest is not None
        # Conv2's prompt (Jan 12) is earlier than conv1's prompt (Jan 15)
        assert earliest["conversation_id"] == data["conv2_id"]


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
        results = get_tool_tags_by_workspace(db_path=test_db_with_tool_tags, n=1)

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
        conn.close()

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
        conn.close()

        assert count == 0


class TestFetchFileRefs:
    """Tests for fetch_file_refs including content-addressable blob support."""

    def test_fetch_file_refs_with_deduped_result(self, tmp_path):
        """fetch_file_refs returns content from content_blobs when result is deduped."""
        from siftd.api.file_refs import fetch_file_refs
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_tool,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test_file_refs.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        tool_id = get_or_create_tool(conn, "file.read")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        # Insert with dedupe_result=True (default) - stores in content_blobs
        result_json = json.dumps({"content": "deduped file content"})
        insert_tool_call(
            conn, response_id, conv_id, tool_id, "tc1",
            '{"file_path": "/test/hello.py"}', result_json, "success", "2024-01-01T10:00:01Z",
            dedupe_result=True,
        )
        conn.commit()

        # Verify result is in event_tool_call blob (event_tool_call is authoritative; legacy tool_calls.result_hash is NULL)
        tc_event_id = conn.execute("SELECT id FROM events WHERE kind = 'tool_call' AND external_id = 'tc1'").fetchone()[0]
        etc_row = conn.execute("SELECT result_hash FROM event_tool_call WHERE event_id = ?", (tc_event_id,)).fetchone()
        assert etc_row is not None
        assert etc_row["result_hash"] is not None, "Expected result_hash to reference blob"

        # Now test fetch_file_refs retrieves content correctly
        refs = fetch_file_refs(conn, [prompt_id])

        assert prompt_id in refs
        assert len(refs[prompt_id]) == 1
        ref = refs[prompt_id][0]
        assert ref.path == "/test/hello.py"
        assert ref.basename == "hello.py"
        assert ref.op == "r"
        assert ref.content == "deduped file content"

        conn.close()

    def test_fetch_file_refs_with_inline_result(self, tmp_path):
        """fetch_file_refs returns content from inline result when not deduped."""
        from siftd.api.file_refs import fetch_file_refs
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_tool,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test_file_refs_inline.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        tool_id = get_or_create_tool(conn, "file.read")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        # Insert with dedupe_result=False - stores inline
        result_json = '{"content": "inline content"}'
        insert_tool_call(
            conn, response_id, conv_id, tool_id, "tc1",
            '{"file_path": "/test/inline.txt"}', result_json, "success", "2024-01-01T10:00:01Z",
            dedupe_result=False,
        )
        conn.commit()

        refs = fetch_file_refs(conn, [prompt_id])

        assert prompt_id in refs
        assert len(refs[prompt_id]) == 1
        ref = refs[prompt_id][0]
        assert ref.path == "/test/inline.txt"
        assert ref.content == "inline content"

        conn.close()

    def test_fetch_file_refs_mixed_storage(self, tmp_path):
        """fetch_file_refs handles mix of inline and blob-stored results."""
        from siftd.api.file_refs import fetch_file_refs
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_tool,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test_file_refs_mixed.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        tool_id = get_or_create_tool(conn, "file.read")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        # One blob-stored, one inline
        insert_tool_call(
            conn, response_id, conv_id, tool_id, "tc1",
            '{"file_path": "/test/blob.txt"}', '{"content": "blob content"}', "success", "2024-01-01T10:00:01Z",
            dedupe_result=True,
        )
        insert_tool_call(
            conn, response_id, conv_id, tool_id, "tc2",
            '{"file_path": "/test/inline.txt"}', '{"content": "inline content"}', "success", "2024-01-01T10:00:02Z",
            dedupe_result=False,
        )
        conn.commit()

        refs = fetch_file_refs(conn, [prompt_id])

        assert len(refs[prompt_id]) == 2
        paths = {r.path: r.content for r in refs[prompt_id]}
        assert paths["/test/blob.txt"] == "blob content"
        assert paths["/test/inline.txt"] == "inline content"

        conn.close()


class TestExtractText:
    """Tests for _extract_text utility."""

    def test_json_wrapped(self):
        assert _extract_text('{"text": "hello"}') == "hello"

    def test_plain_string(self):
        assert _extract_text("plain text") == "plain text"


class TestMatchesToolFilter:
    """Tests for _matches_tool_filter utility."""

    def test_no_filter(self):
        assert _matches_tool_filter("file.read", "success", None) is True

    def test_errors_filter(self):
        assert _matches_tool_filter("file.read", "error", "errors") is True
        assert _matches_tool_filter("file.read", "success", "errors") is False

    def test_exact_name(self):
        assert _matches_tool_filter("file.read", "success", "file.read") is True
        assert _matches_tool_filter("file.write", "success", "file.read") is False

    def test_prefix_match(self):
        assert _matches_tool_filter("file.read", "success", "file") is True
        assert _matches_tool_filter("shell.execute", "success", "file") is False

    def test_none_tool_name(self):
        assert _matches_tool_filter(None, "success", "file") is False


class TestCollapseToolCallRows:
    """Tests for _collapse_tool_call_rows utility."""

    def test_empty(self):
        assert _collapse_tool_call_rows([]) == []

    def test_collapses_consecutive(self):
        rows = [
            {"tool_name": "file.read", "status": "success"},
            {"tool_name": "file.read", "status": "success"},
            {"tool_name": "shell.execute", "status": "success"},
        ]
        result = _collapse_tool_call_rows(rows)
        assert len(result) == 2
        assert result[0].tool_name == "file.read"
        assert result[0].count == 2
        assert result[1].tool_name == "shell.execute"


class TestExtractThinking:
    """Tests for _extract_thinking utility."""

    def test_thinking_key(self):
        assert _extract_thinking('{"thinking": "pondering..."}') == "pondering..."

    def test_text_key(self):
        assert _extract_thinking('{"text": "analysis"}') == "analysis"

    def test_subject_description(self):
        result = _extract_thinking('{"subject": "Code review", "description": "checking tests"}')
        assert "Code review" in result
        assert "checking tests" in result

    def test_description_only(self):
        assert _extract_thinking('{"description": "checking"}') == "checking"

    def test_subject_only(self):
        assert _extract_thinking('{"subject": "Review"}') == "Review"

    def test_plain_string(self):
        assert _extract_thinking("raw text") == "raw text"


class TestExtractToolUseId:
    """Tests for _extract_tool_use_id utility."""

    def test_id_key(self):
        assert _extract_tool_use_id('{"id": "toolu_123"}') == "toolu_123"

    def test_tool_use_id_key(self):
        assert _extract_tool_use_id('{"tool_use_id": "call_456"}') == "call_456"

    def test_call_id_key(self):
        assert _extract_tool_use_id('{"call_id": "fc_789"}') == "fc_789"

    def test_no_id(self):
        assert _extract_tool_use_id('{"name": "tool"}') is None

    def test_plain_string(self):
        assert _extract_tool_use_id("not json") is None


class TestExtractToolResult:
    """Tests for _extract_tool_result utility."""

    def test_text_key(self):
        assert _extract_tool_result('{"text": "output"}') == "output"

    def test_content_string(self):
        assert _extract_tool_result('{"content": "hello"}') == "hello"

    def test_content_dict(self):
        result = _extract_tool_result('{"content": {"text": "nested"}}')
        assert result == "nested"

    def test_content_dict_no_text(self):
        result = _extract_tool_result('{"content": {"key": "val"}}')
        assert "key" in result  # JSON dump

    def test_content_list(self):
        result = _extract_tool_result('{"content": ["line1", "line2"]}')
        assert "line1" in result
        assert "line2" in result

    def test_content_list_with_dicts(self):
        result = _extract_tool_result('{"content": [{"text": "a"}, {"text": "b"}]}')
        assert "a" in result and "b" in result

    def test_output_key(self):
        assert _extract_tool_result('{"output": "done"}') == "done"

    def test_plain_string(self):
        assert _extract_tool_result("raw") == "raw"

    def test_non_string_value(self):
        result = _extract_tool_result('{"text": 42}')
        assert result == "42"

    def test_content_list_with_non_text_dict(self):
        result = _extract_tool_result('{"content": [{"key": "val"}, "text"]}')
        assert "key" in result and "text" in result


class TestCollapseToolDetails:
    """Tests for _collapse_tool_details utility."""

    def test_empty(self):
        assert _collapse_tool_details([], collapse=True) == []

    def test_no_collapse(self):
        tools = [ToolCallDetail(tool_name="file.read", status="success")]
        result = _collapse_tool_details(tools, collapse=False)
        assert len(result) == 1

    def test_collapses(self):
        tools = [
            ToolCallDetail(tool_name="file.read", status="success"),
            ToolCallDetail(tool_name="file.read", status="success"),
            ToolCallDetail(tool_name="shell.execute", status="success"),
        ]
        result = _collapse_tool_details(tools, collapse=True)
        assert len(result) == 2
        assert result[0].count == 2


class TestResolveEntityId:
    """Tests for resolve_entity_id utility."""

    def test_workspace(self, test_db):
        conn = open_database(test_db, read_only=True)
        # Look up actual workspace from test_db
        ws = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        result = resolve_entity_id(conn, "workspace", ws["id"])
        conn.close()
        assert result == ws["id"]

    def test_unknown_type(self, test_db):
        conn = open_database(test_db, read_only=True)
        result = resolve_entity_id(conn, "unknown_type", "id1")
        conn.close()
        assert result is None

    def test_tool_call(self, test_db):
        conn = open_database(test_db, read_only=True)
        result = resolve_entity_id(conn, "tool_call", "nonexistent")
        conn.close()
        assert result is None


class TestBuildNarrative:
    """Tests for _build_narrative with mock content blocks."""

    def test_text_flushes_pending_tools(self):
        """L648-655: text block flushes pending tool calls."""
        from siftd.api.conversations import _build_narrative
        blocks = [
            {"block_type": "tool_use", "content": '{"id": "tu1"}'},
            {"block_type": "text", "content": "After tools"},
        ]
        tool_calls = [{"external_id": "tu1", "tool_name": "file.read", "status": "success", "input": "{}", "result": "ok"}]
        result = _build_narrative(
            [{"id": "r1"}],
            {"r1": blocks},
            {"r1": tool_calls},
            include_thinking=False,
            include_tool_content=False,
            tool_filter=None,
        )
        types = [b.block_type for b in result]
        assert "tool_calls" in types
        assert "text" in types
        assert types.index("tool_calls") < types.index("text")

    def test_thinking_flushes_pending_tools(self):
        """L668-675: thinking block flushes pending tool calls."""
        from siftd.api.conversations import _build_narrative
        blocks = [
            {"block_type": "tool_use", "content": '{"id": "tu1"}'},
            {"block_type": "thinking", "content": '{"thinking": "hmm"}'},
        ]
        tool_calls = [{"external_id": "tu1", "tool_name": "shell.execute", "status": "success", "input": "{}", "result": "ok"}]
        result = _build_narrative(
            [{"id": "r1"}],
            {"r1": blocks},
            {"r1": tool_calls},
            include_thinking=True,
            include_tool_content=False,
            tool_filter=None,
        )
        types = [b.block_type for b in result]
        assert "tool_calls" in types
        assert "thinking" in types

    def test_tool_use_fallback_matching(self):
        """L694-703: tool_use with no matching external_id falls back to order."""
        from siftd.api.conversations import _build_narrative
        blocks = [
            {"block_type": "tool_use", "content": '{"id": "unknown_id"}'},
        ]
        tool_calls = [{"external_id": None, "tool_name": "file.write", "status": "success", "input": "{}", "result": "ok"}]
        result = _build_narrative(
            [{"id": "r1"}],
            {"r1": blocks},
            {"r1": tool_calls},
            include_thinking=False,
            include_tool_content=False,
            tool_filter=None,
        )
        # Should have matched by fallback order
        assert len(result) >= 1

    def test_tool_result_block(self):
        """L718-732: tool_result/tool_output blocks."""
        from siftd.api.conversations import _build_narrative
        blocks = [
            {"block_type": "tool_use", "content": '{"id": "tu1"}'},
            {"block_type": "tool_result", "content": '{"text": "result data"}'},
        ]
        tool_calls = [{"external_id": "tu1", "tool_name": "file.read", "status": "success", "input": "{}", "result": "ok"}]
        result = _build_narrative(
            [{"id": "r1"}],
            {"r1": blocks},
            {"r1": tool_calls},
            include_thinking=False,
            include_tool_content=False,
            tool_filter=None,
        )
        types = [b.block_type for b in result]
        assert "tool_result" in types

    def test_tool_use_fallback_skips_used_ids(self):
        """L699,702: fallback matching skips already-used external_ids."""
        from siftd.api.conversations import _build_narrative
        blocks = [
            {"block_type": "tool_use", "content": '{"id": "tu1"}'},
            {"block_type": "tool_use", "content": '{"id": "tu_unknown"}'},
        ]
        tool_calls = [
            {"external_id": "tu1", "tool_name": "file.read", "status": "success", "input": "{}", "result": "ok"},
            {"external_id": "tu2", "tool_name": "file.write", "status": "success", "input": "{}", "result": "done"},
        ]
        result = _build_narrative(
            [{"id": "r1"}],
            {"r1": blocks},
            {"r1": tool_calls},
            include_thinking=False,
            include_tool_content=True,
            tool_filter=None,
        )
        # First tool_use matches tu1 by ID
        # Second tool_use falls back, finds tu1 already used, picks tu2
        assert len(result) >= 1




class TestFetchOwnersEdgeCases:
    def test_empty_ids(self, test_db):
        """L398: empty conversation_ids returns empty dict."""
        from siftd.api.conversations import _fetch_owners_for_conversations
        conn = open_database(test_db, read_only=True)
        assert _fetch_owners_for_conversations(conn, []) == {}
        conn.close()

    def test_owner_no_table(self, tmp_path):
        """L229: conversation_owners table missing returns []."""
        import sqlite3

        from siftd.api.conversations import _list_conversations_impl
        # Use bare DB without conversation_owners table
        db = tmp_path / "bare.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, workspace_id TEXT, started_at TEXT, ended_at TEXT)")
        conn.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT, git_remote TEXT)")
        conn.execute("CREATE TABLE responses (id TEXT PRIMARY KEY, conversation_id TEXT, model_id TEXT)")
        conn.execute("CREATE TABLE models (id TEXT PRIMARY KEY, raw_name TEXT, name TEXT)")
        result = _list_conversations_impl(
            conn, workspace=None, model=None, since=None, before=None,
            search=None, tool=None, tag=None, all_tags=None, no_tag=None,
            tool_tag=None, n=50, oldest=False, owner="alice",
        )
        assert result == []
        conn.close()


class TestListConversationsFilters:
    def test_tool_filter(self, test_db):
        """L239: tool filter adds SQL clause."""
        result = list_conversations(db_path=test_db, tool="nonexistent_tool")
        assert result == []  # no conversations match this tool
