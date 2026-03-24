"""Tests for siftd.api.tools — tool usage statistics API."""

import pytest

from siftd.api.tools import TagUsage, WorkspaceTagUsage, get_tool_tag_summary, get_tool_tags_by_workspace


class TestTagUsage:
    def test_fields(self):
        t = TagUsage(name="shell:test", count=5)
        assert t.name == "shell:test"
        assert t.count == 5


class TestWorkspaceTagUsage:
    def test_fields(self):
        tags = [TagUsage(name="shell:run", count=3)]
        w = WorkspaceTagUsage(workspace="/proj", tags=tags, total=3)
        assert w.workspace == "/proj"
        assert len(w.tags) == 1
        assert w.total == 3


class TestGetToolTagSummary:
    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Database not found"):
            get_tool_tag_summary(db_path=tmp_path / "missing.db")

    def test_returns_tag_usage_list(self, monkeypatch, tmp_path):
        db = tmp_path / "test.db"
        db.write_text("x")

        class _Conn:
            def close(self):
                pass

        monkeypatch.setattr(
            "siftd.api.tools.open_database",
            lambda _p, read_only=False: _Conn(),
        )
        monkeypatch.setattr(
            "siftd.api.tools.fetch_tool_tags_by_prefix",
            lambda _c, _p: [{"name": "shell:test", "count": 10}, {"name": "shell:lint", "count": 3}],
        )

        result = get_tool_tag_summary(db_path=db, prefix="shell:")
        assert len(result) == 2
        assert all(isinstance(r, TagUsage) for r in result)
        assert result[0].name == "shell:test"
        assert result[1].count == 3

    def test_default_prefix(self, monkeypatch, tmp_path):
        """Default prefix is 'shell:'."""
        db = tmp_path / "test.db"
        db.write_text("x")

        seen_prefix = {}

        class _Conn:
            def close(self):
                pass

        monkeypatch.setattr("siftd.api.tools.open_database", lambda _p, read_only=False: _Conn())
        monkeypatch.setattr(
            "siftd.api.tools.fetch_tool_tags_by_prefix",
            lambda _c, p: (seen_prefix.update({"prefix": p}), [])[1],
        )

        get_tool_tag_summary(db_path=db)
        assert seen_prefix["prefix"] == "shell:"


class TestGetToolTagsByWorkspace:
    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Database not found"):
            get_tool_tags_by_workspace(db_path=tmp_path / "missing.db")

    def test_aggregation_and_sorting(self, monkeypatch, tmp_path):
        db = tmp_path / "test.db"
        db.write_text("x")

        class _Conn:
            def close(self):
                pass

        monkeypatch.setattr("siftd.api.tools.open_database", lambda _p, read_only=False: _Conn())
        monkeypatch.setattr(
            "siftd.api.tools.fetch_tool_tags_by_workspace",
            lambda _c, _p: [
                {"workspace": "/proj-a", "tag": "shell:run", "count": 10},
                {"workspace": "/proj-a", "tag": "shell:test", "count": 5},
                {"workspace": "/proj-b", "tag": "shell:run", "count": 2},
            ],
        )

        result = get_tool_tags_by_workspace(db_path=db)
        assert len(result) == 2
        assert all(isinstance(r, WorkspaceTagUsage) for r in result)
        # Sorted by total descending: proj-a (15) before proj-b (2)
        assert result[0].workspace == "/proj-a"
        assert result[0].total == 15
        assert len(result[0].tags) == 2
        assert result[1].workspace == "/proj-b"
        assert result[1].total == 2

    def test_n_limits_results(self, monkeypatch, tmp_path):
        db = tmp_path / "test.db"
        db.write_text("x")

        class _Conn:
            def close(self):
                pass

        monkeypatch.setattr("siftd.api.tools.open_database", lambda _p, read_only=False: _Conn())
        monkeypatch.setattr(
            "siftd.api.tools.fetch_tool_tags_by_workspace",
            lambda _c, _p: [
                {"workspace": f"/proj-{i}", "tag": "shell:run", "count": 10 - i}
                for i in range(5)
            ],
        )

        result = get_tool_tags_by_workspace(db_path=db, n=2)
        assert len(result) == 2
