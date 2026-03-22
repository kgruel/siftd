"""Tests for siftd tag CLI command (apply, remove, list, rename, delete)."""

import pytest

from siftd.cli import main
from siftd.cli.tags import _parse_tag_args
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
# cmd_tag — apply / remove
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

    def test_tag_last_without_separator(self, test_db, capsys):
        """--last directly followed by tag name (no -- separator, no N)."""
        rc = main(["--db", str(test_db), "tag", "--last", "direct-tag"])
        assert rc == 0
        assert "Applied tag 'direct-tag'" in capsys.readouterr().out

    def test_tag_last_multiple_tags(self, test_db, capsys):
        """--last with multiple tags applied at once."""
        rc = main(["--db", str(test_db), "tag", "--last", "multi-a", "multi-b"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Applied tag 'multi-a'" in out
        assert "Applied tag 'multi-b'" in out

    def test_tag_last_n_multiple_tags(self, test_db, capsys):
        """--last N with multiple tags."""
        rc = main(["--db", str(test_db), "tag", "--last", "2", "batch-a", "batch-b"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Applied tag 'batch-a'" in out
        assert "Applied tag 'batch-b'" in out
        assert "2 conversation" in out

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

    def test_tag_current_with_session(self, test_db, capsys, tmp_path, monkeypatch):
        """--current queues tags when a session is registered."""
        from siftd.paths import session_id_file

        monkeypatch.chdir(tmp_path)
        workspace = str(tmp_path.resolve())
        sid_file = session_id_file(workspace)
        sid_file.parent.mkdir(parents=True, exist_ok=True)
        sid_file.write_text("fake-session-id\n")

        rc = main(["--db", str(test_db), "tag", "--current", "current-tag"])
        assert rc == 0
        assert "Queued tag 'current-tag'" in capsys.readouterr().out

    def test_tag_current_falls_back_to_last(self, test_db, capsys, tmp_path, monkeypatch):
        """--current falls back to --last when no session is registered."""
        monkeypatch.chdir(tmp_path)

        rc = main(["--db", str(test_db), "tag", "--current", "fallback-tag"])
        assert rc == 0
        assert "Applied tag 'fallback-tag'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# tag list / rename / delete (unified subcommands)
# ---------------------------------------------------------------------------


class TestCmdTagList:
    def test_list_empty(self, test_db, capsys):
        """siftd tag list on a db with no tags."""
        rc = main(["--db", str(test_db), "tag", "list"])
        assert rc == 0

    def test_list_with_tags(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "listed-tag"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "list"])
        assert rc == 0
        assert "listed-tag" in capsys.readouterr().out

    def test_list_prefix_filter(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "prefix:a"])
        main(["--db", str(test_db), "tag", conv_id, "other:b"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "list", "--prefix", "prefix:"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "prefix:a" in out
        assert "other:b" not in out

    def test_list_prefix_no_match(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "exists"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "list", "--prefix", "zzz:"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No tags found" in out

    def test_list_missing_db(self, tmp_path, capsys):
        rc = main(["--db", str(tmp_path / "missing.db"), "tag", "list"])
        assert rc == 1

    def test_drill_down(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "drilldown"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "list", "drilldown"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "drilldown" in out

    def test_drill_down_no_match(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "list", "nonexistent-tag"])
        assert rc == 0
        assert "No conversations found" in capsys.readouterr().out


class TestCmdTagRename:
    def test_rename(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "old-name"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "rename", "old-name", "new-name"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Renamed" in out

    def test_rename_nonexistent(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "rename", "no-such", "new"])
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_rename_missing_args(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "rename", "only-one"])
        assert rc == 1
        assert "Usage:" in capsys.readouterr().out


class TestCmdTagDelete:
    def test_delete_unassociated(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "temp-tag"])
        main(["--db", str(test_db), "tag", "--remove", conv_id, "temp-tag"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "delete", "temp-tag"])
        assert rc == 0
        assert "Deleted" in capsys.readouterr().out

    def test_delete_with_associations_blocked(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "in-use"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "delete", "in-use"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "--force" in out

    def test_delete_force(self, test_db, capsys):
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "force-del"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tag", "delete", "force-del", "--force"])
        assert rc == 0
        assert "Deleted" in capsys.readouterr().out

    def test_delete_nonexistent(self, test_db, capsys):
        rc = main(["--db", str(test_db), "tag", "delete", "no-such-tag"])
        assert rc == 1
        assert "not found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Disambiguation: subcommand names vs conversation IDs
# ---------------------------------------------------------------------------


class TestSubcommandDisambiguation:
    def test_list_dispatches_to_subcommand(self, test_db, capsys):
        """'list' is recognized as a subcommand, not a conversation ID."""
        rc = main(["--db", str(test_db), "tag", "list"])
        assert rc == 0  # list succeeds (empty or with tags)

    def test_rename_dispatches_to_subcommand(self, test_db, capsys):
        """'rename' is recognized as a subcommand."""
        rc = main(["--db", str(test_db), "tag", "rename"])
        assert rc == 1  # fails due to missing args, but dispatched correctly
        assert "Usage:" in capsys.readouterr().out

    def test_delete_dispatches_to_subcommand(self, test_db, capsys):
        """'delete' is recognized as a subcommand."""
        rc = main(["--db", str(test_db), "tag", "delete"])
        assert rc == 1  # fails due to missing args, but dispatched correctly
        assert "Usage:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Deprecated 'tags' command (bridge)
# ---------------------------------------------------------------------------


class TestTagsDeprecationBridge:
    def test_tags_warns(self, test_db, capsys):
        """siftd tags emits deprecation warning."""
        main(["--db", str(test_db), "tags"])
        err = capsys.readouterr().err
        assert "deprecated" in err.lower()

    def test_tags_list_still_works(self, test_db, capsys):
        """siftd tags still lists tags (with warning)."""
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "bridge-tag"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags"])
        assert rc == 0
        combined = capsys.readouterr()
        assert "bridge-tag" in combined.out
        assert "deprecated" in combined.err.lower()

    def test_tags_rename_still_works(self, test_db, capsys):
        """siftd tags --rename still works (with warning)."""
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "old-bridge"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--rename", "old-bridge", "new-bridge"])
        assert rc == 0
        combined = capsys.readouterr()
        assert "Renamed" in combined.out
        assert "deprecated" in combined.err.lower()

    def test_tags_delete_still_works(self, test_db, capsys):
        """siftd tags --delete still works (with warning)."""
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        main(["--db", str(test_db), "tag", conv_id, "del-bridge"])
        main(["--db", str(test_db), "tag", "--remove", conv_id, "del-bridge"])
        capsys.readouterr()

        rc = main(["--db", str(test_db), "tags", "--delete", "del-bridge"])
        assert rc == 0
        combined = capsys.readouterr()
        assert "Deleted" in combined.out
        assert "deprecated" in combined.err.lower()
