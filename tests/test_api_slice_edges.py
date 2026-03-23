import pytest

from siftd.api import slice as api_slice


def _res(one=None, all_=None):
    return type("R", (), {"fetchone": lambda self: one, "fetchall": lambda self: (all_ if all_ is not None else [])})()


def test_slice_database_raises_when_source_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="Database not found"):
        api_slice.slice_database(tmp_path / "missing.db", tmp_path / "out.db")


def test_populate_slice_raises_on_foreign_key_violations():
    class _Conn:
        def execute(self, sql, params=None):
            if "name='prompt_tags'" in sql:
                return _res(None)
            if "PRAGMA slice.foreign_key_check" in sql:
                return _res(all_=[("responses", 1, "conversations", 0)])
            return _res()

    with pytest.raises(RuntimeError, match="Foreign key violations in sliced database"):
        api_slice._populate_slice(_Conn(), ["c1"])
