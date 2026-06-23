"""Workspace pins + list sort: owner-scoped pin state, idempotent writes, the
pinned flag surfaced through list_workspaces, the recency/sessions/tokens/cost
sorts, and the read-only guard when workspace_pins is absent.

Base-lane (no serve): exercises the storage + api layers directly — an exact
mirror of test_tag_pins. The serve end-to-end / IDOR path lives in the serve
lane (test_serve_swiss_shell.py).
"""

from __future__ import annotations

from pathlib import Path

from siftd.api.stats import list_workspaces, set_workspace_pin
from siftd.storage.queries import has_workspace_pins_table
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_workspace,
    insert_conversation,
    open_database,
)


def _seed(path: Path):
    """Two workspaces where session-order != recency-order, with usage so the
    token/cost sorts are exercised:

      /a : 2 sessions, older activity, few tokens, no priced usage (cost NULL)
      /b : 1 session,  newer activity, many tokens, priced ($5)
    """
    conn = create_database(path)
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    wa = get_or_create_workspace(conn, "/a", "2026-01-01T00:00:00Z")
    wb = get_or_create_workspace(conn, "/b", "2026-01-01T00:00:00Z")

    def conv(ext, wid, started):
        return insert_conversation(
            conn, external_id=ext, harness_id=h, workspace_id=wid, started_at=started
        )

    a1 = conv("a1", wa, "2026-01-10T00:00:00Z")
    conv("a2", wa, "2026-01-11T00:00:00Z")  # /a latest activity = Jan 11
    b1 = conv("b1", wb, "2026-02-01T00:00:00Z")  # /b latest activity = Feb 1 (newer)

    def usage(cid, inp, out, cost):
        conn.execute(
            "INSERT INTO usage_by_conv_model (conversation_id, input_tokens, output_tokens, cost)"
            " VALUES (?, ?, ?, ?)",
            (cid, inp, out, cost),
        )

    usage(a1, 10, 5, None)  # /a: 15 tokens, unpriced
    usage(b1, 1000, 500, 5.0)  # /b: 1500 tokens, $5
    conn.commit()
    conn.close()
    return path, {"/a": wa, "/b": wb}


def _by_path(rows):
    return {r["path"]: r for r in rows}


# --- table lifecycle -------------------------------------------------------

def test_fresh_db_has_workspace_pins_table(tmp_path):
    # schema.sql carries workspace_pins, so a brand-new DB has it without a write.
    db, _ = _seed(tmp_path / "t.db")
    conn = open_database(db, read_only=True)
    try:
        assert has_workspace_pins_table(conn) is True
    finally:
        conn.close()


def test_list_workspaces_unpinned_when_table_absent(tmp_path):
    """A read-only open of a DB that predates workspace_pins (no write-open since)
    must degrade to 'nothing pinned', never raise 'no such table'."""
    db, _ = _seed(tmp_path / "t.db")
    conn = open_database(db)
    conn.execute("DROP TABLE workspace_pins")
    conn.commit()
    conn.close()

    conn = open_database(db, read_only=True)
    try:
        assert has_workspace_pins_table(conn) is False
        rows = list_workspaces(conn=conn, n=10, with_usage=True)
        assert rows and all(r["pinned"] == 0 for r in rows)
    finally:
        conn.close()


# --- pin round-trip --------------------------------------------------------

def test_set_workspace_pin_round_trip_and_idempotent(tmp_path):
    db, ids = _seed(tmp_path / "t.db")
    wid = ids["/a"]
    assert _by_path(list_workspaces(db_path=db))["/a"]["pinned"] == 0

    assert set_workspace_pin(wid, pinned=True, db_path=db) is True
    assert set_workspace_pin(wid, pinned=True, db_path=db) is False  # already pinned
    assert _by_path(list_workspaces(db_path=db))["/a"]["pinned"] == 1

    assert set_workspace_pin(wid, pinned=False, db_path=db) is True
    assert set_workspace_pin(wid, pinned=False, db_path=db) is False  # already unpinned
    assert _by_path(list_workspaces(db_path=db))["/a"]["pinned"] == 0


