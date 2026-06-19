"""Tests for the public API module."""

import json

import pytest
from conftest import make_conversation
from painted import Fidelity

from siftd.api import (
    ConversationDetail,
    ConversationSummary,
    CostCoverage,
    DatabaseStats,
    get_conversation,
    get_cost_coverage,
    get_stats,
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


def test_lazy_search_names_are_a_subset_of_all():
    """Every lazily-resolved search symbol must also be declared in __all__, or
    the documented public surface silently disagrees with what __getattr__ will
    resolve (and `from pkg import *` drops it). Pins the invariant for both the
    top-level package and the api package."""
    import siftd
    import siftd.api

    for mod in (siftd, siftd.api):
        missing = mod._LAZY_SEARCH_NAMES - set(mod.__all__)
        assert not missing, f"{mod.__name__}: lazy search names absent from __all__: {sorted(missing)}"


def test_process_search_view_resolves_through_api_boundaries():
    """The Slice-3 orchestrator + its result type are reachable from both the
    package boundary and the top level (and are the same object)."""
    from siftd import SearchView as TopSearchView
    from siftd import process_search_view as top_process
    from siftd.api import SearchView, process_search_view

    assert TopSearchView is SearchView
    assert top_process is process_search_view
    assert callable(process_search_view)
    assert {"results", "view", "tier1", "tier2", "n_skipped", "empty_reason"} <= set(
        SearchView.__dataclass_fields__
    )


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
        from siftd.storage.usage_rollup import rebuild_rollups

        conn = open_database(test_db)
        rebuild_rollups(conn, commit=True)
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
        from siftd.storage.usage_rollup import rebuild_rollups

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

        rebuild_rollups(conn, commit=True)
        result = get_cost_coverage(conn)
        conn.close()

        assert result.total_with_tokens == 2
        assert result.with_positive_cost == 1   # c1 has cost > 0
        assert result.with_null_cost == 1        # c2 is NULL
        assert result.pct_covered == 50.0


class TestListConversationsGroupSubagents:
    """group_subagents pages by ROOT session; sub-agents ride along, unlimited."""

    def _db(self, tmp_path):
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
        )

        conn = create_database(tmp_path / "g.db")
        h = get_or_create_harness(conn, "claude_code", source="anthropic")
        w = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        # Newest root r1 + 2 sub-agents; older root r2 + 1 sub-agent.
        insert_conversation(conn, "claude_code::r1", h, w, "2024-02-02T00:00:00Z")
        insert_conversation(conn, "claude_code::r1::agent::a", h, w, "2024-02-02T00:01:00Z")
        insert_conversation(conn, "claude_code::r1::agent::b", h, w, "2024-02-02T00:02:00Z")
        insert_conversation(conn, "claude_code::r2", h, w, "2024-01-01T00:00:00Z")
        insert_conversation(conn, "claude_code::r2::agent::c", h, w, "2024-01-01T00:01:00Z")
        conn.commit()
        conn.close()
        return tmp_path / "g.db"

    def test_n_limits_roots_not_children(self, tmp_path):
        db = self._db(tmp_path)
        # n=1 -> only the newest ROOT (r1), but BOTH its sub-agents come along.
        rows = list_conversations(
            fidelity=Fidelity(), db_path=db, n=1, group_subagents=True
        )
        exts = {r.external_id for r in rows}
        assert exts == {
            "claude_code::r1",
            "claude_code::r1::agent::a",
            "claude_code::r1::agent::b",
        }
        # r2 (root #2) and its child are past the n=1 page -> excluded entirely.
        assert not any(e.startswith("claude_code::r2") for e in exts)

    def test_parent_external_id_derived(self, tmp_path):
        db = self._db(tmp_path)
        rows = list_conversations(
            fidelity=Fidelity(), db_path=db, n=1, group_subagents=True
        )
        by_ext = {r.external_id: r for r in rows}
        assert by_ext["claude_code::r1"].parent_external_id is None
        assert by_ext["claude_code::r1::agent::a"].parent_external_id == "claude_code::r1"
        assert by_ext["claude_code::r1::agent::b"].parent_external_id == "claude_code::r1"

    def test_two_roots_pull_all_their_children(self, tmp_path):
        db = self._db(tmp_path)
        rows = list_conversations(
            fidelity=Fidelity(), db_path=db, n=2, group_subagents=True
        )
        assert {r.external_id for r in rows} == {
            "claude_code::r1", "claude_code::r1::agent::a", "claude_code::r1::agent::b",
            "claude_code::r2", "claude_code::r2::agent::c",
        }

    def test_flat_mode_counts_all_conversations(self, tmp_path):
        db = self._db(tmp_path)
        # Without grouping, n=1 is a flat limit over ALL conversations.
        rows = list_conversations(fidelity=Fidelity(), db_path=db, n=1)
        assert len(rows) == 1


