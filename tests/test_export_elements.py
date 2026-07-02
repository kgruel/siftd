"""WS6: element export (siftd export --view elements --tag X)."""

import json

import pytest
from painted import Fidelity

from siftd.api.export import export_document, export_elements
from siftd.api.tags import apply_tags
from siftd.storage.sqlite import open_database


def _ids(test_db):
    conn = open_database(test_db, read_only=True)
    try:
        conv = conn.execute("SELECT id FROM conversations ORDER BY started_at LIMIT 1").fetchone()["id"]
        pid = conn.execute(
            "SELECT id FROM events WHERE kind='prompt' AND conversation_id=? LIMIT 1", (conv,)
        ).fetchone()["id"]
        rid = conn.execute(
            "SELECT id FROM events WHERE kind='response' AND conversation_id=? LIMIT 1", (conv,)
        ).fetchone()["id"]
    finally:
        conn.close()
    return conv, pid, rid


def test_export_elements_selects_tagged(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="response", entity_id=rid)
    els = export_elements(tag=["docs:thing"], db_path=test_db)
    assert len(els) == 1
    assert els[0].kind == "response"
    assert els[0].event_id == rid
    assert els[0].tags == ["docs:thing"]
    assert els[0].alias.endswith(":response:1")
    assert els[0].text


def test_export_elements_md_golden(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="response", entity_id=rid)
    art = export_document(
        fidelity=Fidelity(depth=3), format="md", tag=["docs:thing"],
        view="elements", db_path=test_db,
    )
    assert art.media_type == "text/markdown"
    assert art.count == 1
    assert "### response ·" in art.content
    assert "tags: docs:thing" in art.content


def test_export_elements_json_shape(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="response", entity_id=rid)
    art = export_document(
        fidelity=Fidelity(depth=3), format="json", tag=["docs:thing"],
        view="elements", db_path=test_db,
    )
    payload = json.loads(art.content)
    assert len(payload) == 1
    assert payload[0]["kind"] == "response"
    assert payload[0]["event_id"] == rid
    assert payload[0]["tags"] == ["docs:thing"]


def test_export_elements_exchange_emits_pair(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["q:pair"], entity_type="exchange", entity_id=pid)
    art = export_document(
        fidelity=Fidelity(depth=3), format="md", tag=["q:pair"],
        view="elements", db_path=test_db,
    )
    assert "### exchange ·" in art.content
    assert "**Response:**" in art.content


def test_export_elements_requires_tag(test_db):
    with pytest.raises(ValueError, match="elements view requires --tag"):
        export_document(fidelity=Fidelity(depth=3), format="md", view="elements", db_path=test_db)


def test_export_default_view_unchanged(test_db):
    # A conversations-view export still works without a tag.
    art = export_document(fidelity=Fidelity(depth=1), format="md", last=1, db_path=test_db)
    assert art.media_type == "text/markdown"
