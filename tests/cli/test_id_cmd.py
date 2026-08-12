"""Tests for 'siftd id' command - ULID classification."""

from __future__ import annotations

import json

import pytest
from conftest import unambiguous_prefix

from siftd.cli import main
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
def id_test_db(tmp_path):
    """Database with both conversation and event for testing classification."""
    db_path = tmp_path / "id_test.db"
    conn = create_database(db_path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    ws = get_or_create_workspace(conn, "/code/test", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-3-opus")
    t = get_or_create_tool(conn, "shell.execute")

    c = insert_conversation(
        conn, external_id="c1", harness_id=h,
        workspace_id=ws, started_at="2024-01-15T10:00:00Z"
    )
    p = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, p, 0, "text", '{"text": "q"}')
    r = insert_response(
        conn, c, p, m, None, "r1",
        "2024-01-15T10:00:01Z", input_tokens=1, output_tokens=1
    )
    insert_response_content(conn, r, 0, "text", '{"text": "answer"}')
    tc = insert_tool_call(
        conn, r, c, t, "tc1",
        '{"command": "ls"}', '"ok"', "success",
        "2024-01-15T10:00:01Z"
    )

    conn.commit()
    conn.close()
    return db_path, c, p, r, tc


# 12 chars, the width `short_id` prints. A ULID's first 10 chars are its
# millisecond timestamp, so ids minted in one ingest millisecond agree on all
# of them and differ only in the 10 random bits chars 11-12 carry — these are
# hand-picked rather than minted so the collision is certain, not ~1/1024 (#33).
COLLIDING_PREFIX = "01ZZZZZZZZAB"


@pytest.fixture
def collide_db(tmp_path):
    """One conversation and two events all sharing ``COLLIDING_PREFIX``.

    The conversation shares one char *more* with the first event, so a
    13-char prefix isolates the cross-kind (conversation vs event) collision
    from the within-events one.
    """
    db_path = tmp_path / "collide.db"
    conn = create_database(db_path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    ws = get_or_create_workspace(conn, "/code/test", "2024-01-01T00:00:00Z")

    conv = COLLIDING_PREFIX + "C" + "0123456789ABC"
    events = [COLLIDING_PREFIX + "C" + "DEFGHJKMNPQRS", COLLIDING_PREFIX + "Q" + "TVWXYZ0123456"]
    assert len({len(i) for i in (conv, *events)}) == 1 and len(conv) == 26

    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at)"
        " VALUES (?, 'c1', ?, ?, '2024-01-15T10:00:00Z')", (conv, h, ws),
    )
    for i, event_id in enumerate(events):
        conn.execute(
            "INSERT INTO events (id, conversation_id, kind, external_id, timestamp)"
            " VALUES (?, ?, 'prompt', ?, '2024-01-15T10:00:00Z')", (event_id, conv, f"p{i}"),
        )
    conn.commit()
    conn.close()
    return db_path, conv, events


