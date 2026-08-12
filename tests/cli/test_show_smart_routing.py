"""Phase 4: smart-routing of `siftd show <id>` between conversations and events.

Ported from query <id> after query lost its detail-view positional
(docs/dev/cli-verb-coherence-2026-07-07.md)."""

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
def event_db(tmp_path):
    db_path = tmp_path / "smart.db"
    conn = create_database(db_path)
    h = get_or_create_harness(conn, "h", source="t", log_format="jsonl")
    ws = get_or_create_workspace(conn, "/p", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "claude-3-opus")
    t = get_or_create_tool(conn, "shell.execute")

    c = insert_conversation(conn, external_id="c1", harness_id=h,
                            workspace_id=ws, started_at="2024-01-15T10:00:00Z")
    p = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
    insert_prompt_content(conn, p, 0, "text", '{"text": "q"}')
    r = insert_response(conn, c, p, m, None, "r1",
                        "2024-01-15T10:00:01Z", input_tokens=1, output_tokens=1)
    insert_response_content(conn, r, 0, "text", '{"text": "answer"}')
    tc = insert_tool_call(conn, r, c, t, "tc1",
                          '{"command": "ls"}', '"ok"', "success",
                          "2024-01-15T10:00:01Z")

    conn.commit()
    conn.close()
    return db_path, c, p, r, tc


class TestSmartRouting:
    def test_conversation_id_routes_to_conversation_detail(self, event_db, capsys):
        db, c, *_ = event_db
        rc = main(["--db", str(db), "show", c, "--json"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        # Conversation JSON has "turns"; event JSON has "kind"
        d = json.loads(out)
        assert "turns" in d
        assert d["id"] == c

    def test_event_id_routes_to_event_detail(self, event_db, capsys):
        db, _c, _p, r, _tc = event_db
        rc = main(["--db", str(db), "show", r, "--json"])
        assert rc == 0
        d = json.loads(capsys.readouterr().out.strip())
        assert d["id"] == r
        assert d["kind"] == "response"

    def test_event_id_prefix_routes_to_event_detail(self, event_db, capsys):
        db, c, p, r, tc = event_db
        prefix = unambiguous_prefix(r, (c, p, tc))
        rc = main(["--db", str(db), "show", prefix, "--json"])
        assert rc == 0
        d = json.loads(capsys.readouterr().out.strip())
        assert d["id"] == r

    def test_tool_call_id_routes_to_event_detail(self, event_db, capsys):
        db, *_, tc = event_db
        rc = main(["--db", str(db), "show", tc, "--json"])
        assert rc == 0
        d = json.loads(capsys.readouterr().out.strip())
        assert d["kind"] == "tool_call"
        assert "tool_call" in d

    def test_unknown_id_returns_error(self, event_db, capsys):
        db, *_ = event_db
        # Plausible-looking ULID prefix that doesn't match anything
        rc = main(["--db", str(db), "show", "ZZZZZZZZZZZZZZZZZZZ"])
        assert rc == 1

    def test_neighbors_flag_enables_neighbors_block(self, event_db, capsys):
        db, _c, _p, r, _tc = event_db
        rc = main([
            "--db", str(db), "show", r, "--json", "--neighbors",
        ])
        assert rc == 0
        d = json.loads(capsys.readouterr().out.strip())
        assert "neighbors" in d
        # Single response in fixture: prev/next both None
        assert d["neighbors"]["prev_event_id"] is None
        assert d["neighbors"]["next_event_id"] is None
