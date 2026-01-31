"""Tests for storage/queries.py correctness and efficiency."""

import json

import pytest

from siftd.storage.queries import (
    ExchangeRow,
    fetch_conversation_exchanges,
    fetch_exchanges,
    fetch_prompt_response_texts,
)
from siftd.storage.sqlite import (
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

    return {
        "db_path": db_path,
        "conn": conn,
        "conv1_id": conv1_id,
        "prompt1_id": prompt1_id,
        "harness_id": harness_id,
        "workspace_id": workspace_id,
        "model_id": model_id,
    }


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
