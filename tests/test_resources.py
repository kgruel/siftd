"""Tests for siftd.api.resources — adapter/query/formatter copy operations."""

import pytest

from siftd.api.resources import (
    CopyError,
    copy_adapter,
    copy_formatter,
    copy_query,
    list_builtin_formatters,
    list_builtin_queries,
)


class TestCopyAdapter:
    def test_copies_known(self, tmp_path):
        dest = copy_adapter("claude_code", dest_dir=tmp_path)
        assert dest.exists() and dest.name == "claude_code.py" and dest.read_text().strip()

    def test_unknown_raises(self, tmp_path):
        with pytest.raises(CopyError, match="not found"):
            copy_adapter("nonexistent_xyz", dest_dir=tmp_path)

    def test_exists_no_force(self, tmp_path):
        copy_adapter("claude_code", dest_dir=tmp_path)
        with pytest.raises(CopyError, match="exists"):
            copy_adapter("claude_code", dest_dir=tmp_path)

    def test_force_overwrites(self, tmp_path):
        copy_adapter("claude_code", dest_dir=tmp_path)
        assert copy_adapter("claude_code", dest_dir=tmp_path, force=True).exists()


class TestCopyQuery:
    def test_copies_known(self, tmp_path):
        queries = list_builtin_queries()
        if not queries:
            pytest.skip("No built-in queries")
        dest = copy_query(queries[0], dest_dir=tmp_path)
        assert dest.exists() and dest.suffix == ".sql"

    def test_unknown_raises(self, tmp_path):
        with pytest.raises(CopyError, match="not found"):
            copy_query("nonexistent_xyz", dest_dir=tmp_path)

    def test_exists_no_force(self, tmp_path):
        queries = list_builtin_queries()
        if not queries:
            pytest.skip("No built-in queries")
        copy_query(queries[0], dest_dir=tmp_path)
        with pytest.raises(CopyError, match="exists"):
            copy_query(queries[0], dest_dir=tmp_path)

    def test_force_overwrites(self, tmp_path):
        queries = list_builtin_queries()
        if not queries:
            pytest.skip("No built-in queries")
        copy_query(queries[0], dest_dir=tmp_path)
        assert copy_query(queries[0], dest_dir=tmp_path, force=True).exists()


class TestCopyFormatter:
    def test_copies_terminal(self, tmp_path):
        assert copy_formatter("terminal", dest_dir=tmp_path).exists()

    def test_unknown_raises(self, tmp_path):
        with pytest.raises(CopyError, match="not found"):
            copy_formatter("nonexistent_xyz", dest_dir=tmp_path)

    def test_exists_no_force(self, tmp_path):
        copy_formatter("terminal", dest_dir=tmp_path)
        with pytest.raises(CopyError, match="exists"):
            copy_formatter("terminal", dest_dir=tmp_path)

    def test_force_overwrites(self, tmp_path):
        copy_formatter("terminal", dest_dir=tmp_path)
        assert copy_formatter("terminal", dest_dir=tmp_path, force=True).exists()


class TestListBuiltins:
    def test_queries(self):
        assert isinstance(list_builtin_queries(), list)

    def test_formatters(self):
        assert list_builtin_formatters() == ["terminal", "markdown", "json"]


class TestResourceEdgeBranches:
    def test_copy_adapter_package_lookup_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "importlib.resources.files",
            lambda pkg: (_ for _ in ()).throw(TypeError("bad package")),
        )
        with pytest.raises(CopyError, match="Cannot locate adapter package"):
            copy_adapter("claude_code", dest_dir=tmp_path)

    def test_copy_query_default_dest_and_package_errors(self, tmp_path, monkeypatch):
        queries = list_builtin_queries()
        if queries:
            monkeypatch.setattr("siftd.api.resources.queries_dir", lambda: tmp_path)
            assert copy_query(queries[0]).exists()

        monkeypatch.setattr(
            "importlib.resources.files",
            lambda pkg: (_ for _ in ()).throw(ModuleNotFoundError("missing")),
        )
        with pytest.raises(CopyError, match="Cannot locate built-in queries package"):
            copy_query("anything", dest_dir=tmp_path)

    def test_copy_query_no_builtin_queries_available_message(self, tmp_path, monkeypatch):
        class _Ref:
            def joinpath(self, _name):
                return self

            def is_file(self):
                return False

        monkeypatch.setattr("importlib.resources.files", lambda pkg: _Ref())
        monkeypatch.setattr("siftd.api.resources.list_builtin_queries", lambda: [])
        with pytest.raises(CopyError, match="No built-in queries available"):
            copy_query("missing", dest_dir=tmp_path)

    def test_list_builtin_queries_lookup_error_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "importlib.resources.files",
            lambda pkg: (_ for _ in ()).throw(TypeError("bad package")),
        )
        assert list_builtin_queries() == []

    def test_copy_formatter_default_dest_and_package_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("siftd.api.resources.formatters_dir", lambda: tmp_path)
        assert copy_formatter("terminal").exists()

        monkeypatch.setattr(
            "importlib.resources.files",
            lambda pkg: (_ for _ in ()).throw(ModuleNotFoundError("missing")),
        )
        with pytest.raises(CopyError, match="Cannot locate formatter package"):
            copy_formatter("terminal", dest_dir=tmp_path)