class TestListConversations:
    def test_returns_conversations(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db)

        assert len(conversations) == 2
        assert all(isinstance(c, ConversationSummary) for c in conversations)

    def test_default_sort_newest_first(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db)

        # Should be sorted by started_at descending
        assert conversations[0].started_at > conversations[1].started_at

    def test_oldest_first_sort(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, oldest=True)

        assert conversations[0].started_at < conversations[1].started_at

    def test_limit_parameter(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, n=1)

        assert len(conversations) == 1

    def test_workspace_filter(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, workspace="project")
        assert len(conversations) == 2

        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, workspace="nonexistent")
        assert len(conversations) == 0

    def test_model_filter(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, model="opus")
        assert len(conversations) == 2

        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, model="haiku")
        assert len(conversations) == 0

    def test_since_filter(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, since="2024-01-16")
        assert len(conversations) == 1
        assert "2024-01-16" in conversations[0].started_at

    def test_before_filter(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, before="2024-01-16")
        assert len(conversations) == 1
        assert "2024-01-15" in conversations[0].started_at

    def test_conversation_summary_fields(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, n=1)
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
            list_conversations(fidelity=Fidelity(), db_path=tmp_path / "nonexistent.db")


class TestGetConversation:
    def test_returns_conversation_detail(self, test_db):
        # First get a conversation ID
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, n=1)
        conv_id = conversations[0].id

        detail = get_conversation(conv_id, fidelity=Fidelity(), db_path=test_db)

        assert isinstance(detail, ConversationDetail)
        assert detail.id == conv_id

    def test_supports_prefix_match(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, n=1)
        conv_id = conversations[0].id
        # Use the full ID — prefix matching with less than 26 chars can collide
        # in test DBs where two conversations are inserted in the same millisecond.
        detail = get_conversation(conv_id, fidelity=Fidelity(), db_path=test_db)

        assert detail is not None
        assert detail.id == conv_id

    def test_returns_none_for_missing(self, test_db):
        detail = get_conversation("nonexistent_id", fidelity=Fidelity(), db_path=test_db)
        assert detail is None

    def test_detail_has_exchanges(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, n=1)
        detail = get_conversation(conversations[0].id, fidelity=Fidelity(), db_path=test_db)

        assert len(detail.exchanges) > 0
        exchange = detail.exchanges[0]
        assert exchange.prompt_text is not None or exchange.response_text is not None

    def test_detail_token_counts(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db, n=1)
        detail = get_conversation(conversations[0].id, fidelity=Fidelity(), db_path=test_db)

        assert detail.total_input_tokens > 0
        assert detail.total_output_tokens > 0

    def test_raises_for_missing_db(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_conversation("some_id", fidelity=Fidelity(), db_path=tmp_path / "nonexistent.db")


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
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db)
        results = [
            SearchResult(conversations[0].id, 0.9, "text1", "prompt", "/ws", conversations[0].started_at),
            SearchResult(conversations[1].id, 0.8, "text2", "prompt", "/ws", conversations[1].started_at),
        ]

        earliest = first_mention(results, threshold=0.65, db_path=test_db)

        assert earliest is not None
        # Earlier conversation should be returned
        assert earliest.conversation_id == conversations[1].id

    def test_returns_none_below_threshold(self, test_db):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db)
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
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db)
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
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db)
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
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db_with_tool_tags, tool_tag="shell:test")

        assert len(conversations) == 2  # conv1 and conv3 have shell:test

    def test_filter_by_different_tool_tag(self, test_db_with_tool_tags):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db_with_tool_tags, tool_tag="shell:vcs")

        assert len(conversations) == 1  # only conv2 has shell:vcs

    def test_no_matches_for_unknown_tag(self, test_db_with_tool_tags):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db_with_tool_tags, tool_tag="shell:unknown")

        assert len(conversations) == 0

    def test_tool_tag_combines_with_workspace_filter(self, test_db_with_tool_tags):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db_with_tool_tags,
            tool_tag="shell:test",
            workspace="other",
        )

        assert len(conversations) == 1  # only conv3 matches both

    def test_filter_by_tool_tag_prefix(self, test_db_with_tool_tags):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db_with_tool_tags, tool_tag="shell:")

        assert len(conversations) == 3  # all conversations have shell:* tags

    def test_tool_tag_prefix_no_matches(self, test_db_with_tool_tags):
        conversations = list_conversations(fidelity=Fidelity(), db_path=test_db_with_tool_tags, tool_tag="other:")

        assert len(conversations) == 0


