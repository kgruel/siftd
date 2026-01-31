"""Shared test fixtures for the siftd test suite."""

from pathlib import Path

import pytest

from siftd.domain.models import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    Usage,
)
from siftd.domain.source import Source
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
    record_ingested_file,
)
from siftd.storage.tags import apply_tag, get_or_create_tag

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def make_conversation(
    external_id="test-conv-1",
    workspace_path="/test/project",
    started_at="2024-01-01T10:00:00Z",
    ended_at=None,
    harness_name="test_harness",
    harness_source="test",
    harness_log_format="jsonl",
    model="test-model",
    prompt_text="Hello",
    response_text="Hi there",
    input_tokens=100,
    output_tokens=50,
    tool_calls=None,
    response_attributes=None,
):
    """Build a Conversation domain object for testing.

    Provides sensible defaults for all fields so tests can override only
    what they care about.
    """
    tc_list = tool_calls or []
    return Conversation(
        external_id=external_id,
        workspace_path=workspace_path,
        started_at=started_at,
        ended_at=ended_at,
        harness=Harness(name=harness_name, source=harness_source, log_format=harness_log_format),
        prompts=[
            Prompt(
                external_id="p1",
                timestamp=started_at,
                content=[ContentBlock(block_type="text", content={"text": prompt_text})],
                responses=[
                    Response(
                        external_id="r1",
                        timestamp=started_at,
                        model=model,
                        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
                        content=[ContentBlock(block_type="text", content={"text": response_text})],
                        tool_calls=tc_list,
                        attributes=response_attributes or {},
                    ),
                ],
            ),
        ],
    )


def make_test_adapter(
    dest,
    *,
    name="test_harness",
    dedup="file",
    harness_source="test",
    can_handle_fn=None,
    parse_fn=None,
):
    """Factory for test adapters with configurable dedup strategy and parse function.

    Args:
        dest: Path to the file the adapter will discover
        name: Adapter NAME attribute
        dedup: DEDUP_STRATEGY attribute ('file' or 'session')
        harness_source: HARNESS_SOURCE attribute (e.g., 'test', 'anthropic', 'openai')
        can_handle_fn: Optional custom can_handle(source) function
        parse_fn: Optional custom parse(source) function
    """

    class _Adapter:
        NAME = name
        DEDUP_STRATEGY = dedup
        HARNESS_SOURCE = harness_source

        @staticmethod
        def can_handle(source):
            if can_handle_fn:
                return can_handle_fn(source)
            return True

        @staticmethod
        def parse(source):
            if parse_fn:
                return parse_fn(source)
            return []

        @staticmethod
        def discover():
            yield Source(kind="file", location=dest)

    return _Adapter


def make_session_adapter(dest, *, name="test_harness", dedup="session", parse_fn=None):
    """Factory for test adapters with session-based dedup (convenience wrapper)."""
    return make_test_adapter(dest, name=name, dedup=dedup, parse_fn=parse_fn)


