"""What the assembler produces is what storage can keep, in the order it arrived.

The assembler and storage each state rules the other must honour: storage's
``UNIQUE (conversation_id, kind, external_id)`` and ``events.sequence``, the
assembler's pairing and arrival order. These tests run records through both, so
a stream the assembler accepts cannot fail at ``store_conversation`` (which
rolls back the whole source) and the stored order is the arrival order, not the
order a walk of parent links happens to visit.
"""

from __future__ import annotations

import pytest

from siftd.adapters.assembly import Assembler
from siftd.adapters.records import PatchRec, PromptRec, ResponseRec, Session, ToolCallRec, ToolResultRec, UsageRec
from siftd.domain import Harness, Usage
from siftd.storage.events import get_last_event_id
from siftd.storage.sqlite import create_database, store_conversation

H = Harness(name="t", source="test", log_format="jsonl")
T0 = "2026-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = create_database(tmp_path / "t.db")
    yield c
    c.close()


def store(conn, *records) -> str:
    """Assemble ``records`` as one conversation and store it; returns the conversation id."""
    assembler = Assembler(H)
    for rec in (Session(external_id="t::s", started_at=T0), PromptRec(at=T0), *records):
        assembler.feed(rec)
    (conversation,) = assembler.finish().conversations
    return store_conversation(conn, conversation, commit=True)


def call(name, call_id, parent_key=None):
    return ToolCallRec(at=None, name=name, input={"n": name}, call_id=call_id, parent_key=parent_key)


def stored_calls(conn, cid):
    """``(external_id, input name, result, status)`` of each stored tool call, in recorded order."""
    return [
        (r["external_id"], r["name"], r["result"], r["status"])
        for r in conn.execute(
            "SELECT e.external_id, json_extract(c.input, '$.n') AS name, b.content AS result, c.status"
            " FROM events e JOIN event_tool_call c ON c.event_id = e.id"
            " LEFT JOIN content_blobs b ON b.hash = c.result_hash"
            " WHERE e.conversation_id = ? ORDER BY e.sequence",
            (cid,),
        )
    ]


def test_interleaved_responses_are_stored_in_the_order_their_calls_arrived(conn):
    # R1 -> call1, R2 -> call2, then another chunk of R1 declares call3.
    cid = store(
        conn,
        ResponseRec(at=None, key="r1", external_id="r1"),
        call("one", "call1", "r1"),
        ResponseRec(at=None, key="r2", external_id="r2"),
        call("two", "call2", "r2"),
        ResponseRec(at=None, key="r1", external_id="r1-later"),
        call("three", "call3", "r1"),
    )
    assert [c[0] for c in stored_calls(conn, cid)] == ["call1", "call2", "call3"]
    last = get_last_event_id(conn, cid, "tool_call")
    assert conn.execute("SELECT external_id FROM events WHERE id = ?", (last,)).fetchone()[0] == "call3"
    # Membership still comes from the parent links.
    parents = dict(
        conn.execute(
            "SELECT c.external_id, p.external_id FROM events c JOIN events p ON p.id = c.parent_id"
            " WHERE c.conversation_id = ? AND c.kind = 'tool_call'",
            (cid,),
        ).fetchall()
    )
    assert parents == {"call1": "r1", "call2": "r2", "call3": "r1"}


