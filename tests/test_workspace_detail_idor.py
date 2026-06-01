"""COMMIT-A correctness/security tests for workspace_detail.

Two concerns, both exercised at the API level (not route level) because route
tests pass SimpleNamespace() → owner resolves to None and never exercises
owner scoping:

A1. Cross-tenant read IDOR: an owner-scoped caller who owns zero conversations
    in a workspace must get None (not a detail leaking path + git_remote),
    while owner=None stays fully unscoped.

A2. recent uses exact workspace identity, not a path substring: two workspaces
    whose paths are substrings (/foo and /foo-bar) must not bleed recent
    conversations across the exact-ULID boundary.
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
    """Two workspaces with substring paths; bob owns all of ws_foo, alice none.

    ws_foo  ("/foo")     -> cF1 (bob), cF2 (bob)
    ws_foobar ("/foo-bar") -> cB1 (alice)
    """
    conn = create_database(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    model = get_or_create_model(conn, "claude-3-opus")
    ws_foo = get_or_create_workspace(conn, "/foo", "2024-01-01T00:00:00Z")
    ws_foobar = get_or_create_workspace(conn, "/foo-bar", "2024-01-01T00:00:00Z")

    def _conv(ext, ws, started, owner):
        cid = insert_conversation(
            conn, external_id=ext, harness_id=harness, workspace_id=ws, started_at=started,
        )
        pid = insert_prompt(conn, cid, ext + "p", started)
        insert_prompt_content(conn, pid, 0, "text", '{"text": "hi"}')
        insert_response(
            conn, cid, pid, model, None, ext + "r", started,
            input_tokens=10, output_tokens=5,
        )
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, owner, None, started),
        )
        return cid

    cf1 = _conv("cF1", ws_foo, "2024-01-15T10:00:00Z", "bob")
    cf2 = _conv("cF2", ws_foo, "2024-01-16T10:00:00Z", "bob")
    cbar = _conv("cB1", ws_foobar, "2024-01-17T10:00:00Z", "alice")
    conn.commit()
    conn.close()
    return ws_foo, ws_foobar, cf1, cf2, cbar


# -- A1: cross-tenant read IDOR --------------------------------------------


def test_idor_foreign_workspace_returns_none(tmp_path):
    """alice owns zero conversations in ws_foo (bob owns both) → None."""
    db = tmp_path / "d.db"
    ws_foo, _, _, _, _ = _build(db)

    assert workspace_detail(ws_foo, fidelity=_F, db_path=db, owner="alice") is None


def test_owned_workspace_returns_detail(tmp_path):
    """bob owns both conversations in ws_foo → a real detail."""
    db = tmp_path / "d.db"
    ws_foo, _, _, _, _ = _build(db)

    d = workspace_detail(ws_foo, fidelity=_F, db_path=db, owner="bob")
    assert d is not None
    assert d.id == ws_foo
    assert d.sessions == 2


def test_unscoped_owner_none_stays_unscoped(tmp_path):
    """owner=None (single-tenant/local) must see the workspace regardless."""
    db = tmp_path / "d.db"
    ws_foo, _, _, _, _ = _build(db)

    d = workspace_detail(ws_foo, fidelity=_F, db_path=db, owner=None)
    assert d is not None
    assert d.id == ws_foo
    assert d.sessions == 2


# -- A2: recent uses exact workspace identity, not a path substring ---------


def test_recent_does_not_bleed_substring_workspace(tmp_path):
    """/foo's recent must never contain /foo-bar's conversation."""
    db = tmp_path / "d.db"
    ws_foo, _, cf1, cf2, cbar = _build(db)

    d = workspace_detail(ws_foo, fidelity=_F, db_path=db)
    assert d is not None
    recent_ids = {c.id for c in d.recent}
    assert recent_ids == {cf1, cf2}
    assert cbar not in recent_ids