def test_set_workspace_pin_nonexistent_is_noop(tmp_path):
    db, _ = _seed(tmp_path / "t.db")
    assert set_workspace_pin("01JZZZNOPE0000000000000000", pinned=True, db_path=db) is False
    assert set_workspace_pin("", pinned=True, db_path=db) is False


def test_pin_requires_owner_participation(tmp_path):
    """Under an owner scope a pin requires participation (a conversation there),
    so a crafted request can't strand a pin on a workspace the owner can't see.
    Unpin is always allowed (it only removes state)."""
    db, ids = _seed(tmp_path / "t.db")
    conn = open_database(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    # alice owns /a's conversations; bob owns /b's — alice participates in /a only.
    rows = conn.execute(
        "SELECT c.id, w.path FROM conversations c JOIN workspaces w ON w.id = c.workspace_id"
    ).fetchall()
    for cid, path in rows:
        owner = "alice" if path == "/a" else "bob"
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, owner, None, "2026-01-15T10:00:00Z"),
        )
    conn.commit()
    conn.close()

    assert set_workspace_pin(ids["/a"], pinned=True, db_path=db, owner="alice") is True
    # /b is bob's — alice can't pin what her owner-scoped list can't show.
    assert set_workspace_pin(ids["/b"], pinned=True, db_path=db, owner="alice") is False
    paths = {r["path"]: r for r in list_workspaces(db_path=db, owner="alice", with_usage=True)}
    assert "/b" not in paths
    assert paths["/a"]["pinned"] == 1
    # unpin needs no participation check (removes state); a no-op pin returns False.
    assert set_workspace_pin(ids["/b"], pinned=False, db_path=db, owner="alice") is False


def test_pins_are_owner_scoped(tmp_path):
    db, ids = _seed(tmp_path / "t.db")
    # Give alice ownership of every conversation so her owner-scoped list is non-empty.
    conn = open_database(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    for (cid,) in conn.execute("SELECT id FROM conversations").fetchall():
        conn.execute(
            "INSERT INTO conversation_owners VALUES (?,?,?,?)",
            (cid, "alice", None, "2026-01-15T10:00:00Z"),
        )
    conn.commit()
    conn.close()

    assert set_workspace_pin(ids["/a"], pinned=True, db_path=db, owner="alice") is True
    rows = _by_path(list_workspaces(db_path=db, owner="alice", with_usage=True))
    assert rows["/a"]["pinned"] == 1
    # bob owns nothing → empty owner-scoped list, and alice's pin never leaks to him.
    assert list_workspaces(db_path=db, owner="bob", with_usage=True) == []


# --- sort ------------------------------------------------------------------

def test_sort_sessions_vs_recent(tmp_path):
    db, _ = _seed(tmp_path / "t.db")
    sessions = [r["path"] for r in list_workspaces(db_path=db, sort="sessions")]
    assert sessions == ["/a", "/b"]  # /a has 2 sessions, /b has 1
    recent = [r["path"] for r in list_workspaces(db_path=db, sort="recent")]
    assert recent == ["/b", "/a"]  # /b active Feb 1 > /a Jan 11


def test_sort_tokens_and_cost(tmp_path):
    db, _ = _seed(tmp_path / "t.db")
    tokens = [r["path"] for r in list_workspaces(db_path=db, with_usage=True, sort="tokens")]
    assert tokens == ["/b", "/a"]  # /b 1500 tok > /a 15
    cost = [r["path"] for r in list_workspaces(db_path=db, with_usage=True, sort="cost")]
    assert cost == ["/b", "/a"]  # /b $5 priced; /a NULL cost sorts last


def test_token_sort_falls_back_to_sessions_without_usage(tmp_path):
    # tokens/cost need the usage columns; without with_usage they degrade to
    # sessions order rather than referencing columns that don't exist.
    db, _ = _seed(tmp_path / "t.db")
    rows = [r["path"] for r in list_workspaces(db_path=db, sort="tokens")]
    assert rows == ["/a", "/b"]
