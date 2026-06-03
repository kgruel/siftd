"""Fidelity-gated tag activity enrichment (api.tags.list_tags + 'activity' tag).

Establishes the result-enrichment pattern: list_tags gains an optional fidelity;
when it carries the 'activity' visible tag, each TagInfo gets a per-week
conversation-activity sparkline (oldest->newest, window anchored to the most
recent conversation so it's deterministic). Without the tag, .activity is None
and the extra query is skipped — thin callers pay nothing.
"""

from painted import Fidelity

from siftd.api.tags import list_tags
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    insert_conversation,
)
from siftd.storage.tags import apply_tag, get_or_create_tag

_ACTIVITY = Fidelity(visible=frozenset({"text", "activity"}))


def _build(db_path):
    conn = create_database(db_path)
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    tag_id = get_or_create_tag(conn, "topic:foo")

    # Anchor (most recent) and a conversation ~2 weeks earlier, both tagged.
    c_now = insert_conversation(
        conn, external_id="cNow", harness_id=harness, workspace_id=None,
        started_at="2026-06-01T00:00:00Z",
    )
    c_wk2 = insert_conversation(
        conn, external_id="cWk2", harness_id=harness, workspace_id=None,
        started_at="2026-05-18T00:00:00Z",  # 14 days earlier -> 2 weeks ago
    )
    apply_tag(conn, "conversation", c_now, tag_id)
    apply_tag(conn, "conversation", c_wk2, tag_id)
    conn.commit()
    return conn


def test_activity_present_and_bucketed_when_requested(tmp_path):
    conn = _build(tmp_path / "t.db")
    try:
        tags = {t.name: t for t in list_tags(conn=conn, fidelity=_ACTIVITY)}
    finally:
        conn.close()

    foo = tags["topic:foo"]
    assert foo.activity is not None
    assert len(foo.activity) == 12
    # Anchor conversation lands in the newest bucket; the 2-weeks-earlier one
    # two buckets back (oldest -> newest ordering).
    assert foo.activity[11] == 1  # most recent week
    assert foo.activity[9] == 1   # ~2 weeks ago
    assert sum(foo.activity) == 2


def test_activity_none_without_fidelity_tag(tmp_path):
    conn = _build(tmp_path / "t.db")
    try:
        # No fidelity, and a fidelity lacking the 'activity' tag — both skip it.
        plain = {t.name: t for t in list_tags(conn=conn)}
        text_only = {t.name: t for t in list_tags(conn=conn, fidelity=Fidelity(visible=frozenset({"text"})))}
    finally:
        conn.close()

    assert plain["topic:foo"].activity is None
    assert text_only["topic:foo"].activity is None


def test_activity_scopes_to_owner(tmp_path):
    conn = create_database(tmp_path / "t.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_owners (conversation_id TEXT,"
        " user_id TEXT, push_id TEXT, assigned_at TEXT)"
    )
    harness = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    tag_id = get_or_create_tag(conn, "topic:foo")
    c_alice = insert_conversation(
        conn, external_id="cA", harness_id=harness, workspace_id=None,
        started_at="2026-06-01T00:00:00Z",
    )
    c_bob = insert_conversation(
        conn, external_id="cB", harness_id=harness, workspace_id=None,
        started_at="2026-06-01T00:00:00Z",
    )
    apply_tag(conn, "conversation", c_alice, tag_id)
    apply_tag(conn, "conversation", c_bob, tag_id)
    conn.executemany(
        "INSERT INTO conversation_owners VALUES (?,?,?,?)",
        [(c_alice, "alice", None, "2026-06-01T00:00:00Z"),
         (c_bob, "bob", None, "2026-06-01T00:00:00Z")],
    )
    conn.commit()
    try:
        tags = {t.name: t for t in list_tags(conn=conn, owner="alice", fidelity=_ACTIVITY)}
    finally:
        conn.close()

    # Only Alice's conversation contributes to her activity series.
    assert tags["topic:foo"].activity[11] == 1
    assert sum(tags["topic:foo"].activity) == 1
