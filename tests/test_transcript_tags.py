"""WS3: element tag chips in CLI conversation-detail transcripts."""

import io

from painted import Fidelity, print_block

from siftd.api.conversations import _fetch_conversation_event_tags, get_conversation
from siftd.api.tags import apply_tags
from siftd.output.painted_bridge import render_query_detail_block
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


def test_fetch_conversation_event_tags_batches(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:x"], entity_type="response", entity_id=rid)
    apply_tags(db_path=test_db, tags=["q:review"], entity_type="exchange", entity_id=pid)

    conn = open_database(test_db, read_only=True)
    try:
        tags = _fetch_conversation_event_tags(conn, conv)
    finally:
        conn.close()
    assert tags[rid] == ["docs:x"]
    assert tags[pid] == ["q:review"]  # exchange tag anchors on the prompt id


def test_detail_carries_event_tags(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:x"], entity_type="response", entity_id=rid)
    detail = get_conversation(conv, fidelity=Fidelity(), db_path=test_db)
    assert detail.event_tags.get(rid) == ["docs:x"]


def test_transcript_renders_element_chips(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:x"], entity_type="response", entity_id=rid)
    apply_tags(db_path=test_db, tags=["q:review"], entity_type="exchange", entity_id=pid)

    detail = get_conversation(conv, fidelity=Fidelity(), db_path=test_db)
    block = render_query_detail_block(detail, turns=detail.turns, fidelity=Fidelity())
    buf = io.StringIO()
    print_block(block, buf, use_ansi=False)
    out = buf.getvalue()
    assert "#docs:x" in out
    assert "#q:review" in out
