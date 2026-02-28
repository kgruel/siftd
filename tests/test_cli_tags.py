"""Tests for siftd tag and tags CLI commands."""

import pytest

from siftd.cli import main
from siftd.cli_tags import _parse_tag_args
from siftd.storage.sqlite import open_database


# ---------------------------------------------------------------------------
# _parse_tag_args
# ---------------------------------------------------------------------------


class TestParseTagArgs:
    def test_conversation_default(self):
        result = _parse_tag_args(["abc123", "important"])
        assert result == ("conversation", "abc123", ["important"])

    def test_multiple_tags(self):
        result = _parse_tag_args(["abc123", "tag1", "tag2", "tag3"])
        assert result == ("conversation", "abc123", ["tag1", "tag2", "tag3"])

    def test_explicit_entity_type(self):
        result = _parse_tag_args(["workspace", "abc123", "proj"])
        assert result == ("workspace", "abc123", ["proj"])

    def test_tool_call_entity(self):
        result = _parse_tag_args(["tool_call", "tc123", "slow"])
        assert result == ("tool_call", "tc123", ["slow"])

    def test_entity_type_without_tag_returns_none(self):
        assert _parse_tag_args(["workspace", "abc123"]) is None

    def test_single_arg_returns_none(self):
        assert _parse_tag_args(["abc123"]) is None

    def test_empty_returns_none(self):
        assert _parse_tag_args([]) is None


# ---------------------------------------------------------------------------
# cmd_tag
# ---------------------------------------------------------------------------


class TestCmdTag:
    def test_tag_missing_db(self, tmp_path, capsys):
        rc = main(["--db", str(tmp_path / "missing.db"), "tag", "abc", "foo"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower() or "Database" in out

    def test_tag_no_args_shows_usage(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "Usage:" in out

    def test_tag_apply(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()

        rc = main(["--db", str(test_db), "tag", conv_id, "test-tag"])
        assert rc == 0
        assert "Applied tag" in capsys.readouterr().out

    def test_tag_remove(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()

        main(["--db", str(test_db), "tag", conv_id, "removeme"])
        capsys.readouterr()
        rc = main(["--db", str(test_db), "tag", "--remove", conv_id, "removeme"])
        assert rc == 0
        assert "Removed tag" in capsys.readouterr().out

    def test_tag_nonexistent_id(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "nonexistent_id_xyz", "foo"])
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_tag_last_applies_to_recent(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "--last", "--", "recent-tag"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Applied tag" in out

    def test_tag_last_n(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "--last", "2", "batch-tag"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "2 conversation" in out

    def test_tag_last_remove(self, test_db, capsys):
        main(["--db", str(test_db), "tag", "--last", "--", "rm-tag"])
        capsys.readouterr()
        rc = main(["--db", str(test_db), "tag", "--remove", "--last", "--", "rm-tag"])
        assert rc == 0
        assert "Removed tag" in capsys.readouterr().out

    def test_tag_last_remove_nonexistent(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "--remove", "--last", "--", "no-such-tag"])
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_tag_last_no_tag_name(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "--last"])
        assert rc == 1
        assert "Usage:" in capsys.readouterr().out

    def test_tag_already_applied(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()

        main(["--db", str(test_db), "tag", conv_id, "dupe"])
        capsys.readouterr()
        rc = main(["--db", str(test_db), "tag", conv_id, "dupe"])
        assert rc == 0
        assert "already applied" in capsys.readouterr().out

    def test_tag_exchange_without_session_warns(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()

        main(["--db", str(test_db), "tag", "--exchange", "0", conv_id, "foo"])
        err = capsys.readouterr().err
        assert "--exchange ignored" in err


# ---------------------------------------------------------------------------
# cmd_tags
# ---------------------------------------------------------------------------


class TestCmdTags:
    def test_tags_list_empty(self, test_db, capsys):
        """siftd tags on a db with no tags."""
        rc = main(["--db", str(test_db), "tags"])
        assert rc == 0

    def test_tags_list_with_tags(self, test_db, capsys):
        # Apply a tag first
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "listed-tag"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags"])
        assert rc == 0
        assert "listed-tag" in capsys.readouterr().out

    def test_tags_prefix_filter(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "prefix:a"])
        main(["--db", str(test_db), "tag", conv_id, "other:b"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--prefix", "prefix:"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "prefix:a" in out
        assert "other:b" not in out

    def test_tags_prefix_no_match(self, test_db, capsys):
        # Must have at least one tag so list_tags returns non-empty
        # (otherwise "No tags defined." is printed before prefix filter)
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "exists"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--prefix", "zzz:"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No tags found" in out

    def test_tags_missing_db(self, tmp_path, capsys):
        rc = main(["--db", str(tmp_path / "missing.db"), "tags"])
        assert rc == 1

    def test_tags_rename(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "old-name"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--rename", "old-name", "new-name"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Renamed" in out

    def test_tags_rename_nonexistent(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tags", "--rename", "no-such", "new"])
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_tags_delete_unassociated(self, test_db, capsys):
        # Create then remove tag from conversation (leaves tag in tags table)
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "temp-tag"])
        main(["--db", str(test_db), "tag", "--remove", conv_id, "temp-tag"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--delete", "temp-tag"])
        assert rc == 0
        assert "Deleted" in capsys.readouterr().out

    def test_tags_delete_with_associations_blocked(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "in-use"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--delete", "in-use"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "--force" in out

    def test_tags_delete_force(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "force-del"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--delete", "force-del", "--force"])
        assert rc == 0
        assert "Deleted" in capsys.readouterr().out

    def test_tags_delete_nonexistent(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tags", "--delete", "no-such-tag"])
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_tags_drill_down(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "drilldown"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "drilldown"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "drilldown" in out

    def test_tags_drill_down_no_match(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tags", "nonexistent-tag"])
        assert rc == 0
        assert "No conversations found" in capsys.readouterr().out