# Every pairing the assembler supports: a stream, and the calls it must store.
PAIRINGS = {
    "result after call": (
        [call("a", "c"), ToolResultRec(at=None, call_id="c", result={"r": 1})],
        [("c", "a", '{"r": 1}', "success")],
    ),
    "result before call": (
        [ToolResultRec(at=None, call_id="c", result={"r": 1}, status="error"), call("a", "c")],
        [("c", "a", '{"r": 1}', "error")],
    ),
    "call with no result": ([call("a", "c")], [("c", "a", None, "pending")]),
    "two calls sharing an id, results in order": (
        [
            call("a", "c"),
            call("b", "c"),
            ToolResultRec(at=None, call_id="c", result={"n": 1}),
            ToolResultRec(at=None, call_id="c", result={"n": 2}),
        ],
        [("c", "a", '{"n": 1}', "success"), ("c:2", "b", '{"n": 2}', "success")],
    ),
    "two calls sharing an id, results first": (
        [
            ToolResultRec(at=None, call_id="c", result={"n": 1}),
            ToolResultRec(at=None, call_id="c", result={"n": 2}),
            call("a", "c"),
            call("b", "c"),
        ],
        [("c", "a", '{"n": 1}', "success"), ("c:2", "b", '{"n": 2}', "success")],
    ),
    "three calls sharing an id, one result": (
        [call("a", "c"), call("b", "c"), call("d", "c"), ToolResultRec(at=None, call_id="c", result={"n": 1})],
        [("c", "a", '{"n": 1}', "success"), ("c:2", "b", None, "pending"), ("c:3", "d", None, "pending")],
    ),
    "interim results, then the final one": (
        [
            ToolResultRec(at=None, call_id="c", result={"o": "early"}, status="pending", final=False),
            call("a", "c"),
            ToolResultRec(at=None, call_id="c", result={"o": "running"}, status="pending", final=False),
            ToolResultRec(at=None, call_id="c", result={"o": "done"}),
        ],
        [("c", "a", '{"o": "done"}', "success")],
    ),
    "calls waiting for their parent response": (
        [
            ResponseRec(at=None, key="b"),
            call("a", "c", "a"),
            call("b", "c", "b"),
            ToolResultRec(at=None, call_id="c", result={"for": "a"}),
            ResponseRec(at=None, key="a"),
        ],
        [("c", "a", '{"for": "a"}', "success"), ("c:2", "b", None, "pending")],
    ),
    "a result that arrives before its waiting call is placed": (
        [call("a", "c", "later"), ToolResultRec(at=None, call_id="c", result={"ok": 1}), ResponseRec(at=None, key="later")],
        [("c", "a", '{"ok": 1}', "success")],
    ),
    "a call whose parent never appears is not stored": ([call("a", "c", "never")], []),
    "a result with no call is not stored": ([ToolResultRec(at=None, call_id="ghost", result={})], []),
}


@pytest.mark.parametrize("stream, expected", PAIRINGS.values(), ids=PAIRINGS.keys())
def test_every_pairing_the_assembler_supports_can_be_stored(conn, stream, expected):
    cid = store(conn, *stream)
    stored = stored_calls(conn, cid)
    assert sorted(stored) == sorted(expected)
    assert len({c[0] for c in stored}) == len(stored)


def test_a_call_id_reused_across_turns_stays_unique_in_the_conversation(conn):
    cid = store(
        conn,
        call("a", "c"),
        ToolResultRec(at=None, call_id="c", result={}),
        PromptRec(at=None),
        call("b", "c"),
        ToolResultRec(at=None, call_id="c", result={}),
    )
    assert [c[:2] for c in stored_calls(conn, cid)] == [("c", "a"), ("c:2", "b")]


def test_a_continued_message_stores_its_usage_once(conn):
    usage = Usage(input_tokens=10, output_tokens=40)
    cid = store(
        conn,
        ResponseRec(at=None, key="m", external_id="u1", usage=usage),
        call("skill", "c1", "m"),
        PromptRec(at=None, external_id="injected"),
        ResponseRec(at=None, key="m", external_id="u2", usage=usage),
        call("bash", "c2", "m"),
        UsageRec(at=None, usage=Usage(input_tokens=1), target_key="nowhere"),
        PatchRec(at=None, attributes={"x": "1"}, target_key="m"),
    )
    rows = conn.execute(
        "SELECT e.external_id, r.input_tokens, r.output_tokens FROM events e"
        " JOIN event_response r ON r.event_id = e.id WHERE e.conversation_id = ? ORDER BY e.sequence",
        (cid,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [("u1", 10, 40), ("u2", None, None)]
    assert [c[0] for c in stored_calls(conn, cid)] == ["c1", "c2"]
