"""Tests for storage/queries.py correctness and efficiency."""

import json

import pytest

from siftd.storage.queries import (
    ExchangeRow,
    fetch_conversation_exchanges,
    fetch_exchanges,
    fetch_prompt_response_texts,
    fetch_top_tools,
    fetch_top_workspaces,
)
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
)


@pytest.fixture
def queries_db(tmp_path):
    """Create a test database with multi-block prompts/responses."""
    db_path = tmp_path / "queries_test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "test-model")

    # Conversation 1: Single prompt with multi-block content
    conv1_id = insert_conversation(
        conn,
        external_id="conv1",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )

    prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    # Insert blocks in non-index order to verify ordering
    insert_prompt_content(conn, prompt1_id, 2, "text", json.dumps({"text": "Third block"}))
    insert_prompt_content(conn, prompt1_id, 0, "text", json.dumps({"text": "First block"}))
    insert_prompt_content(conn, prompt1_id, 1, "text", json.dumps({"text": "Second block"}))

    response1_id = insert_response(
        conn, conv1_id, prompt1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50,
    )
    # Insert response blocks in non-index order
    insert_response_content(conn, response1_id, 1, "text", json.dumps({"text": "Response part B"}))
    insert_response_content(conn, response1_id, 0, "text", json.dumps({"text": "Response part A"}))

    conn.commit()

    yield {
        "db_path": db_path,
        "conn": conn,
        "conv1_id": conv1_id,
        "prompt1_id": prompt1_id,
        "harness_id": harness_id,
        "workspace_id": workspace_id,
        "model_id": model_id,
    }

    conn.close()


class TestMultiBlockOrdering:
    """Verify multi-block content comes back in correct block_index order."""

    def test_prompt_blocks_ordered_by_index(self, queries_db):
        """Prompt content blocks are concatenated in block_index order."""
        conn = queries_db["conn"]
        prompt_id = queries_db["prompt1_id"]

        result = fetch_prompt_response_texts(conn, [prompt_id])

        assert len(result) == 1
        _, prompt_text, _ = result[0]
        # Blocks should be in order: First, Second, Third
        assert "First block\nSecond block\nThird block" == prompt_text

    def test_response_blocks_ordered_by_index(self, queries_db):
        """Response content blocks are concatenated in block_index order."""
        conn = queries_db["conn"]
        prompt_id = queries_db["prompt1_id"]

        result = fetch_prompt_response_texts(conn, [prompt_id])

        assert len(result) == 1
        _, _, response_text = result[0]
        # Blocks should be in order: A, B
        assert "Response part A\nResponse part B" == response_text

    def test_fetch_exchanges_returns_correct_order(self, queries_db):
        """fetch_exchanges returns blocks in correct order."""
        conn = queries_db["conn"]
        conv_id = queries_db["conv1_id"]

        result = fetch_exchanges(conn, conversation_id=conv_id)

        assert len(result) == 1
        assert result[0].prompt_text == "First block\nSecond block\nThird block"
        assert result[0].response_text == "Response part A\nResponse part B"