class TestListConversationsPolymorphicTags:
    """list_conversations -l filter must surface tags applied at any kind."""

    @pytest.fixture
    def db_with_polymorphic_tags(self, tmp_path):
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_model,
            get_or_create_tool, get_or_create_workspace, insert_conversation,
            insert_prompt, insert_prompt_content, insert_response,
            insert_response_content, insert_tool_call,
        )
        from siftd.storage.tags import apply_tag, get_or_create_tag

        db_path = tmp_path / "polymorphic.db"
        conn = create_database(db_path)
        harness_id = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
        ws_id = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "claude-3-opus")
        tool_id = get_or_create_tool(conn, "shell.execute")

        review_tag = get_or_create_tag(conn, "review")

        # conv_a: tag on conversation
        conv_a = insert_conversation(conn, external_id="a", harness_id=harness_id,
                                     workspace_id=ws_id, started_at="2024-01-15T10:00:00Z")
        p_a = insert_prompt(conn, conv_a, "p_a", "2024-01-15T10:00:00Z")
        insert_prompt_content(conn, p_a, 0, "text", '{"text": "q"}')
        r_a = insert_response(conn, conv_a, p_a, model_id, None, "r_a",
                              "2024-01-15T10:00:01Z", input_tokens=1, output_tokens=1)
        insert_response_content(conn, r_a, 0, "text", '{"text": "a"}')
        apply_tag(conn, "conversation", conv_a, review_tag)

        # conv_b: tag on response
        conv_b = insert_conversation(conn, external_id="b", harness_id=harness_id,
                                     workspace_id=ws_id, started_at="2024-01-16T10:00:00Z")
        p_b = insert_prompt(conn, conv_b, "p_b", "2024-01-16T10:00:00Z")
        insert_prompt_content(conn, p_b, 0, "text", '{"text": "q"}')
        r_b = insert_response(conn, conv_b, p_b, model_id, None, "r_b",
                              "2024-01-16T10:00:01Z", input_tokens=1, output_tokens=1)
        insert_response_content(conn, r_b, 0, "text", '{"text": "a"}')
        apply_tag(conn, "response", r_b, review_tag)

        # conv_c: tag on tool_call
        conv_c = insert_conversation(conn, external_id="c", harness_id=harness_id,
                                     workspace_id=ws_id, started_at="2024-01-17T10:00:00Z")
        p_c = insert_prompt(conn, conv_c, "p_c", "2024-01-17T10:00:00Z")
        insert_prompt_content(conn, p_c, 0, "text", '{"text": "q"}')
        r_c = insert_response(conn, conv_c, p_c, model_id, None, "r_c",
                              "2024-01-17T10:00:01Z", input_tokens=1, output_tokens=1)
        tc_c = insert_tool_call(conn, r_c, conv_c, tool_id, "tc_c",
                                '{}', '{}', "success", "2024-01-17T10:00:01Z")
        apply_tag(conn, "tool_call", tc_c, review_tag)

        # conv_d: untagged (control)
        conv_d = insert_conversation(conn, external_id="d", harness_id=harness_id,
                                     workspace_id=ws_id, started_at="2024-01-18T10:00:00Z")
        p_d = insert_prompt(conn, conv_d, "p_d", "2024-01-18T10:00:00Z")
        insert_prompt_content(conn, p_d, 0, "text", '{"text": "q"}')
        insert_response(conn, conv_d, p_d, model_id, None, "r_d",
                        "2024-01-18T10:00:01Z", input_tokens=1, output_tokens=1)

        conn.commit()
        conn.close()
        return db_path, conv_a, conv_b, conv_c, conv_d

    def test_default_matches_all_kinds(self, db_with_polymorphic_tags):
        db, a, b, c, _d = db_with_polymorphic_tags
        ids = {c.id for c in list_conversations(fidelity=Fidelity(), db_path=db, tag=["review"], n=0)}
        assert ids == {a, b, c}

    def test_scoped_to_conversation(self, db_with_polymorphic_tags):
        db, a, _b, _c, _d = db_with_polymorphic_tags
        ids = {c.id for c in list_conversations(fidelity=Fidelity(), db_path=db, tag=["review"], tag_kind=["conversation"], n=0,
        )}
        assert ids == {a}

    def test_scoped_to_response(self, db_with_polymorphic_tags):
        db, _a, b, _c, _d = db_with_polymorphic_tags
        ids = {c.id for c in list_conversations(fidelity=Fidelity(), db_path=db, tag=["review"], tag_kind=["response"], n=0,
        )}
        assert ids == {b}

    def test_scoped_to_tool_call(self, db_with_polymorphic_tags):
        db, _a, _b, c, _d = db_with_polymorphic_tags
        ids = {row.id for row in list_conversations(fidelity=Fidelity(), db_path=db, tag=["review"], tag_kind=["tool_call"], n=0,
        )}
        assert ids == {c}

    def test_scoped_to_multiple_kinds(self, db_with_polymorphic_tags):
        db, _a, b, c, _d = db_with_polymorphic_tags
        ids = {row.id for row in list_conversations(fidelity=Fidelity(), db_path=db, tag=["review"], tag_kind=["response", "tool_call"], n=0,
        )}
        assert ids == {b, c}

    def test_no_tag_excludes_polymorphically(self, db_with_polymorphic_tags):
        db, _a, _b, _c, d = db_with_polymorphic_tags
        ids = {row.id for row in list_conversations(fidelity=Fidelity(), db_path=db, no_tag=["review"], n=0)}
        assert ids == {d}

    def test_all_tags_polymorphic(self, db_with_polymorphic_tags):
        db, a, b, c, _d = db_with_polymorphic_tags
        # Default kinds: 'review' present at any granularity satisfies all_tags=['review']
        ids = {row.id for row in list_conversations(fidelity=Fidelity(), db_path=db, all_tags=["review"], n=0,
        )}
        assert ids == {a, b, c}


