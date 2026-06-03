"""Behavioral tests for the workspace-detail Operation (api.stats.workspace_detail).

Built on a real schema via the storage builders so the reused list_conversations
(recent sessions) runs for real. Verifies the stat grid + by-model mix + recent,
ULID addressing, owner-scoping, and None on unknown id.
"""

from painted import Fidelity

from siftd.api.stats import workspace_detail
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
)

_F = Fidelity()


def _build(db_path):
    conn = create_database(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    model = get_or_create_model(conn, "claude-3-opus")
    ws_a = get_or_create_workspace(conn, "/test/projA", "2024-01-01T00:00:00Z")
    ws_b = get_or_create_workspace(conn, "/test/projB", "2024-01-01T00:00:00Z")

    def _conv(ext, ws, started, owner, inp, out):
        cid = insert_conversation(
            conn, external_id=ext, harness_id=harness, workspace_id=ws, started_at=started,
        )
        pid = insert_prompt(conn, cid, ext + "p", started)
        insert_prompt_content(conn, pid, 0, "text", '{"text": "hi"}')
        insert_response(
            conn, cid, pid, model, None, ext + "r", started,
            input_tokens=inp, output_tokens=out,
        )
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, owner, None, started),
        )
        return cid

    _conv("cA1", ws_a, "2024-01-15T10:00:00Z", "alice", 100, 50)
    _conv("cA2", ws_a, "2024-01-16T10:00:00Z", "bob", 1000, 500)
    _conv("cB1", ws_b, "2024-01-17T10:00:00Z", "alice", 7, 3)
    conn.commit()
    conn.close()
    return ws_a, ws_b


def test_workspace_detail_unscoped_aggregates_whole_workspace(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)

    d = workspace_detail(ws_a, fidelity=_F, db_path=db)
    assert d is not None
    assert d.id == ws_a
    assert d.path == "/test/projA"
    assert d.sessions == 2
    assert d.input_tokens == 1100  # 100 + 1000
    assert d.output_tokens == 550  # 50 + 500
    assert {g.name for g in d.model_mix} == {"claude-3-opus"}
    assert len(d.recent) == 2


def test_workspace_detail_scopes_to_owner(tmp_path):
    db = tmp_path / "d.db"
    ws_a, _ = _build(db)

    d = workspace_detail(ws_a, fidelity=_F, db_path=db, owner="alice")
    assert d is not None
    # Only Alice's conversation in workspace A (cA1), not Bob's (cA2).
    assert d.sessions == 1
    assert d.input_tokens == 100
    assert d.output_tokens == 50
    assert len(d.recent) == 1


def test_workspace_detail_unknown_id_returns_none(tmp_path):
    db = tmp_path / "d.db"
    _build(db)
    assert workspace_detail("01HNOPE", fidelity=_F, db_path=db) is None
