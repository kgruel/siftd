import pytest

from siftd.api import merge as api_merge


def test_merge_database_foreign_key_violation_branch(monkeypatch, tmp_path):
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"
    target.write_text("x")
    source.write_text("y")

    calls = {"rollback": False, "closed": False}

    class _Conn:
        def execute(self, sql, params=None):
            if "main.user_version" in sql or "src.user_version" in sql:
                return type("R", (), {"fetchone": lambda self: [1], "fetchall": lambda self: []})()
            if "foreign_key_check" in sql:
                return type("R", (), {"fetchone": lambda self: None, "fetchall": lambda self: [("responses", 1, "conversations", 0)]})()
            return type("R", (), {"fetchone": lambda self: None, "fetchall": lambda self: []})()

        def rollback(self): calls["rollback"] = True
        def commit(self): pass
        def close(self): calls["closed"] = True

    monkeypatch.setattr("siftd.api.merge.open_database", lambda *_a, **_k: _Conn())
    monkeypatch.setattr("siftd.api.merge._merge_attached", lambda conn, replace=True: {"ok": 1})

    with pytest.raises(RuntimeError, match="Foreign key violations after merge"):
        api_merge.merge_database(target, source, dry_run=False)

    assert calls["rollback"] and calls["closed"]