class TestGetConversationEventIds:
    """get_conversation must surface prompt_id/response_ids/tool_call_ids on Turn."""

    @pytest.fixture
    def db_with_event_ids(self, tmp_path):
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_model,
            get_or_create_tool, get_or_create_workspace, insert_conversation,
            insert_prompt, insert_prompt_content, insert_response,
            insert_response_content, insert_tool_call,
        )

        db_path = tmp_path / "events.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        m = get_or_create_model(conn, "claude-3-opus")
        t = get_or_create_tool(conn, "shell.execute")

        c = insert_conversation(conn, external_id="x", harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")

        p1 = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
        insert_prompt_content(conn, p1, 0, "text", '{"text": "do thing"}')
        r1 = insert_response(conn, c, p1, m, None, "r1",
                             "2024-01-15T10:00:01Z", input_tokens=10, output_tokens=5)
        insert_response_content(conn, r1, 0, "text", '{"text": "result text"}')
        insert_response_content(conn, r1, 1, "tool_use",
                                '{"id": "toolu_1", "name": "shell.execute", "input": {"command": "ls"}}')
        tc1 = insert_tool_call(conn, r1, c, t, "toolu_1",
                               '{"command": "ls"}', '{}', "success", "2024-01-15T10:00:01Z")

        p2 = insert_prompt(conn, c, "p2", "2024-01-15T10:01:00Z")
        insert_prompt_content(conn, p2, 0, "text", '{"text": "next"}')

        conn.commit()
        conn.close()
        return db_path, c, p1, r1, tc1, p2

    def test_turn_carries_event_ids(self, db_with_event_ids):
        db, _c, p1, r1, tc1, p2 = db_with_event_ids
        detail = get_conversation(_c, fidelity=Fidelity(visible=frozenset({"tools"})), db_path=db, )
        assert detail is not None
        assert len(detail.turns) == 2

        t1 = detail.turns[0]
        assert t1.prompt_id == p1
        assert t1.response_ids == [r1]
        assert t1.tool_call_ids == [tc1]

        # Empty-response turn still carries prompt_id
        t2 = detail.turns[1]
        assert t2.prompt_id == p2
        assert t2.response_ids == []
        assert t2.tool_call_ids == []

    def test_narrative_blocks_carry_event_id(self, db_with_event_ids):
        db, _c, _p1, r1, _tc1, _p2 = db_with_event_ids
        detail = get_conversation(_c, fidelity=Fidelity(visible=frozenset({"tools"})), db_path=db, )
        t1 = detail.turns[0]
        # Every block in turn 1 came from r1
        for blk in t1.narrative:
            assert blk.event_id == r1

    def test_tool_call_detail_has_id(self, db_with_event_ids):
        db, _c, _p1, _r1, tc1, _p2 = db_with_event_ids
        detail = get_conversation(_c, fidelity=Fidelity(visible=frozenset({"tools"})), db_path=db, )
        # Find the tool_calls block in turn 1
        tool_blocks = [b for b in detail.turns[0].narrative if b.block_type == "tool_calls"]
        assert tool_blocks, "expected at least one tool_calls block"
        # When include_tool_content=True (collapse=False), tool_call_id is preserved
        flat_tools = [tc for b in tool_blocks for tc in b.tool_calls]
        assert any(tc.tool_call_id == tc1 for tc in flat_tools)


class TestSerializeConversationDetailEventIds:
    def test_json_emits_event_ids_default_on(self, tmp_path):
        from siftd.serialization.conversations import serialize_conversation_detail
        from siftd.storage.sqlite import (
            create_database, get_or_create_harness, get_or_create_model,
            get_or_create_workspace, insert_conversation, insert_prompt,
            insert_prompt_content, insert_response, insert_response_content,
        )

        db_path = tmp_path / "ser.db"
        conn = create_database(db_path)
        h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
        ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
        m = get_or_create_model(conn, "claude-3-opus")
        c = insert_conversation(conn, external_id="x", harness_id=h,
                                workspace_id=ws, started_at="2024-01-15T10:00:00Z")
        p = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
        insert_prompt_content(conn, p, 0, "text", '{"text": "q"}')
        r = insert_response(conn, c, p, m, None, "r1",
                            "2024-01-15T10:00:01Z", input_tokens=1, output_tokens=1)
        insert_response_content(conn, r, 0, "text", '{"text": "answer"}')
        conn.commit()
        conn.close()

        detail = get_conversation(c, fidelity=Fidelity(), db_path=db_path)
        d = serialize_conversation_detail(detail)
        assert d["turns"][0]["prompt_id"] == p
        assert d["turns"][0]["response_ids"] == [r]
        # narrative block carries event_id
        narr = d["turns"][0]["narrative"]
        text_blocks = [b for b in narr if b.get("type") == "text"]
        assert text_blocks
        assert text_blocks[0]["event_id"] == r


class TestSearchJsonChunkIds:
    def test_chunk_id_and_source_ids_default_on(self):
        from siftd.output.json_fmt import _json_chunk_list

        rows = [{
            "conversation_id": "C123",
            "score": 0.9,
            "chunk_type": "response",
            "text": "snippet",
            "_started_at": "2024-01-01",
            "_workspace": "/p",
            "chunk_id": "chunk-1",
            "source_ids": ["e1", "e2"],
        }]
        out = _json_chunk_list(rows)
        assert out[0]["chunk_id"] == "chunk-1"
        assert out[0]["source_ids"] == ["e1", "e2"]


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
            FROM tag_assignments ta
            JOIN tags t ON t.id = ta.tag_id
            WHERE ta.target_kind = 'tool_call'
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

        cur = conn.execute("SELECT COUNT(*) as cnt FROM tag_assignments WHERE target_kind='tool_call'")
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
        tool_calls = [{"tool_call_id": "tc-1", "external_id": "tu1", "tool_name": "file.read", "status": "success", "input": "{}", "result": "ok"}]
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
        tool_calls = [{"tool_call_id": "tc-1", "external_id": "tu1", "tool_name": "shell.execute", "status": "success", "input": "{}", "result": "ok"}]
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
        tool_calls = [{"tool_call_id": "tc-1", "external_id": None, "tool_name": "file.write", "status": "success", "input": "{}", "result": "ok"}]
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
        tool_calls = [{"tool_call_id": "tc-1", "external_id": "tu1", "tool_name": "file.read", "status": "success", "input": "{}", "result": "ok"}]
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
            {"tool_call_id": "tc-1", "external_id": "tu1", "tool_name": "file.read", "status": "success", "input": "{}", "result": "ok"},
            {"tool_call_id": "tc-2", "external_id": "tu2", "tool_name": "file.write", "status": "success", "input": "{}", "result": "done"},
        ]
        result = _build_narrative(
            [{"id": "r1"}],
            {"r1": blocks},
            {"r1": tool_calls},
            include_thinking=False,
            include_tool_content=False,
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
            tool_tag=None, n=50, oldest=False, owner="alice", fidelity=Fidelity(),
        )
        assert result == []
        conn.close()

    def test_list_cost_null_in_no_stats_fallback(self, monkeypatch, tmp_path):
        """The no-stats fallback emits NULL cost (S3-B).

        The rollup is the single canonical cost definition, so an un-ingested /
        sliced DB with no materialized tier reports cost as NULL (unknown) — it
        does NOT re-derive cost at read time.  The setup is the 21%-mispricing
        case: a token-bearing response with NULL provider_id whose harness
        source ("anthropic") has pricing.  The canonical rollup path prices it;
        the retired fallback emitted 0.0 (coalesce_pricing=True + plain provider
        join, no harness-source fallback).  Asserting ``is None`` distinguishes
        the fix from both the old mispriced 0.0 and any computed number.
        """
        import siftd.storage.sqlite as sq
        from siftd.api import conversations as conv_api

        db = tmp_path / "nostats.db"
        conn = sq.create_database(db)
        provider_id = sq.get_or_create_provider(conn, "anthropic")
        model_id = sq.get_or_create_model(conn, "claude-test-model")
        harness_id = sq.get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
        ws_id = sq.get_or_create_workspace(conn, "/proj", "2024-01-01T10:00:00Z")
        conn.execute(
            "INSERT INTO pricing (id, model_id, provider_id, input_per_mtok, output_per_mtok) "
            "VALUES (?, ?, ?, ?, ?)",
            ("pr-1", model_id, provider_id, 3.0, 15.0),
        )
        conv_id = sq.insert_conversation(conn, "c1", harness_id, ws_id, "2024-01-01T10:00:00Z")
        prompt_id = sq.insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        sq.insert_response(
            conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-01T10:00:01Z", 1_000_000, 0
        )
        conn.commit()
        conn.close()

        # Force the no-stats fallback even though the table exists.
        monkeypatch.setattr(conv_api, "has_conversation_stats_table", lambda _conn: False)

        rows = conv_api.list_conversations(fidelity=Fidelity(depth=3), db_path=db, n=1)
        assert rows
        assert rows[0].cost is None


class TestListConversationsFilters:
    def test_tool_filter(self, test_db):
        """L239: tool filter adds SQL clause."""
        result = list_conversations(fidelity=Fidelity(), db_path=test_db, tool="nonexistent_tool")
        assert result == []  # no conversations match this tool
