from __future__ import annotations

import pytest

from siftd.api import create_database, open_database
from siftd.api.tags import (
    apply_tag,
    apply_tags,
    delete_tag_safe,
    get_or_create_tag,
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
