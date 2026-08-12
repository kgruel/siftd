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
    monkeypatch.setattr("siftd.api.merge._missing_merge_runtime_schema", lambda *_a, **_k: [])
    monkeypatch.setattr("siftd.api.merge._merge_attached", lambda conn, replace=True, user_id=None: {"ok": 1})

    with pytest.raises(RuntimeError, match="Foreign key violations after merge"):
        api_merge.merge_database(target, source, dry_run=False, preflight=False)

    assert calls["rollback"] and calls["closed"]


def test_dry_run_reports_foreign_key_violations(monkeypatch, tmp_path):
    """A dry run predicts the FK failure instead of reporting success (#51).

    The check used to be gated on `not dry_run`, which made the one prediction
    worth having the one a dry run could not make. It was defensible while
    merge ran with foreign keys off — both paths were equally blind — and stops
    being so the moment the real path can fail on it.

    The unwind is the savepoint's, not a rollback: a dry run has one open and
    `conn.rollback()` would discard a transaction it does not own.
    """
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"
    target.write_text("x")
    source.write_text("y")

    statements: list[str] = []

    class _Conn:
        def execute(self, sql, params=None):
            statements.append(sql)
            if "main.user_version" in sql or "src.user_version" in sql:
                return type("R", (), {"fetchone": lambda self: [1], "fetchall": lambda self: []})()
            if "foreign_key_check" in sql:
                return type("R", (), {
                    "fetchone": lambda self: None,
                    "fetchall": lambda self: [("events", 1, "conversations", 0)],
                })()
            return type("R", (), {"fetchone": lambda self: None, "fetchall": lambda self: []})()

        def rollback(self): raise AssertionError("a dry run must unwind its savepoint")
        def commit(self): raise AssertionError("a dry run must not commit")
        def close(self): pass

    monkeypatch.setattr("siftd.api.merge.open_database", lambda *_a, **_k: _Conn())
    monkeypatch.setattr("siftd.api.merge._missing_merge_runtime_schema", lambda *_a, **_k: [])
    monkeypatch.setattr("siftd.api.merge._merge_attached", lambda conn, replace=True, user_id=None: {"ok": 1})

    with pytest.raises(RuntimeError, match="Foreign key violations after merge"):
        api_merge.merge_database(target, source, dry_run=True, preflight=False)

    assert "ROLLBACK TO merge_dry_run" in statements
    assert "RELEASE merge_dry_run" in statements
