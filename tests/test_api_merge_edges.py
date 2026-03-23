
import pytest

from siftd.api import merge as api_merge


def test_merge_database_foreign_key_violation_branch(monkeypatch, tmp_path):
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"
    target.write_text("x")
    source.write_text("y")

    calls = {"rollback": False, "closed": False}

    class _Result:
        def __init__(self, one=None, all_=None):
            self._one = one
            self._all = all_ if all_ is not None else []

        def fetchone(self):
            return self._one

        def fetchall(self):
            return self._all

    class _Conn:
        def execute(self, sql, params=None):
            if "PRAGMA main.user_version" in sql:
                return _Result([1])
            if "PRAGMA src.user_version" in sql:
                return _Result([1])
            if "PRAGMA foreign_key_check" in sql:
                return _Result(all_=[("responses", 1, "conversations", 0)])
            return _Result()

        def rollback(self):
            calls["rollback"] = True

        def commit(self):
            pass

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr("siftd.api.merge.open_database", lambda *_a, **_k: _Conn())
    monkeypatch.setattr("siftd.api.merge._merge_attached", lambda conn, replace=True: {"ok": 1})

    with pytest.raises(RuntimeError, match="Foreign key violations after merge"):
        api_merge.merge_database(target, source, dry_run=False)

    assert calls["rollback"] and calls["closed"]
