"""WS2: filter-only search enumerates tagged elements; ranked hits carry tags."""

from siftd.api.search import enrich_tags, enumerate_tagged, search_view
from siftd.api.tags import apply_tags
from siftd.domain.search_types import SearchChunk
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


def test_facet_only_returns_element_hits(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="response", entity_id=rid)

    sv = search_view("", db_path=test_db, tag=["docs:thing"])
    assert len(sv.results) == 1
    hit = sv.results[0]
    assert hit["chunk_type"] == "response"
    assert hit["event_id"] == rid
    assert hit["tags"] == ["docs:thing"]
    assert hit["text"]  # excerpt decoded from JSON, not raw


def test_facet_only_tag_kind_scopes(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="response", entity_id=rid)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="prompt", entity_id=pid)

    both = search_view("", db_path=test_db, tag=["docs:thing"])
    assert {h["chunk_type"] for h in both.results} == {"prompt", "response"}

    only = search_view("", db_path=test_db, tag=["docs:thing"], tag_kind=["response"])
    assert [h["chunk_type"] for h in only.results] == ["response"]


def test_facet_only_conversation_kind_yields_no_element_hits(test_db):
    conv, _, _ = _ids(test_db)
    apply_tags(db_path=test_db, tags=["proj:x"], entity_type="conversation", entity_id=conv)
    # tag lives only on the conversation → no element hits enumerated
    sv = search_view("", db_path=test_db, tag=["proj:x"])
    assert sv.results == []


def test_facet_only_conversations_view_aggregates(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="response", entity_id=rid)
    sv = enumerate_tagged(db_path=test_db, tag=["docs:thing"], view="conversations")
    assert len(sv.results) == 1
    assert sv.results[0]["conversation_id"] == conv


def test_all_tags_and_semantics(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["a", "b"], entity_type="response", entity_id=rid)
    apply_tags(db_path=test_db, tags=["a"], entity_type="prompt", entity_id=pid)

    both = search_view("", db_path=test_db, all_tags=["a", "b"])
    assert [h["event_id"] for h in both.results] == [rid]  # only the response has both


def test_enrich_tags_batches(test_db):
    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["z:1"], entity_type="response", entity_id=rid)
    chunks = [SearchChunk(conversation_id=conv, score=1.0, text="x", chunk_type="response", event_id=rid)]
    conn = open_database(test_db, read_only=True)
    try:
        enrich_tags(conn, chunks)
    finally:
        conn.close()
    assert chunks[0].tags == ["z:1"]


def test_no_query_no_facet_still_errors(test_db):
    from siftd.cli import main

    rc = main(["--db", str(test_db), "search"])
    assert rc == 1


def test_terminal_renderer_shows_tag_chip(test_db):
    import io

    from painted import Fidelity, print_block

    from siftd.output.painted_bridge import render_search_block

    conv, pid, rid = _ids(test_db)
    apply_tags(db_path=test_db, tags=["docs:thing"], entity_type="response", entity_id=rid)
    sv = search_view("", db_path=test_db, tag=["docs:thing"])
    block = render_search_block(sv.results, Fidelity(), query="", mode="chunks")
    buf = io.StringIO()
    print_block(block, buf, use_ansi=False)
    assert "docs:thing" in buf.getvalue()
