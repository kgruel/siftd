import sqlite3

import pytest
from test_stats_cache import _make_stats

from siftd.api.stats import (
    get_cost_coverage,
    get_usage_by_model,
    get_usage_by_workspace,
    get_usage_summary,
    list_workspaces,
    write_stats_cache,
)


class _SeqConn:
    def __init__(self, rows): self.rows, self.closed = list(rows), False
    def execute(self, *_a, **_k):
        row = self.rows.pop(0)
        return type("R", (), {"fetchone": lambda self: row, "fetchall": lambda self: row})()
    def close(self): self.closed = True


def test_open_close_and_cache_cleanup_paths(monkeypatch, tmp_path):
    c = _SeqConn([[0]])
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: c)
    assert get_cost_coverage(db_path=tmp_path / "db.sqlite") is None and c.closed

    c2 = _SeqConn([])
    monkeypatch.setattr("siftd.api.stats.open_database", lambda *a, **k: c2)
    monkeypatch.setattr("siftd.api.stats.fetch_top_workspaces", lambda conn, limit=10: [{"path": "p", "convs": 1}])
    assert list_workspaces(n=1, db_path=tmp_path / "db.sqlite") and c2.closed

    db = tmp_path / "s.db"
    db.write_text("x")
    monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr("siftd.api.stats.os.replace", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("siftd.api.stats.os.unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(RuntimeError):
        write_stats_cache(_make_stats(db))


def test_usage_functions_with_stats_table(tmp_path):
    db = tmp_path / "u.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, workspace_id TEXT);"
        "CREATE TABLE models (id TEXT PRIMARY KEY, raw_name TEXT, name TEXT);"
        "CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT);"
        "CREATE TABLE harnesses (id TEXT PRIMARY KEY, name TEXT);"
        "CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT);"
        "CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT, conversation_id TEXT, parent_id TEXT, external_id TEXT, timestamp TEXT);"
        "CREATE TABLE event_response (event_id TEXT PRIMARY KEY, model_id TEXT, provider_id TEXT, input_tokens INTEGER, output_tokens INTEGER);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY, conversation_id TEXT, model_id TEXT, input_tokens INTEGER, output_tokens INTEGER);"
        "CREATE TABLE conversation_stats (conversation_id TEXT, cost REAL, total_tokens INTEGER);"
        "CREATE TABLE usage_by_conv_model (conversation_id TEXT, model_id TEXT,"
        " provider_id TEXT, input_tokens INTEGER, output_tokens INTEGER,"
        " response_count INTEGER, responses_with_tokens INTEGER, cost REAL);"
        "INSERT INTO models VALUES ('m1','model-a','model-a');"
        "INSERT INTO workspaces VALUES ('w1','/tmp/ws');"
        "INSERT INTO conversations VALUES ('c1','w1');"
        "INSERT INTO events VALUES ('e1','response','c1',NULL,NULL,'2024-01-01T00:00:00Z');"
        "INSERT INTO event_response VALUES ('e1','m1',NULL,10,20);"
        "INSERT INTO responses VALUES ('r1','c1','m1',10,20);"
        "INSERT INTO conversation_stats VALUES ('c1',1.5,30);"
        "INSERT INTO usage_by_conv_model VALUES ('c1','m1',NULL,10,20,1,1,1.5);"
    )
    conn.close()

    s = get_usage_summary(db_path=db)
    assert s.total_conversations == 1 and s.total_cost == 1.5 and get_usage_by_model(db_path=db)[0].name == "model-a" and get_usage_by_workspace(db_path=db)[0].name == "/tmp/ws"