class TestMultipleResponsesPerPrompt:
    """Verify handling of multiple responses per prompt."""

    def test_multiple_responses_concatenated_by_timestamp(self, queries_db):
        """When a prompt has multiple responses, they're concatenated by timestamp."""
        conn = queries_db["conn"]
        conv_id = queries_db["conv1_id"]
        model_id = queries_db["model_id"]

        # Add a second prompt with multiple responses
        prompt2_id = insert_prompt(conn, conv_id, "p2", "2024-01-15T10:01:00Z")
        insert_prompt_content(conn, prompt2_id, 0, "text", json.dumps({"text": "Multi-response prompt"}))

        # First response (earlier timestamp)
        resp2a_id = insert_response(
            conn, conv_id, prompt2_id, model_id, None, "r2a", "2024-01-15T10:01:01Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, resp2a_id, 0, "text", json.dumps({"text": "First response"}))

        # Second response (later timestamp)
        resp2b_id = insert_response(
            conn, conv_id, prompt2_id, model_id, None, "r2b", "2024-01-15T10:01:02Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, resp2b_id, 0, "text", json.dumps({"text": "Second response"}))

        conn.commit()

        result = fetch_prompt_response_texts(conn, [prompt2_id])

        assert len(result) == 1
        _, _, response_text = result[0]
        # Both responses should be present, separated by double newline
        assert "First response\n\nSecond response" == response_text

    def test_responses_ordered_by_timestamp_not_insert_order(self, queries_db):
        """Responses are ordered by timestamp, not by insertion order."""
        conn = queries_db["conn"]
        conv_id = queries_db["conv1_id"]
        model_id = queries_db["model_id"]

        prompt3_id = insert_prompt(conn, conv_id, "p3", "2024-01-15T10:02:00Z")
        insert_prompt_content(conn, prompt3_id, 0, "text", json.dumps({"text": "Test prompt"}))

        # Insert later response first
        resp3b_id = insert_response(
            conn, conv_id, prompt3_id, model_id, None, "r3b", "2024-01-15T10:02:02Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, resp3b_id, 0, "text", json.dumps({"text": "Later response"}))

        # Insert earlier response second
        resp3a_id = insert_response(
            conn, conv_id, prompt3_id, model_id, None, "r3a", "2024-01-15T10:02:01Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, resp3a_id, 0, "text", json.dumps({"text": "Earlier response"}))

        conn.commit()

        result = fetch_prompt_response_texts(conn, [prompt3_id])

        _, _, response_text = result[0]
        # Earlier response should come first despite being inserted second
        assert "Earlier response\n\nLater response" == response_text


class TestQueryEfficiency:
    """Verify queries don't perform unbounded scans."""

    def test_conversation_filter_limits_scan(self, queries_db):
        """fetch_exchanges with conversation_id only touches that conversation's rows."""
        conn = queries_db["conn"]
        harness_id = queries_db["harness_id"]
        workspace_id = queries_db["workspace_id"]
        model_id = queries_db["model_id"]

        # Create a second conversation with data
        conv2_id = insert_conversation(
            conn,
            external_id="conv2",
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at="2024-01-16T10:00:00Z",
        )
        prompt2_id = insert_prompt(conn, conv2_id, "p2-conv2", "2024-01-16T10:00:00Z")
        insert_prompt_content(conn, prompt2_id, 0, "text", json.dumps({"text": "Conv2 prompt"}))
        response2_id = insert_response(
            conn, conv2_id, prompt2_id, model_id, None, "r2-conv2", "2024-01-16T10:00:01Z",
            input_tokens=100, output_tokens=50,
        )
        insert_response_content(conn, response2_id, 0, "text", json.dumps({"text": "Conv2 response"}))
        conn.commit()

        # Fetch only conv1's exchanges
        conv1_id = queries_db["conv1_id"]
        result = fetch_exchanges(conn, conversation_id=conv1_id)

        # Should only get conv1's data
        assert all(ex.conversation_id == conv1_id for ex in result)
        # Should not include conv2's data
        conv_ids = {ex.conversation_id for ex in result}
        assert conv2_id not in conv_ids

    def test_prompt_ids_filter_limits_scan(self, queries_db):
        """fetch_exchanges with prompt_ids only touches those prompts."""
        conn = queries_db["conn"]
        prompt1_id = queries_db["prompt1_id"]

        result = fetch_exchanges(conn, prompt_ids=[prompt1_id])

        assert len(result) == 1
        assert result[0].prompt_id == prompt1_id

    def test_empty_prompt_ids_returns_empty(self, queries_db):
        """fetch_exchanges with empty prompt_ids returns empty without querying."""
        conn = queries_db["conn"]

        result = fetch_exchanges(conn, prompt_ids=[])

        assert result == []

    def test_conversation_exchanges_respects_filter(self, queries_db):
        """fetch_conversation_exchanges with conversation_id doesn't scan all responses."""
        conn = queries_db["conn"]
        harness_id = queries_db["harness_id"]
        workspace_id = queries_db["workspace_id"]
        model_id = queries_db["model_id"]

        # Create many conversations to make unbounded scan expensive
        for i in range(5):
            conv_id = insert_conversation(
                conn,
                external_id=f"conv-extra-{i}",
                harness_id=harness_id,
                workspace_id=workspace_id,
                started_at=f"2024-01-2{i}T10:00:00Z",
            )
            prompt_id = insert_prompt(conn, conv_id, f"p-extra-{i}", f"2024-01-2{i}T10:00:00Z")
            insert_prompt_content(conn, prompt_id, 0, "text", json.dumps({"text": f"Extra prompt {i}"}))
            response_id = insert_response(
                conn, conv_id, prompt_id, model_id, None, f"r-extra-{i}", f"2024-01-2{i}T10:00:01Z",
                input_tokens=100, output_tokens=50,
            )
            insert_response_content(conn, response_id, 0, "text", json.dumps({"text": f"Extra response {i}"}))

        conn.commit()

        # Fetch only the original conversation
        conv1_id = queries_db["conv1_id"]
        result = fetch_conversation_exchanges(conn, conversation_id=conv1_id)

        # Should only have the original conversation
        assert conv1_id in result
        assert len(result) == 1


class TestFetchExchangesBasics:
    """Basic functionality tests for fetch_exchanges."""

    def test_returns_exchange_row_dataclass(self, queries_db):
        """fetch_exchanges returns ExchangeRow instances."""
        conn = queries_db["conn"]
        conv_id = queries_db["conv1_id"]

        result = fetch_exchanges(conn, conversation_id=conv_id)

        assert len(result) > 0
        assert isinstance(result[0], ExchangeRow)

    def test_exchange_row_has_all_fields(self, queries_db):
        """ExchangeRow has all expected fields populated."""
        conn = queries_db["conn"]
        conv_id = queries_db["conv1_id"]
        prompt_id = queries_db["prompt1_id"]

        result = fetch_exchanges(conn, conversation_id=conv_id)

        assert len(result) == 1
        ex = result[0]
        assert ex.conversation_id == conv_id
        assert ex.prompt_id == prompt_id
        assert ex.prompt_timestamp == "2024-01-15T10:00:00Z"
        assert ex.prompt_text  # Non-empty
        assert ex.response_text  # Non-empty

    def test_prompt_without_response(self, queries_db):
        """Prompts without responses return empty response_text."""
        conn = queries_db["conn"]
        conv_id = queries_db["conv1_id"]

        # Add a prompt with no response
        prompt_no_resp_id = insert_prompt(conn, conv_id, "p-no-resp", "2024-01-15T10:03:00Z")
        insert_prompt_content(conn, prompt_no_resp_id, 0, "text", json.dumps({"text": "Unanswered prompt"}))
        conn.commit()

        result = fetch_exchanges(conn, prompt_ids=[prompt_no_resp_id])

        assert len(result) == 1
        assert result[0].prompt_text == "Unanswered prompt"
        assert result[0].response_text == ""

    def test_strips_whitespace(self, queries_db):
        """Text values are stripped of leading/trailing whitespace."""
        conn = queries_db["conn"]
        conv_id = queries_db["conv1_id"]
        model_id = queries_db["model_id"]

        prompt_ws_id = insert_prompt(conn, conv_id, "p-ws", "2024-01-15T10:04:00Z")
        insert_prompt_content(conn, prompt_ws_id, 0, "text", json.dumps({"text": "  Whitespace prompt  "}))

        response_ws_id = insert_response(
            conn, conv_id, prompt_ws_id, model_id, None, "r-ws", "2024-01-15T10:04:01Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, response_ws_id, 0, "text", json.dumps({"text": "  Whitespace response  "}))
        conn.commit()

        result = fetch_exchanges(conn, prompt_ids=[prompt_ws_id])

        assert result[0].prompt_text == "Whitespace prompt"
        assert result[0].response_text == "Whitespace response"


class TestFetchConversationExchanges:
    """Tests for fetch_conversation_exchanges wrapper."""

    def test_groups_by_conversation(self, queries_db):
        """Results are grouped by conversation_id."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        result = fetch_conversation_exchanges(conn, conversation_id=conv1_id)

        assert conv1_id in result
        assert isinstance(result[conv1_id], list)

    def test_exchange_dict_format(self, queries_db):
        """Exchange dicts have 'text' and 'prompt_id' keys."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        result = fetch_conversation_exchanges(conn, conversation_id=conv1_id)

        exchange = result[conv1_id][0]
        assert "text" in exchange
        assert "prompt_id" in exchange

    def test_text_combines_prompt_and_response(self, queries_db):
        """Exchange text combines prompt and response with double newline."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        result = fetch_conversation_exchanges(conn, conversation_id=conv1_id)

        exchange = result[conv1_id][0]
        # Should have both prompt and response text
        assert "First block" in exchange["text"]
        assert "Response part A" in exchange["text"]
        # Should be separated by double newline
        assert "\n\n" in exchange["text"]

    def test_skips_empty_exchanges(self, queries_db):
        """Exchanges with no text content are skipped."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        # Add a prompt with only non-text content
        prompt_empty_id = insert_prompt(conn, conv1_id, "p-empty", "2024-01-15T10:05:00Z")
        insert_prompt_content(conn, prompt_empty_id, 0, "image", json.dumps({"url": "http://example.com/img.png"}))
        conn.commit()

        result = fetch_conversation_exchanges(conn, conversation_id=conv1_id)

        # The empty exchange should not appear
        prompt_ids = [ex["prompt_id"] for ex in result[conv1_id]]
        assert prompt_empty_id not in prompt_ids


class TestExcludeConversationIds:
    """Tests for exclude_conversation_ids SQL filtering."""

    def test_exclude_single_conversation(self, queries_db):
        """Single conversation can be excluded via SQL."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]
        harness_id = queries_db["harness_id"]
        workspace_id = queries_db["workspace_id"]
        model_id = queries_db["model_id"]

        # Create second conversation
        conv2_id = insert_conversation(
            conn,
            external_id="conv2-excl",
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at="2024-01-17T10:00:00Z",
        )
        prompt2_id = insert_prompt(conn, conv2_id, "p2-excl", "2024-01-17T10:00:00Z")
        insert_prompt_content(conn, prompt2_id, 0, "text", json.dumps({"text": "Conv2 text"}))
        response2_id = insert_response(
            conn, conv2_id, prompt2_id, model_id, None, "r2-excl", "2024-01-17T10:00:01Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, response2_id, 0, "text", json.dumps({"text": "Conv2 response"}))
        conn.commit()

        # Exclude conv1, should only get conv2
        result = fetch_conversation_exchanges(
            conn, exclude_conversation_ids={conv1_id}
        )

        assert conv1_id not in result
        assert conv2_id in result

    def test_exclude_multiple_conversations(self, queries_db):
        """Multiple conversations can be excluded via SQL."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]
        harness_id = queries_db["harness_id"]
        workspace_id = queries_db["workspace_id"]
        model_id = queries_db["model_id"]

        # Create two more conversations
        conv2_id = insert_conversation(
            conn, external_id="conv2-multi", harness_id=harness_id,
            workspace_id=workspace_id, started_at="2024-01-18T10:00:00Z",
        )
        prompt2_id = insert_prompt(conn, conv2_id, "p2-multi", "2024-01-18T10:00:00Z")
        insert_prompt_content(conn, prompt2_id, 0, "text", json.dumps({"text": "Conv2"}))
        response2_id = insert_response(
            conn, conv2_id, prompt2_id, model_id, None, "r2-multi", "2024-01-18T10:00:01Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, response2_id, 0, "text", json.dumps({"text": "Resp2"}))

        conv3_id = insert_conversation(
            conn, external_id="conv3-multi", harness_id=harness_id,
            workspace_id=workspace_id, started_at="2024-01-19T10:00:00Z",
        )
        prompt3_id = insert_prompt(conn, conv3_id, "p3-multi", "2024-01-19T10:00:00Z")
        insert_prompt_content(conn, prompt3_id, 0, "text", json.dumps({"text": "Conv3"}))
        response3_id = insert_response(
            conn, conv3_id, prompt3_id, model_id, None, "r3-multi", "2024-01-19T10:00:01Z",
            input_tokens=50, output_tokens=25,
        )
        insert_response_content(conn, response3_id, 0, "text", json.dumps({"text": "Resp3"}))
        conn.commit()

        # Exclude conv1 and conv2, should only get conv3
        result = fetch_conversation_exchanges(
            conn, exclude_conversation_ids={conv1_id, conv2_id}
        )

        assert conv1_id not in result
        assert conv2_id not in result
        assert conv3_id in result

    def test_exclude_empty_set_returns_all(self, queries_db):
        """Empty exclude set returns all conversations."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        result = fetch_conversation_exchanges(
            conn, exclude_conversation_ids=set()
        )

        # Should still return conv1
        assert conv1_id in result

    def test_exclude_none_returns_all(self, queries_db):
        """None exclude returns all conversations."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        result = fetch_conversation_exchanges(
            conn, exclude_conversation_ids=None
        )

        assert conv1_id in result

    def test_exclude_with_conversation_filter(self, queries_db):
        """Exclude works alongside conversation_id filter."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        # Excluding the same conversation we're filtering to returns empty
        result = fetch_conversation_exchanges(
            conn,
            conversation_id=conv1_id,
            exclude_conversation_ids={conv1_id},
        )

        assert conv1_id not in result

    def test_exclude_large_list_batched(self, queries_db):
        """Large exclude lists are handled via batching."""
        conn = queries_db["conn"]
        conv1_id = queries_db["conv1_id"]

        # Create a large set of fake IDs to exclude (simulating 1000+ indexed convs)
        large_exclude = {f"fake-id-{i}" for i in range(1500)}
        # Don't exclude the real conversation
        assert conv1_id not in large_exclude

        result = fetch_conversation_exchanges(
            conn, exclude_conversation_ids=large_exclude
        )

        # conv1 should still be returned (not in exclude list)
        assert conv1_id in result


class TestFetchTopWorkspaces:
    """Tests for fetch_top_workspaces behavior."""

    def test_excludes_workspaces_with_no_conversations(self, tmp_path):
        """Workspaces with 0 conversations are NOT included in results.

        This is intentional: a workspace with no activity isn't "top".
        The query only returns workspaces that have at least one conversation.
        """
        db_path = tmp_path / "top_ws_test.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")

        # Create workspace with conversations
        ws_active_id = get_or_create_workspace(conn, "/active/project", "2024-01-01T00:00:00Z")
        insert_conversation(conn, "c1", harness_id, ws_active_id, "2024-01-01T00:00:00Z")
        insert_conversation(conn, "c2", harness_id, ws_active_id, "2024-01-02T00:00:00Z")

        # Create workspace WITHOUT conversations
        get_or_create_workspace(conn, "/empty/project", "2024-01-01T00:00:00Z")

        conn.commit()

        result = fetch_top_workspaces(conn)

        # Only the active workspace should appear
        paths = [row["path"] for row in result]
        assert "/active/project" in paths
        assert "/empty/project" not in paths

        conn.close()

    def test_orders_by_conversation_count_desc(self, tmp_path):
        """Results are ordered by conversation count, highest first."""
        db_path = tmp_path / "top_ws_order.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")

        # Workspace A: 3 conversations
        ws_a_id = get_or_create_workspace(conn, "/project-a", "2024-01-01T00:00:00Z")
        for i in range(3):
            insert_conversation(conn, f"a-{i}", harness_id, ws_a_id, f"2024-01-0{i+1}T00:00:00Z")

        # Workspace B: 1 conversation
        ws_b_id = get_or_create_workspace(conn, "/project-b", "2024-01-01T00:00:00Z")
        insert_conversation(conn, "b-0", harness_id, ws_b_id, "2024-01-01T00:00:00Z")

        conn.commit()

        result = fetch_top_workspaces(conn)

        assert result[0]["path"] == "/project-a"
        assert result[0]["convs"] == 3
        assert result[1]["path"] == "/project-b"
        assert result[1]["convs"] == 1

        conn.close()

    def test_respects_limit(self, tmp_path):
        """Only returns up to the specified limit."""
        db_path = tmp_path / "top_ws_limit.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")

        # Create 5 workspaces each with 1 conversation
        for i in range(5):
            ws_id = get_or_create_workspace(conn, f"/project-{i}", "2024-01-01T00:00:00Z")
            insert_conversation(conn, f"c-{i}", harness_id, ws_id, "2024-01-01T00:00:00Z")

        conn.commit()

        result = fetch_top_workspaces(conn, limit=3)

        assert len(result) == 3

        conn.close()


class TestFetchTopTools:
    """Tests for fetch_top_tools behavior."""

    def test_excludes_tools_with_no_calls(self, tmp_path):
        """Tools with 0 calls are NOT included in results.

        This is intentional: a tool that was never called isn't "top".
        The query only returns tools that have at least one call.
        """
        db_path = tmp_path / "top_tools_test.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        ws_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "test-model")
        conv_id = insert_conversation(conn, "c1", harness_id, ws_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-01T00:00:01Z", 100, 50)

        # Tool with calls
        tool_used_id = get_or_create_tool(conn, "tool_with_calls")
        insert_tool_call(conn, response_id, conv_id, tool_used_id, "tc1", "{}", None, "success", "2024-01-01T00:00:02Z")

        # Tool WITHOUT calls
        get_or_create_tool(conn, "unused_tool")

        conn.commit()

        result = fetch_top_tools(conn)

        names = [row["name"] for row in result]
        assert "tool_with_calls" in names
        assert "unused_tool" not in names

        conn.close()

    def test_orders_by_usage_count_desc(self, tmp_path):
        """Results are ordered by usage count, highest first."""
        db_path = tmp_path / "top_tools_order.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        ws_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "test-model")
        conv_id = insert_conversation(conn, "c1", harness_id, ws_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-01T00:00:01Z", 100, 50)

        # Tool A: 5 calls
        tool_a_id = get_or_create_tool(conn, "tool_a")
        for i in range(5):
            insert_tool_call(conn, response_id, conv_id, tool_a_id, f"a-{i}", "{}", None, "success", "2024-01-01T00:00:02Z")

        # Tool B: 2 calls
        tool_b_id = get_or_create_tool(conn, "tool_b")
        for i in range(2):
            insert_tool_call(conn, response_id, conv_id, tool_b_id, f"b-{i}", "{}", None, "success", "2024-01-01T00:00:02Z")

        conn.commit()

        result = fetch_top_tools(conn)

        assert result[0]["name"] == "tool_a"
        assert result[0]["uses"] == 5
        assert result[1]["name"] == "tool_b"
        assert result[1]["uses"] == 2

        conn.close()

    def test_empty_database_returns_empty(self, tmp_path):
        """Empty database (no tool_calls) returns empty list."""
        db_path = tmp_path / "top_tools_empty.db"
        conn = create_database(db_path)

        result = fetch_top_tools(conn)

        assert result == []

        conn.close()