@pytest.fixture
def test_db(tmp_path):
    """Create a test database with standard sample data.

    Contains: 1 harness, 1 workspace, 1 model, 2 conversations with
    prompts, responses, and content.
    """
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "claude-3-opus-20240229")

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

    prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, prompt1_id, 0, "text", '{"text": "Hello, how are you?"}')
    response1_id = insert_response(
        conn, conv1_id, prompt1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50,
    )
    insert_response_content(conn, response1_id, 0, "text", '{"text": "I am doing well, thank you!"}')

    prompt2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
    insert_prompt_content(conn, prompt2_id, 0, "text", '{"text": "What is Python?"}')
    response2_id = insert_response(
        conn, conv2_id, prompt2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
        input_tokens=200, output_tokens=150,
    )
    insert_response_content(conn, response2_id, 0, "text", '{"text": "Python is a programming language."}')

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture
def test_db_with_tool_tags(tmp_path):
    """Create a test database with tool calls and tags.

    Contains: 2 workspaces, 3 conversations, tool calls tagged with
    shell:test and shell:vcs.
    """
    db_path = tmp_path / "test_tools.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    workspace2_id = get_or_create_workspace(conn, "/other/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "claude-3-opus-20240229")
    tool_id = get_or_create_tool(conn, "shell.execute")

    test_tag_id = get_or_create_tag(conn, "shell:test")
    vcs_tag_id = get_or_create_tag(conn, "shell:vcs")

    # Conversation 1 (/test/project) — test command
    conv1_id = insert_conversation(
        conn, external_id="conv1", harness_id=harness_id,
        workspace_id=workspace_id, started_at="2024-01-15T10:00:00Z",
    )
    prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, prompt1_id, 0, "text", '{"text": "Run tests"}')
    response1_id = insert_response(
        conn, conv1_id, prompt1_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50,
    )
    tc1_id = insert_tool_call(
        conn, response1_id, conv1_id, tool_id, "tc1",
        '{"command": "pytest"}', '{"output": "OK"}', "success", "2024-01-15T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc1_id, test_tag_id)

    # Conversation 2 (/test/project) — vcs command
    conv2_id = insert_conversation(
        conn, external_id="conv2", harness_id=harness_id,
        workspace_id=workspace_id, started_at="2024-01-16T10:00:00Z",
    )
    prompt2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-16T10:00:00Z")
    insert_prompt_content(conn, prompt2_id, 0, "text", '{"text": "Commit changes"}')
    response2_id = insert_response(
        conn, conv2_id, prompt2_id, model_id, None, "r2", "2024-01-16T10:00:01Z",
        input_tokens=200, output_tokens=150,
    )
    tc2_id = insert_tool_call(
        conn, response2_id, conv2_id, tool_id, "tc2",
        '{"command": "git commit"}', '{"output": "OK"}', "success", "2024-01-16T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc2_id, vcs_tag_id)

    # Conversation 3 (/other/project) — test command
    conv3_id = insert_conversation(
        conn, external_id="conv3", harness_id=harness_id,
        workspace_id=workspace2_id, started_at="2024-01-17T10:00:00Z",
    )
    prompt3_id = insert_prompt(conn, conv3_id, "p3", "2024-01-17T10:00:00Z")
    insert_prompt_content(conn, prompt3_id, 0, "text", '{"text": "Run more tests"}')
    response3_id = insert_response(
        conn, conv3_id, prompt3_id, model_id, None, "r3", "2024-01-17T10:00:01Z",
        input_tokens=150, output_tokens=100,
    )
    tc3_id = insert_tool_call(
        conn, response3_id, conv3_id, tool_id, "tc3",
        '{"command": "pytest -v"}', '{"output": "OK"}', "success", "2024-01-17T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc3_id, test_tag_id)

    conn.commit()
    conn.close()

    return db_path


@pytest.fixture
def test_db_with_ingested_files(tmp_path):
    """Create a test database with conversations linked to ingested files.

    Used by active session exclusion tests.
    """
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "claude_code", source="local", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")

    active_conv_id = insert_conversation(
        conn,
        external_id="active-conv",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )
    insert_prompt(conn, active_conv_id, "p1", "2024-01-15T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/abc/session-active.jsonl",
        "hash_active",
        active_conv_id,
    )

    inactive_conv_id = insert_conversation(
        conn,
        external_id="inactive-conv",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-14T10:00:00Z",
    )
    insert_prompt(conn, inactive_conv_id, "p2", "2024-01-14T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/abc/session-old.jsonl",
        "hash_old",
        inactive_conv_id,
    )

    active2_conv_id = insert_conversation(
        conn,
        external_id="active-conv-2",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-16T10:00:00Z",
    )
    insert_prompt(conn, active2_conv_id, "p3", "2024-01-16T10:00:00Z")
    record_ingested_file(
        conn,
        "/home/user/.claude/projects/xyz/session-active2.jsonl",
        "hash_active2",
        active2_conv_id,
    )

    conn.commit()
    conn.close()

    return {
        "db_path": db_path,
        "active_conv_id": active_conv_id,
        "inactive_conv_id": inactive_conv_id,
        "active2_conv_id": active2_conv_id,
    }