@pytest.fixture
def id_test_db_multi_turn(tmp_path):
    """Database with multiple turns to validate parent-anchor turn resolution."""
    db_path = tmp_path / "id_test_multi_turn.db"
    conn = create_database(db_path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    ws = get_or_create_workspace(conn, "/code/test", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-3-opus")
    t = get_or_create_tool(conn, "shell.execute")

    c = insert_conversation(
        conn, external_id="c-multi", harness_id=h,
        workspace_id=ws, started_at="2024-01-15T10:00:00Z"
    )

    p1 = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, p1, 0, "text", '{"text": "q1"}')
    r1 = insert_response(
        conn, c, p1, m, None, "r1",
        "2024-01-15T10:00:01Z", input_tokens=1, output_tokens=1
    )
    insert_response_content(conn, r1, 0, "text", '{"text": "a1"}')

    p2 = insert_prompt(conn, c, "p2", "2024-01-15T10:00:02Z")
    insert_prompt_content(conn, p2, 0, "text", '{"text": "q2"}')
    r2 = insert_response(
        conn, c, p2, m, None, "r2",
        "2024-01-15T10:00:03Z", input_tokens=1, output_tokens=1
    )
    insert_response_content(conn, r2, 0, "text", '{"text": "a2"}')

    p3 = insert_prompt(conn, c, "p3", "2024-01-15T10:00:04Z")
    insert_prompt_content(conn, p3, 0, "text", '{"text": "q3"}')
    r3 = insert_response(
        conn, c, p3, m, None, "r3",
        "2024-01-15T10:00:05Z", input_tokens=1, output_tokens=1
    )
    insert_response_content(conn, r3, 0, "text", '{"text": "a3"}')
    tc3 = insert_tool_call(
        conn, r3, c, t, "tc3",
        '{"command": "pwd"}', '"ok"', "success",
        "2024-01-15T10:00:06Z"
    )

    conn.commit()
    conn.close()
    return db_path, r2, tc3


class TestIdClassification:
    """Test ID classification for conversations and events."""

    def test_conversation_id_classification(self, id_test_db, capsys):
        """siftd id <conversation> classifies as conversation."""
        db, c, *_ = id_test_db
        rc = main(["--db", str(db), "id", c])
        assert rc == 0

        output = capsys.readouterr().out
        assert "Conversation" in output
        assert c[:8] in output
        assert "Workspace" in output
        assert "siftd query" in output

    def test_prompt_event_classification(self, id_test_db, capsys):
        """siftd id <prompt_id> classifies as event with correct turn number."""
        db, _c, p, _r, _tc = id_test_db
        rc = main(["--db", str(db), "id", p])
        assert rc == 0

        output = capsys.readouterr().out
        assert "Event" in output
        assert p[:8] in output
        assert "Conversation" in output
        assert "Turn" in output
        assert "siftd query" in output

    def test_response_event_classification(self, id_test_db, capsys):
        """siftd id <response_id> classifies as event with correct turn number."""
        db, _c, _p, r, _tc = id_test_db
        rc = main(["--db", str(db), "id", r])
        assert rc == 0

        output = capsys.readouterr().out
        assert "Event" in output
        assert r[:8] in output
        assert "Conversation" in output
        assert "Turn" in output
        assert "siftd query" in output

    def test_tool_call_event_classification(self, id_test_db, capsys):
        """siftd id <tool_call_id> classifies as event with correct turn number."""
        db, *_, tc = id_test_db
        rc = main(["--db", str(db), "id", tc])
        assert rc == 0

        output = capsys.readouterr().out
        assert "Event" in output
        assert tc[:8] in output
        assert "Turn" in output

    def test_conversation_prefix_classification(self, id_test_db, capsys):
        """siftd id <conversation_prefix> classifies as conversation."""
        db, c, p, r, tc = id_test_db
        rc = main(["--db", str(db), "id", unambiguous_prefix(c, (p, r, tc))])
        assert rc == 0

        output = capsys.readouterr().out
        assert "Conversation" in output
        assert c[:8] in output

    def test_event_prefix_classification(self, id_test_db, capsys):
        """siftd id <event_prefix> classifies as event."""
        db, c, p, r, tc = id_test_db
        rc = main(["--db", str(db), "id", unambiguous_prefix(r, (c, p, tc))])
        assert rc == 0

        output = capsys.readouterr().out
        assert "Event" in output
        assert r[:8] in output

    def test_unknown_id_returns_error(self, id_test_db):
        """siftd id <unknown> returns exit code 1."""
        db, *_ = id_test_db
        # Plausible ULID that doesn't exist
        rc = main(["--db", str(db), "id", "ZZZZZZZZZZZZZZZZZZZ"])
        assert rc == 1

    def test_prefix_colliding_across_events_returns_exit_2(self, collide_db, capsys):
        """A prefix naming several events exits 2 and lists them (#33).

        `siftd id` used to answer with an arbitrary one of them, because the
        event resolver first-matched instead of raising.
        """
        db, _conv, events = collide_db
        rc = main(["--db", str(db), "id", COLLIDING_PREFIX])
        assert rc == 2

        err = capsys.readouterr().err
        assert f"{COLLIDING_PREFIX!r} matches 2 events" in err
        assert "longer prefix" in err
        for event_id in events:
            assert event_id in err

    def test_prefix_matching_one_conversation_and_one_event(self, collide_db, capsys):
        """The cross-kind branch: exactly one conversation and one event."""
        db, conv, events = collide_db
        # One char deeper names a single event, so the collision is cross-kind.
        prefix = events[0][: len(COLLIDING_PREFIX) + 1]
        assert conv.startswith(prefix)

        rc = main(["--db", str(db), "id", prefix])
        assert rc == 2

        err = capsys.readouterr().err
        assert "Ambiguous ID prefix" in err
        assert "matches both a conversation and an event" in err
        assert f"conversation: {conv}" in err
        assert f"event: {events[0]}" in err

    def test_json_output_conversation(self, id_test_db, capsys):
        """siftd id --json outputs structured conversation classification."""
        db, c, *_ = id_test_db
        rc = main(["--db", str(db), "id", c, "--json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["kind"] == "conversation"
        assert data["id"] == c
        assert "context" in data
        assert "workspace" in data["context"]
        assert "started_at" in data["context"]

    def test_json_output_event(self, id_test_db, capsys):
        """siftd id --json outputs structured event classification."""
        db, _c, _p, r, _tc = id_test_db
        rc = main(["--db", str(db), "id", r, "--json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["kind"] == "event"
        assert data["id"] == r
        assert "context" in data
        assert "conversation_id" in data["context"]

    def test_json_output_prompt_with_turn(self, id_test_db, capsys):
        """siftd id --json outputs correct turn for prompt."""
        db, _c, p, _r, _tc = id_test_db
        rc = main(["--db", str(db), "id", p, "--json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["kind"] == "event"
        assert data["id"] == p
        assert data["context"]["turn"] == 1

    def test_json_output_response_with_turn(self, id_test_db, capsys):
        """siftd id --json outputs correct turn for response (walks parent chain)."""
        db, _c, _p, r, _tc = id_test_db
        rc = main(["--db", str(db), "id", r, "--json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["kind"] == "event"
        assert data["id"] == r
        assert data["context"]["turn"] == 1

    def test_json_output_tool_call_with_turn(self, id_test_db, capsys):
        """siftd id --json outputs correct turn for tool_call (walks full parent chain)."""
        db, _c, _p, _r, tc = id_test_db
        rc = main(["--db", str(db), "id", tc, "--json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["kind"] == "event"
        assert data["id"] == tc
        assert data["context"]["turn"] == 1

    def test_json_output_response_with_turn_in_multi_turn_conversation(self, id_test_db_multi_turn, capsys):
        """siftd id --json resolves response turn from its parent prompt when turn > 1."""
        db, r2, _tc3 = id_test_db_multi_turn
        rc = main(["--db", str(db), "id", r2, "--json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["kind"] == "event"
        assert data["id"] == r2
        assert data["context"]["turn"] == 2

    def test_json_output_tool_call_with_turn_in_multi_turn_conversation(self, id_test_db_multi_turn, capsys):
        """siftd id --json resolves tool_call turn through response->prompt chain when turn > 1."""
        db, _r2, tc3 = id_test_db_multi_turn
        rc = main(["--db", str(db), "id", tc3, "--json"])
        assert rc == 0

        out = capsys.readouterr().out.strip()
        data = json.loads(out)
        assert data["kind"] == "event"
        assert data["id"] == tc3
        assert data["context"]["turn"] == 3


class TestIdHelp:
    """Test help and discovery."""

    def test_id_help_exists(self):
        """siftd id --help exits with code 0."""
        with pytest.raises(SystemExit) as exc_info:
            main(["id", "--help"])
        assert exc_info.value.code == 0

    def test_main_help_includes_id(self):
        """siftd --help output includes 'id' command."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
