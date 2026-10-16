import pytest

from siftd.api.file_refs import _extract_file_content, _strip_line_numbers


def test_strip_line_numbers_and_extract_content_lists():
    assert "→" not in _strip_line_numbers("     1→line one\n   123→line two")
    out = _extract_file_content('{"content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]}')
    assert "line1" in out and "line2" in out
    assert "line1" in _extract_file_content('{"content": ["line1", "line2"]}')


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('"just a string"', None),
        ('{"other": "data"}', None),
        ('{"content": "hello world"}', "hello world"),
        ('{"output": "hello"}', "hello"),
        ('{"content": []}', None),
        ('{"content": 42}', None),
        (None, None),
    ],
)
def test_extract_content_basics(payload, expected):
    assert _extract_file_content(payload) == expected


def test_fetch_file_refs_edges(test_db):
    from siftd.api.file_refs import fetch_file_refs
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db, read_only=True)
    assert fetch_file_refs(conn, []) == {}
    assert fetch_file_refs(conn, ["no-such-prompt"]) == {}
    conn.close()


def test_a_file_call_without_a_path_is_not_a_reference(tmp_path):
    """Was a fake connection feeding one row with no file_path; now a stored call without one."""
    from siftd.api.file_refs import fetch_file_refs
    from siftd.domain import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall
    from siftd.storage.sqlite import ensure_tool_aliases, get_or_create_harness, open_database, store_conversation

    conn = open_database(tmp_path / "t.db")
    ensure_tool_aliases(conn, get_or_create_harness(conn, "claude_code", source="anthropic"), {"Read": "file.read"})
    ts = "2024-03-01T10:00:00Z"
    text = [ContentBlock(block_type="text", content={"text": "x"})]
    cid = store_conversation(conn, Conversation(
        external_id="c", harness=Harness(name="claude_code", source="anthropic", log_format="jsonl"), started_at=ts,
        prompts=[Prompt(timestamp=ts, external_id="p", content=text, responses=[Response(
            timestamp=ts, external_id="r", content=text,
            tool_calls=[ToolCall(tool_name="Read", input={}, external_id="t0", timestamp=ts),
                        ToolCall(tool_name="Read", input={"file_path": "/x/a.py"}, external_id="t1", timestamp=ts)],
        )])],
    ), commit=True)
    pid = conn.execute("SELECT id FROM events WHERE conversation_id = ? AND kind = 'prompt'", (cid,)).fetchone()[0]
    assert [r.basename for r in fetch_file_refs(conn, [pid])[pid]] == ["a.py"]
    conn.close()
