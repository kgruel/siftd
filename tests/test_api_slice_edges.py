import pytest

from siftd.api import slice as api_slice


def test_slice_database_raises_when_source_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="Database not found"):
        api_slice.slice_database(tmp_path / "missing.db", tmp_path / "out.db")


def test_populate_slice_raises_on_foreign_key_violations():
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
            if "name='prompt_tags'" in sql:
                return _Result(None)
            if "PRAGMA slice.foreign_key_check" in sql:
                return _Result(all_=[("responses", 1, "conversations", 0)])
            return _Result()

    with pytest.raises(RuntimeError, match="Foreign key violations in sliced database"):
        api_slice._populate_slice(_Conn(), ["c1"])