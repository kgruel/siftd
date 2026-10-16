"""A response that precedes every prompt is stored with no parent, not dropped.

The schema and ``insert_response`` always accepted a NULL parent; the domain
tree had no place to put such a response, so eight adapters dropped it and two
invented a prompt for it (#93). ``Conversation.preamble`` is that place. This
pins the storage half: the row exists, its parent is NULL, its text is
searchable, and its tokens reach the rollup.
"""

from __future__ import annotations

from conftest import make_conversation

from siftd.domain import ContentBlock, Response, ToolCall, Usage
from siftd.storage.sqlite import create_database, store_conversation
from siftd.storage.usage_rollup import rebuild_rollups


def _with_preamble():
    conv = make_conversation(external_id="pre-1", input_tokens=100, output_tokens=50)
    conv.preamble = [
        Response(
            timestamp="2024-01-01T09:59:00Z",
            content=[ContentBlock(block_type="text", content={"text": "continuing the forked investigation"})],
            usage=Usage(input_tokens=7, output_tokens=3),
            model="test-model",
            external_id="pre-r0",
            tool_calls=[ToolCall(tool_name="Read", input={"file_path": "/x"}, external_id="t0")],
        )
    ]
    return conv


def test_preamble_response_is_stored_with_a_null_parent(tmp_path):
    conn = create_database(tmp_path / "t.db")
    conv_id = store_conversation(conn, _with_preamble(), commit=True)

    rows = conn.execute(
        "SELECT external_id, parent_id FROM events WHERE conversation_id = ? AND kind = 'response' ORDER BY timestamp",
        (conv_id,),
    ).fetchall()
    assert [(r["external_id"], r["parent_id"] is None) for r in rows] == [("pre-r0", True), ("r1", False)]

    call = conn.execute(
        "SELECT e.parent_id FROM events e WHERE e.conversation_id = ? AND e.kind = 'tool_call'", (conv_id,)
    ).fetchone()
    pre_id = conn.execute("SELECT id FROM events WHERE external_id = 'pre-r0'").fetchone()["id"]
    assert call["parent_id"] == pre_id


def test_preamble_text_is_searchable_and_its_tokens_roll_up(tmp_path):
    conn = create_database(tmp_path / "t.db")
    conv_id = store_conversation(conn, _with_preamble(), commit=True)
    rebuild_rollups(conn, commit=True)

    hits = conn.execute("SELECT conversation_id FROM content_fts WHERE content_fts MATCH 'forked'").fetchall()
    assert [h["conversation_id"] for h in hits] == [conv_id]

    row = conn.execute(
        "SELECT SUM(output_tokens) AS out, SUM(response_count) AS n FROM usage_by_conv_model WHERE conversation_id = ?",
        (conv_id,),
    ).fetchone()
    assert (row["out"], row["n"]) == (53, 2)


def test_a_response_naming_its_provider_is_stored_under_it(tmp_path):
    # A multi-provider harness (pi) records one per message; the harness's own
    # source is only the fallback.
    conn = create_database(tmp_path / "t.db")
    conv = _with_preamble()
    conv.preamble[0].provider = "anthropic"
    conv_id = store_conversation(conn, conv, commit=True)
    rows = conn.execute(
        "SELECT e.external_id, p.name FROM events e JOIN event_response er ON er.event_id = e.id "
        "JOIN providers p ON p.id = er.provider_id WHERE e.conversation_id = ? ORDER BY e.timestamp",
        (conv_id,),
    ).fetchall()
    assert [(r["external_id"], r["name"]) for r in rows] == [("pre-r0", "anthropic"), ("r1", "test")]
