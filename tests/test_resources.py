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
