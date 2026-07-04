"""Real-backend integration for the post-ingest auto-index hook (embed lane — fastembed).

Verifies the actual embed_status -> build_index wiring: a built index plus a newly-added
(stale) conversation gets embedded inline, while an unbuilt index defers to `siftd embed`.
"""

import pytest

pytestmark = pytest.mark.embeddings

pytest.importorskip("fastembed")

from siftd.api import ingest as ingest_api
from siftd.api.search import build_index
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_content,
)


def _add_conversation(db, ext: str, day: int) -> None:
    conn = create_database(db)
    h = get_or_create_harness(conn, "t", source="t", log_format="jsonl")
    m = get_or_create_model(conn, "test-model")
    w = get_or_create_workspace(conn, "/proj", "2024-01-01T00:00:00Z")
    cid = insert_conversation(conn, external_id=ext, harness_id=h, workspace_id=w, started_at=f"2024-01-{day:02d}T00:00:00Z")
    pid = insert_prompt(conn, cid, f"{ext}-p0", f"2024-01-{day:02d}T00:00:01Z")
    insert_prompt_content(conn, pid, 0, "text", '{"text": "How do I handle Python errors gracefully?"}')
    rid = insert_response(conn, cid, pid, m, None, f"{ext}-r0", f"2024-01-{day:02d}T00:00:02Z", input_tokens=5, output_tokens=10)
    insert_response_content(conn, rid, 0, "text", '{"text": "Use try/except blocks."}')
    conn.commit()
    conn.close()


def test_auto_index_embeds_new_conversation_end_to_end(tmp_path):
    db = tmp_path / "main.db"
    edb = tmp_path / "embed.db"

    _add_conversation(db, "c1", 1)
    build_index(db_path=db, embed_db_path=edb)  # index is now built + up to date

    _add_conversation(db, "c2", 2)  # a new, unindexed (stale) conversation

    rep = ingest_api._maybe_auto_index(db, edb)
    assert rep is not None
    assert rep.ran is True
    assert rep.conversations_indexed == 1
    assert rep.chunks_added >= 1
    assert rep.skipped_reason is None and rep.error is None
    assert rep.notice is None  # fastembed is local — no egress notice


def test_auto_index_defers_when_unbuilt(tmp_path):
    db = tmp_path / "main.db"
    edb = tmp_path / "never-built.db"
    _add_conversation(db, "c1", 1)

    rep = ingest_api._maybe_auto_index(db, edb)
    assert rep is not None
    assert rep.skipped_reason == "unbuilt"
    assert rep.awaiting == 1
    assert rep.ran is False
