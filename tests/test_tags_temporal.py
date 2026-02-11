"""Tests for list_tags temporal filtering."""

from siftd.storage.sqlite import (
    create_database,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_tool_call,
)
from siftd.storage.sqlite import get_or_create_harness, get_or_create_model, get_or_create_tool, get_or_create_workspace
from siftd.storage.tags import apply_tag, get_or_create_tag, list_tags


def _build_db(tmp_path):
    """Build a DB with tagged conversations and tool calls across dates."""
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
    ws_id = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    model_id = get_or_create_model(conn, "test-model")
    tool_id = get_or_create_tool(conn, "shell.execute")

    tag_old = get_or_create_tag(conn, "old-work")
    tag_new = get_or_create_tag(conn, "new-work")
    tag_shell = get_or_create_tag(conn, "shell:test")

    # Old conversation: Jan 10
    conv1 = insert_conversation(
        conn, external_id="c1", harness_id=harness_id,
        workspace_id=ws_id, started_at="2024-01-10T10:00:00Z",
    )
    apply_tag(conn, "conversation", conv1, tag_old)
    p1 = insert_prompt(conn, conv1, "p1", "2024-01-10T10:00:00Z")
    insert_prompt_content(conn, p1, 0, "text", '{"text": "old"}')
    r1 = insert_response(conn, conv1, p1, model_id, None, "r1", "2024-01-10T10:00:01Z")
    tc1 = insert_tool_call(
        conn, r1, conv1, tool_id, "tc1",
        '{"command": "pytest"}', '{"output": "ok"}', "success", "2024-01-10T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc1, tag_shell)

    # New conversation: Jan 20
    conv2 = insert_conversation(
        conn, external_id="c2", harness_id=harness_id,
        workspace_id=ws_id, started_at="2024-01-20T10:00:00Z",
    )
    apply_tag(conn, "conversation", conv2, tag_new)
    p2 = insert_prompt(conn, conv2, "p2", "2024-01-20T10:00:00Z")
    insert_prompt_content(conn, p2, 0, "text", '{"text": "new"}')
    r2 = insert_response(conn, conv2, p2, model_id, None, "r2", "2024-01-20T10:00:01Z")
    tc2 = insert_tool_call(
        conn, r2, conv2, tool_id, "tc2",
        '{"command": "pytest -v"}', '{"output": "ok"}', "success", "2024-01-20T10:00:01Z",
    )
    apply_tag(conn, "tool_call", tc2, tag_shell)

    conn.commit()
    return conn


def test_list_tags_no_filter(tmp_path):
    """Without temporal filters, all tags and counts are returned."""
    conn = _build_db(tmp_path)
    tags = list_tags(conn)
    by_name = {t["name"]: t for t in tags}

    assert by_name["old-work"]["conversation_count"] == 1
    assert by_name["new-work"]["conversation_count"] == 1
    assert by_name["shell:test"]["tool_call_count"] == 2
    conn.close()


def test_list_tags_since(tmp_path):
    """--since filters conversation and tool_call counts to the window."""
    conn = _build_db(tmp_path)
    tags = list_tags(conn, since="2024-01-15T00:00:00Z")
    by_name = {t["name"]: t for t in tags}

    # old-work conversation is before the window
    assert by_name["old-work"]["conversation_count"] == 0
    # new-work is in window
    assert by_name["new-work"]["conversation_count"] == 1
    # shell:test: only the tool_call on conv2 (Jan 20) is in window
    assert by_name["shell:test"]["tool_call_count"] == 1
    conn.close()


def test_list_tags_before(tmp_path):
    """--before filters to conversations before the cutoff."""
    conn = _build_db(tmp_path)
    tags = list_tags(conn, before="2024-01-15T00:00:00Z")
    by_name = {t["name"]: t for t in tags}

    assert by_name["old-work"]["conversation_count"] == 1
    assert by_name["new-work"]["conversation_count"] == 0
    assert by_name["shell:test"]["tool_call_count"] == 1
    conn.close()


def test_list_tags_since_and_before(tmp_path):
    """Both --since and --before narrow to a specific window."""
    conn = _build_db(tmp_path)
    # Window that excludes both conversations
    tags = list_tags(conn, since="2024-01-12T00:00:00Z", before="2024-01-15T00:00:00Z")
    by_name = {t["name"]: t for t in tags}

    assert by_name["old-work"]["conversation_count"] == 0
    assert by_name["new-work"]["conversation_count"] == 0
    assert by_name["shell:test"]["tool_call_count"] == 0
    conn.close()
