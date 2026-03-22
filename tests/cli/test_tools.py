"""Tests for siftd tools command (cmd_tools)."""

import json

import pytest

from siftd.cli import main


class TestCmdTools:
    def test_tools_summary(self, test_db_with_tool_tags, capsys):
        """siftd tools shows tag summary."""
        rc = main(["--db", str(test_db_with_tool_tags), "tools"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "shell:" in out or "total" in out.lower()

    def test_tools_json(self, test_db_with_tool_tags, capsys):
        """siftd tools --json returns JSON array."""
        rc = main(["--db", str(test_db_with_tool_tags), "tools", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        for item in data:
            assert "name" in item
            assert "count" in item

    def test_tools_by_workspace(self, test_db_with_tool_tags, capsys):
        """siftd tools --by-workspace shows per-workspace breakdown."""
        rc = main(["--db", str(test_db_with_tool_tags), "tools", "--by-workspace"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "total" in out.lower()

    def test_tools_by_workspace_json(self, test_db_with_tool_tags, capsys):
        """siftd tools --by-workspace --json returns structured data."""
        rc = main([
            "--db", str(test_db_with_tool_tags),
            "tools", "--by-workspace", "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        for item in data:
            assert "workspace" in item
            assert "total" in item
            assert "tags" in item

    def test_tools_custom_prefix(self, test_db_with_tool_tags, capsys):
        """siftd tools --prefix filters by tag prefix."""
        rc = main(["--db", str(test_db_with_tool_tags), "tools", "--prefix", "shell:"])
        assert rc == 0

    def test_tools_nonexistent_prefix(self, test_db_with_tool_tags, capsys):
        """siftd tools with unmatched prefix shows empty message."""
        rc = main([
            "--db", str(test_db_with_tool_tags),
            "tools", "--prefix", "nonexistent:",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No tool calls" in out

    def test_tools_missing_db(self, tmp_path, capsys):
        """siftd tools with missing database returns error."""
        rc = main(["--db", str(tmp_path / "missing.db"), "tools"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower() or "Database" in out

    def test_tools_missing_db_json(self, tmp_path, capsys):
        """siftd tools --json with missing db returns empty array."""
        rc = main(["--db", str(tmp_path / "missing.db"), "tools", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data == []

    def test_tools_limit(self, test_db_with_tool_tags, capsys):
        """siftd tools --by-workspace -n limits workspace count."""
        rc = main([
            "--db", str(test_db_with_tool_tags),
            "tools", "--by-workspace", "-n", "1",
        ])
        assert rc == 0
