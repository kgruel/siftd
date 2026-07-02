from __future__ import annotations

import pytest

from siftd.api import create_database, open_database
from siftd.api.tags import (
    apply_tag,
    apply_tags,
    delete_tag_safe,
    get_or_create_tag,
    modify_target_tag,
    rename_tag_safe,
)


def _conversation_ids(db_path):
    conn = open_database(db_path)
    try:
        rows = conn.execute("SELECT id FROM conversations ORDER BY started_at ASC").fetchall()
        return [row["id"] for row in rows]
    finally:
        conn.close()


def test_apply_tags_apply_already_and_remove_statuses(test_db):
    conv1, conv2 = _conversation_ids(test_db)[:2]

    first = apply_tags(db_path=test_db, tags=["topic:alpha"], entity_id=conv1)
    assert first.action == "apply"
    assert first.target_count == 1
    assert first.resolved_entity_id == conv1
    assert first.results[0].status == "applied"
    assert first.results[0].count == 1

    second = apply_tags(db_path=test_db, tags=["topic:alpha"], entity_id=conv1)
    assert second.results[0].status == "already_applied"
    assert second.results[0].count == 0

    not_applied = apply_tags(db_path=test_db, tags=["topic:alpha"], entity_id=conv2, remove=True)
    assert not_applied.results[0].status == "not_applied"
    assert not_applied.results[0].count == 0

    missing = apply_tags(db_path=test_db, tags=["topic:missing"], entity_id=conv1, remove=True)
    assert missing.results[0].status == "not_found"
    assert missing.results[0].count == 0


def test_modify_target_tag_element_roundtrip_and_resolved_kind(test_db):
    """modify_target_tag resolves a wire (entity_type, entity_id) to a canonical
    (kind, ULID), mutates, and returns the target's updated tags. The resolved
    kind is authoritative — used for the audit + fragment."""
    conn = open_database(test_db)
    try:
        pid = conn.execute("SELECT id FROM events WHERE kind = 'prompt' LIMIT 1").fetchone()["id"]
    finally:
        conn.close()

    kind, target_id, tags = modify_target_tag("prompt", pid, "flagged", action="apply", db_path=test_db)
    assert kind == "prompt"
    assert target_id == pid
    # tags are (name, kind) pairs so each chip re-posts against its own kind
    assert ("flagged", "prompt") in tags

    kind2, _, tags2 = modify_target_tag("prompt", pid, "flagged", action="remove", db_path=test_db)
    assert kind2 == "prompt"
    assert "flagged" not in [name for name, _ in tags2]


def test_modify_target_tag_missing_target_raises_lookup(test_db):
    """A nonexistent target raises LookupError (the serve route maps it to 404)."""
    with pytest.raises(LookupError):
        modify_target_tag("response", "01DOESNOTEXIST0000", "x", action="apply", db_path=test_db)


def test_apply_tags_last_missing_entities(tmp_path):
    db = tmp_path / "empty.db"
    conn = create_database(db)
    conn.close()

    with pytest.raises(FileNotFoundError, match="no matching entities found"):
        apply_tags(db_path=db, tags=["x"], last=1)


def test_apply_tags_owner_scope_restriction(test_db):
    with pytest.raises(PermissionError, match="only supported for conversations"):
        apply_tags(
            db_path=test_db,
            tags=["scope:test"],
            entity_type="workspace",
            entity_id="ws1",
            owner="alice",
        )


def test_rename_and_delete_safe_paths(test_db):
    conv1, _ = _conversation_ids(test_db)[:2]
    apply_tags(db_path=test_db, tags=["rename:me"], entity_id=conv1)

    renamed = rename_tag_safe(db_path=test_db, old_name="rename:me", new_name="renamed:ok")
    assert renamed.status == "renamed"
    assert renamed.old_name == "rename:me"
    assert renamed.new_name == "renamed:ok"

    deleted = delete_tag_safe(db_path=test_db, tag_name="renamed:ok")
    assert deleted.status == "deleted"
    assert deleted.tag_name == "renamed:ok"

    with pytest.raises(FileNotFoundError, match="Tag not found"):
        rename_tag_safe(db_path=test_db, old_name="nope", new_name="new")

    with pytest.raises(FileNotFoundError, match="Tag not found"):
        delete_tag_safe(db_path=test_db, tag_name="renamed:ok")


