"""Tests for 'siftd id' command - ULID classification."""

from __future__ import annotations

import json

import pytest

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
        assert "conversation" in output
        assert c[:8] in output
        assert "workspace" in output
        assert "siftd query" in output

    def test_prompt_event_classification(self, id_test_db, capsys):
        """siftd id <prompt_id> classifies as event with correct turn number."""
        db, _c, p, _r, _tc = id_test_db
        rc = main(["--db", str(db), "id", p])
        assert rc == 0

        output = capsys.readouterr().out
        assert "event" in output
        assert p[:8] in output
        assert "conversation" in output
        assert "turn" in output
        assert "siftd query" in output

    def test_response_event_classification(self, id_test_db, capsys):
        """siftd id <response_id> classifies as event with correct turn number."""
        db, _c, _p, r, _tc = id_test_db
        rc = main(["--db", str(db), "id", r])
        assert rc == 0

        output = capsys.readouterr().out
        assert "event" in output
        assert r[:8] in output
        assert "conversation" in output
        assert "turn" in output
        assert "siftd query" in output

    def test_tool_call_event_classification(self, id_test_db, capsys):
        """siftd id <tool_call_id> classifies as event with correct turn number."""
        db, *_, tc = id_test_db
        rc = main(["--db", str(db), "id", tc])
        assert rc == 0

        output = capsys.readouterr().out
        assert "event" in output
        assert tc[:8] in output
        assert "turn" in output

    def test_conversation_prefix_classification(self, id_test_db, capsys):
        """siftd id <conversation_prefix> classifies as conversation."""
        db, c, *_ = id_test_db
        rc = main(["--db", str(db), "id", c[:12]])
        assert rc == 0

        output = capsys.readouterr().out
        assert "conversation" in output
        assert c[:8] in output

    def test_event_prefix_classification(self, id_test_db, capsys):
        """siftd id <event_prefix> classifies as event."""
        db, _c, _p, r, _tc = id_test_db
        rc = main(["--db", str(db), "id", r[:12]])
        assert rc == 0

        output = capsys.readouterr().out
        assert "event" in output
        assert r[:8] in output

    def test_unknown_id_returns_error(self, id_test_db):
        """siftd id <unknown> returns exit code 1."""
        db, *_ = id_test_db
        # Plausible ULID that doesn't exist
        rc = main(["--db", str(db), "id", "ZZZZZZZZZZZZZZZZZZZ"])
        assert rc == 1

    def test_ambiguous_prefix_returns_exit_2(self, id_test_db, capsys):
        """siftd id <prefix> returns 2 when it matches both conversation and event."""
        db, c, _p, r, _tc = id_test_db
        prefix = ""
        for i in range(1, min(len(c), len(r)) + 1):
            if c[:i] == r[:i]:
                prefix = c[:i]
            else:
                break
        assert prefix

        rc = main(["--db", str(db), "id", prefix])
        assert rc == 2

        err = capsys.readouterr().err
        assert "Ambiguous ID prefix" in err
        assert "matches both a conversation and an event" in err
        assert "conversation:" in err
        assert "event:" in err

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
