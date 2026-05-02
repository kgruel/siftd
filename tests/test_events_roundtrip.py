"""Roundtrip tests for the polymorphic events writers and readers (schema v4).

Verifies that insert_event / insert_event_response / insert_event_tool_call /
insert_event_content write the expected rows and that get_event_tree returns
the correct tree shape.  Also validates that store_conversation (dual-write)
populates both the new events tables and the legacy fork tables.
"""

import json

import pytest

from siftd.ids import ulid as _ulid
from siftd.storage.events import (
    get_event_tree,
    insert_event,
    insert_event_content,
    insert_event_response,
    insert_event_tool_call,
)
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    store_conversation,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "events_roundtrip.db"
    conn = create_database(path)
    yield conn
    conn.close()


@pytest.fixture
def conv_id(db):
    harness_id = get_or_create_harness(db, "test", source="test", log_format="jsonl")
    ws_id = get_or_create_workspace(db, "/test/ws", "2024-01-01T00:00:00Z")
    return insert_conversation(db, external_id="conv-rt", harness_id=harness_id,
                               workspace_id=ws_id, started_at="2024-01-01T00:00:00Z")


class TestWriterRoundtrip:
    def test_insert_event_prompt(self, db, conv_id):
        eid = _ulid()
        insert_event(db, eid, "prompt", conv_id, "2024-01-01T10:00:00Z", external_id="p1")
        row = db.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
        assert row["kind"] == "prompt"
        assert row["conversation_id"] == conv_id
        assert row["external_id"] == "p1"
        assert row["parent_id"] is None

    def test_insert_event_response_with_tokens(self, db, conv_id):
        model_id = get_or_create_model(db, "claude-test")
        provider_id = get_or_create_provider(db, "anthropic")
        prompt_id = _ulid()
        insert_event(db, prompt_id, "prompt", conv_id, "2024-01-01T10:00:00Z")
        resp_id = _ulid()
        insert_event(db, resp_id, "response", conv_id, "2024-01-01T10:00:01Z", parent_id=prompt_id)
        insert_event_response(db, resp_id, model_id=model_id, provider_id=provider_id,
                              input_tokens=100, output_tokens=50)
        row = db.execute("SELECT * FROM event_response WHERE event_id = ?", (resp_id,)).fetchone()
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
        assert row["model_id"] == model_id

    def test_insert_event_content(self, db, conv_id):
        prompt_id = _ulid()
        insert_event(db, prompt_id, "prompt", conv_id, "2024-01-01T10:00:00Z")
        content_id = _ulid()
        insert_event_content(db, content_id=content_id, event_id=prompt_id,
                             block_index=0, block_type="text", content='{"text": "hello"}')
        row = db.execute("SELECT * FROM event_content WHERE id = ?", (content_id,)).fetchone()
        assert row["event_id"] == prompt_id
        assert row["block_type"] == "text"
        assert json.loads(row["content"])["text"] == "hello"

    def test_insert_event_tool_call_stores_blob(self, db, conv_id):
        prompt_id = _ulid()
        insert_event(db, prompt_id, "prompt", conv_id, "2024-01-01T10:00:00Z")
        resp_id = _ulid()
        insert_event(db, resp_id, "response", conv_id, "2024-01-01T10:00:01Z", parent_id=prompt_id)
        tc_id = _ulid()
        insert_event(db, tc_id, "tool_call", conv_id, "2024-01-01T10:00:02Z", parent_id=resp_id)
        insert_event_tool_call(db, tc_id, tool_id=None,
                               input_json='{"cmd": "ls"}', result_json='{"out": "ok"}',
                               status="success")
        row = db.execute("SELECT * FROM event_tool_call WHERE event_id = ?", (tc_id,)).fetchone()
        assert row["status"] == "success"
        assert row["result_hash"] is not None
        blob = db.execute("SELECT content FROM content_blobs WHERE hash = ?",
                          (row["result_hash"],)).fetchone()
        assert blob is not None
        assert json.loads(blob["content"])["out"] == "ok"

    def test_insert_event_tool_call_null_result(self, db, conv_id):
        prompt_id = _ulid()
        insert_event(db, prompt_id, "prompt", conv_id, "2024-01-01T10:00:00Z")
        resp_id = _ulid()
        insert_event(db, resp_id, "response", conv_id, "2024-01-01T10:00:01Z", parent_id=prompt_id)
        tc_id = _ulid()
        insert_event(db, tc_id, "tool_call", conv_id, "2024-01-01T10:00:02Z", parent_id=resp_id)
        insert_event_tool_call(db, tc_id, tool_id=None,
                               input_json='{}', result_json=None, status="pending")
        row = db.execute("SELECT result_hash FROM event_tool_call WHERE event_id = ?", (tc_id,)).fetchone()
        assert row["result_hash"] is None