def test_granular_tag_roundtrip_per_kind(test_db):
    """apply_tags writes to tag_assignments for each granular target_kind."""
    conn = open_database(test_db)
    try:
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        prompt_id = conn.execute(
            "SELECT id FROM events WHERE kind='prompt' AND conversation_id=? LIMIT 1",
            (conv_id,),
        ).fetchone()["id"]
        response_id = conn.execute(
            "SELECT id FROM events WHERE kind='response' AND conversation_id=? LIMIT 1",
            (conv_id,),
        ).fetchone()["id"]
    finally:
        conn.close()

    for kind, eid in [("prompt", prompt_id), ("response", response_id), ("exchange", prompt_id)]:
        result = apply_tags(db_path=test_db, tags=[f"test:{kind}"], entity_type=kind, entity_id=eid)
        assert result.results[0].status == "applied", f"kind={kind}"

    conn = open_database(test_db)
    try:
        rows = conn.execute(
            "SELECT target_kind FROM tag_assignments WHERE target_id IN (?, ?)",
            (prompt_id, response_id),
        ).fetchall()
        kinds_found = {r["target_kind"] for r in rows}
    finally:
        conn.close()

    assert "prompt" in kinds_found
    assert "response" in kinds_found
    assert "exchange" in kinds_found


def test_granular_tag_cross_kind_isolation(test_db):
    """Tagging a prompt event does not affect response queries and vice versa."""
    conn = open_database(test_db)
    try:
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        prompt_id = conn.execute(
            "SELECT id FROM events WHERE kind='prompt' AND conversation_id=? LIMIT 1",
            (conv_id,),
        ).fetchone()["id"]
        response_id = conn.execute(
            "SELECT id FROM events WHERE kind='response' AND conversation_id=? LIMIT 1",
            (conv_id,),
        ).fetchone()["id"]
    finally:
        conn.close()

    apply_tags(db_path=test_db, tags=["isolation:prompt-only"], entity_type="prompt", entity_id=prompt_id)

    conn = open_database(test_db)
    try:
        prompt_count = conn.execute(
            "SELECT COUNT(*) FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id "
            "WHERE ta.target_kind='prompt' AND ta.target_id=? AND t.name='isolation:prompt-only'",
            (prompt_id,),
        ).fetchone()[0]
        response_count = conn.execute(
            "SELECT COUNT(*) FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id "
            "WHERE ta.target_kind='response' AND ta.target_id=? AND t.name='isolation:prompt-only'",
            (response_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert prompt_count == 1
    assert response_count == 0


def test_exchange_and_prompt_coexist_on_same_event(test_db):
    """exchange and prompt tags can coexist on the same event ID (different target_kind)."""
    conn = open_database(test_db)
    try:
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        prompt_id = conn.execute(
            "SELECT id FROM events WHERE kind='prompt' AND conversation_id=? LIMIT 1",
            (conv_id,),
        ).fetchone()["id"]
    finally:
        conn.close()

    apply_tags(db_path=test_db, tags=["label:prompt-level"], entity_type="prompt", entity_id=prompt_id)
    apply_tags(db_path=test_db, tags=["label:exchange-level"], entity_type="exchange", entity_id=prompt_id)

    conn = open_database(test_db)
    try:
        prompt_tag = conn.execute(
            "SELECT t.name FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id "
            "WHERE ta.target_kind='prompt' AND ta.target_id=?",
            (prompt_id,),
        ).fetchone()
        exchange_tag = conn.execute(
            "SELECT t.name FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id "
            "WHERE ta.target_kind='exchange' AND ta.target_id=?",
            (prompt_id,),
        ).fetchone()
        row_count = conn.execute(
            "SELECT COUNT(*) FROM tag_assignments WHERE target_id=?",
            (prompt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert prompt_tag["name"] == "label:prompt-level"
    assert exchange_tag["name"] == "label:exchange-level"
    assert row_count == 2  # two distinct rows for same event_id, different target_kind


def test_apply_tags_rejects_invalid_granular_kind(test_db):
    """apply_tags raises ValueError for unknown entity types."""
    conn = open_database(test_db)
    try:
        event_id = conn.execute("SELECT id FROM events LIMIT 1").fetchone()["id"]
    finally:
        conn.close()

    with pytest.raises(ValueError, match="Unsupported entity_type"):
        apply_tags(db_path=test_db, tags=["x"], entity_type="nonsense", entity_id=event_id)


def test_rename_delete_owner_protection(test_db):
    conv1, _ = _conversation_ids(test_db)[:2]
    conn = open_database(test_db)
    try:
        tag_id = get_or_create_tag(conn, "protected:tag")
        apply_tag(conn, "conversation", conv1, tag_id, commit=False)
        conn.execute(
            "INSERT OR REPLACE INTO conversation_owners (conversation_id, user_id, push_id, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            (conv1, "bob", None, "2026-03-28T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PermissionError, match="another owner"):
        rename_tag_safe(db_path=test_db, old_name="protected:tag", new_name="protected:renamed", owner="alice")

    with pytest.raises(PermissionError, match="another owner"):
        delete_tag_safe(db_path=test_db, tag_name="protected:tag", owner="alice")


def test_modify_target_tag_workspace_blocked_under_owner_scope(test_db):
    """Workspace tags are shared-dimension state: owner-scoped callers may not
    mutate them through modify_target_tag (LookupError → the route's 404, so
    existence isn't leaked). Unscoped callers are unaffected.

    Regression: the workspace resolution arm ignored owner entirely, so any
    authed tenant could read/write any workspace's tags via ``POST /tag``.
    """
    conn = open_database(test_db)
    try:
        ws_id = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()["id"]
    finally:
        conn.close()

    # Unscoped (single-user) works.
    kind, target_id, tags = modify_target_tag(
        "workspace", ws_id, "infra", action="apply", db_path=test_db
    )
    assert kind == "workspace" and target_id == ws_id
    assert ("infra", "workspace") in tags

    # Owner-scoped is refused 404-shaped — even for a participant.
    with pytest.raises(LookupError):
        modify_target_tag(
            "workspace", ws_id, "sneaky", action="apply", db_path=test_db, owner="alice"
        )


def test_owner_participation_covers_block_tags(test_db):
    """Both polarities of the participation guard see block-kind assignments.

    Regression: the event arm enumerated only the four event kinds, so a tag
    whose only usage was a block was (a) deletable/renamable across tenants —
    tag_used_by_other_owners returned False — and (b) unpinnable by its own
    owner — owner_uses_tag returned False.
    """
    from siftd.storage.tags import owner_uses_tag, tag_used_by_other_owners

    conv1 = _conversation_ids(test_db)[0]
    conn = open_database(test_db)
    try:
        block_id = conn.execute(
            "SELECT ec.id FROM event_content ec JOIN events e ON e.id = ec.event_id "
            "WHERE e.conversation_id = ? LIMIT 1",
            (conv1,),
        ).fetchone()["id"]
        tag_id = get_or_create_tag(conn, "block:only")
        apply_tag(conn, "block", block_id, tag_id, commit=False)
        conn.execute(
            "INSERT OR REPLACE INTO conversation_owners (conversation_id, user_id, push_id, assigned_at) "
            "VALUES (?, ?, ?, ?)",
            (conv1, "bob", None, "2026-03-28T00:00:00Z"),
        )
        conn.commit()

        # bob owns the conversation holding the tagged block: he uses the tag,
        # and to alice it is another owner's.
        assert owner_uses_tag(conn, tag_id, "bob") is True
        assert tag_used_by_other_owners(conn, tag_id, "alice") is True
        # Inverses hold too.
        assert owner_uses_tag(conn, tag_id, "alice") is False
        assert tag_used_by_other_owners(conn, tag_id, "bob") is False
    finally:
        conn.close()

    # The guard actually blocks the cross-tenant delete/rename.
    with pytest.raises(PermissionError, match="another owner"):
        delete_tag_safe(db_path=test_db, tag_name="block:only", owner="alice")
