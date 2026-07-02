import sqlite3

from siftd.api import conversations as conv
from painted import Fidelity


def test_get_conversation_includes_prompt_with_no_response(tmp_path, monkeypatch):
    db = tmp_path / "d.db"
    sqlite3.connect(db).close()

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr("siftd.api.conversations.open_database", lambda *a, **k: _Conn())
    monkeypatch.setattr("siftd.api.conversations.resolve_entity_id", lambda conn, kind, _id, **kw: "c1")
    monkeypatch.setattr("siftd.api.conversations.fetch_conversation_by_id_or_prefix", lambda conn, _id: {"id": "c1", "workspace": "/w", "started_at": "2024-01-01"})
    monkeypatch.setattr("siftd.api.conversations.fetch_conversation_model", lambda conn, cid: "m")
    monkeypatch.setattr("siftd.api.conversations.fetch_conversation_token_totals", lambda conn, cid: (0, 0))
    monkeypatch.setattr("siftd.api.conversations.fetch_prompts_for_conversation", lambda conn, cid: [{"id": "p1", "timestamp": "2024-01-01"}])
    monkeypatch.setattr("siftd.api.conversations.fetch_prompt_text_contents", lambda conn, pids: {})
    monkeypatch.setattr("siftd.api.conversations.fetch_responses_for_conversation", lambda conn, cid: [])
    monkeypatch.setattr("siftd.api.conversations.fetch_response_content_blocks", lambda conn, ids, block_types=None: {})
    monkeypatch.setattr("siftd.api.conversations.fetch_tool_calls_for_conversation", lambda conn, cid, include_content=False: [])
    monkeypatch.setattr("siftd.api.conversations.fetch_conversation_tags", lambda conn, cid: [])
    monkeypatch.setattr("siftd.api.conversations._fetch_conversation_event_tags", lambda conn, cid: {})

    detail = conv.get_conversation("c1", fidelity=Fidelity(), db_path=db)
    assert detail is not None and len(detail.turns) == 1 and detail.turns[0].total_input_tokens == 0


def test_run_query_file_rejects_write_operations(tmp_path, monkeypatch):
    from siftd.api import create_database
    from siftd.api.conversations import QueryError

    db = tmp_path / "db.sqlite"
    create_database(db).close()

    qdir = tmp_path / "queries"
    qdir.mkdir()
    (qdir / "q.sql").write_text("PRAGMA user_version = 1;")
    monkeypatch.setattr("siftd.paths.queries_dir", lambda: qdir)

    import pytest

    with pytest.raises(QueryError, match="SQL error"):
        conv.run_query_file("q", db_path=db)