class TestTreeShape:
    def test_event_tree_prompt_response_tool_call(self, db, conv_id):
        model_id = get_or_create_model(db, "claude-test")
        tool_id = get_or_create_tool(db, "Bash")

        prompt_id = _ulid()
        insert_event(db, prompt_id, "prompt", conv_id, "2024-01-01T10:00:00Z", external_id="p1")

        resp_id = _ulid()
        insert_event(db, resp_id, "response", conv_id, "2024-01-01T10:00:01Z",
                     parent_id=prompt_id, external_id="r1")
        insert_event_response(db, resp_id, model_id=model_id, input_tokens=100, output_tokens=50)

        tc_id = _ulid()
        insert_event(db, tc_id, "tool_call", conv_id, "2024-01-01T10:00:02Z",
                     parent_id=resp_id, external_id="tc1")
        insert_event_tool_call(db, tc_id, tool_id=tool_id,
                               input_json='{"cmd": "ls"}', result_json='{"out": "ok"}',
                               status="success")

        tree = get_event_tree(db, conv_id)

        assert len(tree["prompts"]) == 1
        prompt_node = tree["prompts"][0]
        assert prompt_node["id"] == prompt_id
        assert prompt_node["external_id"] == "p1"

        assert len(prompt_node["responses"]) == 1
        resp_node = prompt_node["responses"][0]
        assert resp_node["id"] == resp_id
        assert resp_node["model_id"] == model_id
        assert resp_node["input_tokens"] == 100

        assert len(resp_node["tool_calls"]) == 1
        tc_node = resp_node["tool_calls"][0]
        assert tc_node["id"] == tc_id
        assert tc_node["status"] == "success"
        assert tc_node["result_hash"] is not None

    def test_event_tree_empty_conversation(self, db, conv_id):
        tree = get_event_tree(db, conv_id)
        assert tree == {"prompts": []}

    def test_event_tree_multiple_prompts(self, db, conv_id):
        for i in range(3):
            pid = _ulid()
            insert_event(db, pid, "prompt", conv_id, f"2024-01-01T10:00:0{i}Z")
        tree = get_event_tree(db, conv_id)
        assert len(tree["prompts"]) == 3


class TestDualWrite:
    """Verify store_conversation populates both events tables and legacy fork tables."""

    def test_store_conversation_writes_events(self, tmp_path):
        from siftd.domain.models import ContentBlock, Conversation, Harness, Prompt, Response, Usage
        from siftd.storage.sqlite import open_database

        path = tmp_path / "dual.db"
        conn = create_database(path)
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        ws_id = get_or_create_workspace(conn, "/ws", "2024-01-01T00:00:00Z")

        conv = Conversation(
            external_id="c1",
            workspace_path="/ws",
            started_at="2024-01-01T10:00:00Z",
            harness=Harness(name="test", source="test", log_format="jsonl"),
            prompts=[
                Prompt(
                    external_id="p1",
                    timestamp="2024-01-01T10:00:00Z",
                    content=[ContentBlock(block_type="text", content={"text": "hello"})],
                    responses=[
                        Response(
                            external_id="r1",
                            timestamp="2024-01-01T10:00:01Z",
                            model="test-model",
                            usage=Usage(input_tokens=10, output_tokens=5),
                            content=[ContentBlock(block_type="text", content={"text": "world"})],
                        )
                    ],
                )
            ],
        )

        store_conversation(conn, conv, commit=True)

        # New tables: events
        prompts_in_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'prompt'"
        ).fetchone()[0]
        responses_in_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'response'"
        ).fetchone()[0]
        assert prompts_in_events == 1
        assert responses_in_events == 1

        # event_response has token data
        row = conn.execute("SELECT input_tokens, output_tokens FROM event_response").fetchone()
        assert row["input_tokens"] == 10
        assert row["output_tokens"] == 5

        conn.close()

    def test_content_block_and_tool_call_duplication(self, tmp_path):
        """Codex/aider emit both a ContentBlock(tool_use) and a ToolCall for the same
        invocation. Both must be written: event_content row AND a tool_call event."""
        from siftd.domain.models import (
            ContentBlock, Conversation, Harness, Prompt, Response, ToolCall, Usage,
        )

        path = tmp_path / "dup.db"
        conn = create_database(path)
        get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        get_or_create_workspace(conn, "/ws", "2024-01-01T00:00:00Z")

        conv = Conversation(
            external_id="dup-conv",
            workspace_path="/ws",
            started_at="2024-01-01T10:00:00Z",
            harness=Harness(name="test", source="test", log_format="jsonl"),
            prompts=[
                Prompt(
                    external_id="p1",
                    timestamp="2024-01-01T10:00:00Z",
                    content=[ContentBlock(block_type="text", content={"text": "run ls"})],
                    responses=[
                        Response(
                            external_id="r1",
                            timestamp="2024-01-01T10:00:01Z",
                            model="test-model",
                            usage=Usage(input_tokens=10, output_tokens=5),
                            content=[
                                ContentBlock(
                                    block_type="text",
                                    content={"text": "sure"},
                                ),
                                ContentBlock(
                                    block_type="tool_use",
                                    content={"type": "tool_use", "id": "tc_block", "name": "Bash", "input": {}},
                                ),
                            ],
                            tool_calls=[
                                ToolCall(
                                    tool_name="Bash",
                                    input={"command": "ls"},
                                    result={"output": "file.txt"},
                                    status="success",
                                    external_id="tc_call",
                                    timestamp="2024-01-01T10:00:02Z",
                                ),
                            ],
                        )
                    ],
                )
            ],
        )

        store_conversation(conn, conv, commit=True)

        # The response event must have a tool_use content block
        resp_event = conn.execute(
            "SELECT id FROM events WHERE kind = 'response'"
        ).fetchone()
        assert resp_event is not None
        tool_use_content = conn.execute(
            "SELECT id FROM event_content WHERE event_id = ? AND block_type = 'tool_use'",
            (resp_event["id"],),
        ).fetchone()
        assert tool_use_content is not None, "event_content row for tool_use block missing"

        # A separate tool_call event must exist
        tc_event = conn.execute(
            "SELECT id FROM events WHERE kind = 'tool_call'"
        ).fetchone()
        assert tc_event is not None, "tool_call event missing"

        # event_tool_call extension row for that event
        etc_row = conn.execute(
            "SELECT event_id, status, result_hash FROM event_tool_call WHERE event_id = ?",
            (tc_event["id"],),
        ).fetchone()
        assert etc_row is not None, "event_tool_call extension row missing"
        assert etc_row["status"] == "success"
        assert etc_row["result_hash"] is not None

        conn.close()
