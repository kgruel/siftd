"""Tag pins: owner-scoped pin state, idempotent writes, the pinned flag surfaced
through list_tags, and the read-only guard when tag_pins is absent.

Base-lane (no serve): exercises the storage + api layers directly. The serve
end-to-end / IDOR path lives in test_serve_swiss_shell.py.
"""

from __future__ import annotations

from pathlib import Path

from siftd.api.tags import list_tags, set_tag_pin
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_workspace,
    insert_conversation,
    open_database,
)
from siftd.storage.tags import (
    apply_tag,
    get_or_create_tag,
    has_tag_pins_table,
)
from siftd.storage.tags import (
    list_tags as _storage_list_tags,
)


def _db_with_tag(path: Path, name: str = "alpha") -> tuple[Path, str]:
    conn = create_database(path)
    h = get_or_create_harness(conn, "claude_code", source="anthropic", log_format="jsonl")
    w = get_or_create_workspace(conn, "/proj", "2026-01-01T00:00:00Z")
    cid = insert_conversation(
        conn, external_id="c1", harness_id=h, workspace_id=w,
        started_at="2026-01-15T10:00:00Z",
    )
    apply_tag(conn, "conversation", cid, get_or_create_tag(conn, name))
    conn.commit()
    conn.close()
    return path, cid


def _pinned(tags, name: str) -> bool:
    return next(t.pinned for t in tags if t.name == name)


def test_fresh_db_has_tag_pins_table(tmp_path):
    # schema.sql carries tag_pins, so a brand-new DB has it without a write-open.
    db, _ = _db_with_tag(tmp_path / "t.db")
    conn = open_database(db, read_only=True)
    try:
        assert has_tag_pins_table(conn) is True
    finally:
        conn.close()


def test_set_tag_pin_round_trip_and_idempotent(tmp_path):
    db, _ = _db_with_tag(tmp_path / "t.db")
    assert _pinned(list_tags(db_path=db), "alpha") is False

    assert set_tag_pin("alpha", pinned=True, db_path=db) is True
    assert set_tag_pin("alpha", pinned=True, db_path=db) is False  # already pinned
    assert _pinned(list_tags(db_path=db), "alpha") is True

    assert set_tag_pin("alpha", pinned=False, db_path=db) is True
    assert set_tag_pin("alpha", pinned=False, db_path=db) is False  # already unpinned
    assert _pinned(list_tags(db_path=db), "alpha") is False


def test_set_tag_pin_nonexistent_tag_is_noop(tmp_path):
    db, _ = _db_with_tag(tmp_path / "t.db")
    assert set_tag_pin("does-not-exist", pinned=True, db_path=db) is False


def test_pins_are_owner_scoped(tmp_path):
    db, cid = _db_with_tag(tmp_path / "t.db")
    conn = open_database(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    conn.execute(
        "INSERT INTO conversation_owners VALUES (?,?,?,?)",
        (cid, "alice", None, "2026-01-15T10:00:00Z"),
    )
    conn.commit()
    conn.close()

    assert set_tag_pin("alpha", pinned=True, db_path=db, owner="alice") is True
    assert _pinned(list_tags(db_path=db, owner="alice"), "alpha") is True
    # bob owns nothing tagged 'alpha' → it isn't even in his owner-scoped list
    assert "alpha" not in [t.name for t in list_tags(db_path=db, owner="bob")]


def test_list_tags_unpinned_when_table_absent(tmp_path):
    """A read-only open of a DB that predates tag_pins (and has had no write-open
    since) must degrade to 'nothing pinned', never raise 'no such table'."""
    db, _ = _db_with_tag(tmp_path / "t.db")
    conn = open_database(db)
    conn.execute("DROP TABLE tag_pins")
    conn.commit()
    conn.close()

    conn = open_database(db, read_only=True)
    try:
        assert has_tag_pins_table(conn) is False
        rows = _storage_list_tags(conn)
        assert rows and all(r["pinned"] is False for r in rows)
    finally:
        conn.close()
