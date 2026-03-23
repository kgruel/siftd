import sqlite3
from pathlib import Path

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
    def __init__(self, rows):
        self.rows = list(rows)
        self.closed = False

    def execute(self, *_a, **_k):
        row = self.rows.pop(0)

        class _R:
            def __init__(self, data):
                self.data = data

            def fetchone(self):
                return self.data

            def fetchall(self):
                return self.data

        return _R(row)

    def close(self):
        self.closed = True


def test_get_cost_coverage_open_close_and_no_stats(monkeypatch, tmp_path):
    c = _SeqConn([[0]])
    monkeypatch.setattr("siftd.storage.sqlite.open_database", lambda *a, **k: c)
    assert get_cost_coverage(db_path=tmp_path / "db.sqlite") is None
    assert c.closed


def test_list_workspaces_open_close(monkeypatch, tmp_path):
    c = _SeqConn([])
    monkeypatch.setattr("siftd.api.stats.open_database", lambda *a, **k: c)
    monkeypatch.setattr("siftd.api.stats.fetch_top_workspaces", lambda conn, limit=10: [{"path": "p", "convs": 1}])
    assert list_workspaces(n=1, db_path=tmp_path / "db.sqlite")
    assert c.closed


def test_write_stats_cache_cleanup_branch(monkeypatch, tmp_path):
    db = tmp_path / "s.db"
    db.write_text("x")
    stats = _make_stats(db)
    monkeypatch.setattr("siftd.api.stats.cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr("siftd.api.stats.os.replace", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("siftd.api.stats.os.unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    try:
        write_stats_cache(stats)
        assert False
    except RuntimeError:
        assert True


def test_usage_functions_with_stats_table(tmp_path):
    db = tmp_path / "u.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, workspace_id TEXT)")
    conn.execute("CREATE TABLE responses (conversation_id TEXT, model_id TEXT, input_tokens INTEGER, output_tokens INTEGER)")
    conn.execute("CREATE TABLE models (id TEXT PRIMARY KEY, raw_name TEXT)")
    conn.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT)")
    conn.execute("CREATE TABLE conversation_stats (conversation_id TEXT, cost REAL, total_tokens INTEGER)")
    conn.execute("INSERT INTO models VALUES ('m1','model-a')")
    conn.execute("INSERT INTO workspaces VALUES ('w1','/tmp/ws')")
    conn.execute("INSERT INTO conversations VALUES ('c1','w1')")
    conn.execute("INSERT INTO responses VALUES ('c1','m1',10,20)")
    conn.execute("INSERT INTO conversation_stats VALUES ('c1',1.5,30)")
    conn.commit()
    conn.close()

    s = get_usage_summary(db_path=Path(db))
    by_model = get_usage_by_model(db_path=Path(db))
    by_ws = get_usage_by_workspace(db_path=Path(db))

    assert s.total_conversations == 1 and s.total_cost == 1.5
    assert by_model and by_model[0].name == "model-a"
    assert by_ws and by_ws[0].name == "/tmp/ws"
