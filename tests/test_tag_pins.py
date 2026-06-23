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


def test_cannot_pin_tag_the_owner_does_not_use(tmp_path):
    """IDOR guard, mirroring the workspace-pin participation check: a tenant who
    owns nothing tagged 'alpha' must not be able to pin it — else the pin would
    surface a foreign tenant's tag NAME (a cross-tenant existence oracle) while
    its owner-scoped counts stay zero."""
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

    # bob owns nothing tagged 'alpha' → the pin write is denied (no state change)…
    assert set_tag_pin("alpha", pinned=True, db_path=db, owner="bob") is False
    # …and 'alpha' never enters bob's owner-scoped view, pinned or otherwise.
    assert "alpha" not in [t.name for t in list_tags(db_path=db, owner="bob")]
    # alice, who uses the tag, can still pin it; the unscoped caller keeps the
    # existence-only guard (owner=None bypasses participation).
    assert set_tag_pin("alpha", pinned=True, db_path=db, owner="alice") is True
    assert set_tag_pin("alpha", pinned=True, db_path=db) is True


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


def test_pinned_tag_survives_when_owner_stops_using_it(tmp_path):
    """Regression: a pinned tag the owner no longer uses must stay visible (and
    therefore un-pinnable). Untagging doesn't cascade to tag_pins, so dropping it
    by the zero-count filter would orphan the pin — unreachable forever."""
    from siftd.storage.tags import get_tag_id, remove_tag

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

    conn = open_database(db)
    remove_tag(conn, "conversation", cid, get_tag_id(conn, "alpha"), commit=True)
    conn.close()

    tags = list_tags(db_path=db, owner="alice")
    alpha = next((t for t in tags if t.name == "alpha"), None)
    assert alpha is not None, "pinned tag was orphaned by the zero-count filter"
    assert alpha.pinned is True
    assert alpha.conversation_count == 0  # shown with a zero count, still unpinnable


def test_render_tags_dominant_unit_per_grain():
    """The headline 'most used' logic: each tag shows its DOMINANT count with the
    true unit, and a tie resolves to 'conv'. Fixtures elsewhere are conv-only, so
    the calls/prompts branches + tie-break would otherwise be wholly untested."""
    from siftd.api.tags import TagInfo
    from siftd.output.html_fmt import _tag_weight, render_tags

    def mk(name: str, **counts) -> TagInfo:
        base = dict(
            conversation_count=0, workspace_count=0, tool_call_count=0,
            exchange_count=0, prompt_count=0, response_count=0,
        )
        base.update(counts)
        return TagInfo(name=name, description=None, created_at="", **base)

    calls = mk("shell:test", tool_call_count=198)
    prompts = mk("ask", prompt_count=40)
    tie = mk("both", conversation_count=5, tool_call_count=5)

    assert _tag_weight(calls) == (198, "calls")
    assert _tag_weight(prompts) == (40, "prompts")
    assert _tag_weight(tie) == (5, "conv")  # strict-greater + conv-first → ties to conv

    html = render_tags(
        [calls, prompts, tie], list_base="/find", shell_base="/", pin_action_url="/tag/pin"
    )
    assert '<b class="idx-loc__n">198</b><i>calls</i>' in html
    assert '<b class="idx-loc__n">40</b><i>prompts</i>' in html
    assert '<b class="idx-loc__n">5</b><i>conv</i>' in html
