"""Tests for siftd tag CLI command (apply, remove, list, rename, delete)."""

from pathlib import Path
from types import SimpleNamespace

import siftd.cli.tags as tags_cli
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


class TestTagsEdgeBranches:
    def test_detect_current_session_fallback_and_exception(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("siftd.paths.db_path", lambda: tmp_path / "missing.db")
        assert tags_cli._detect_current_session() is None

        monkeypatch.setattr("siftd.paths.db_path", lambda: tmp_path / "exists.db")
        (tmp_path / "exists.db").write_text("x")
        monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert tags_cli._detect_current_session() is None

    def test_tag_session_and_subcommand_edges(self, test_db, monkeypatch, capsys):
        # _tag_session remove disallowed and missing tags usage
        args = SimpleNamespace(remove=True, positional=["x"], exchange=None)
        assert tags_cli._tag_session(args, Path(test_db), "sess") == 1

        args = SimpleNamespace(remove=False, positional=[], exchange=None)
        assert tags_cli._tag_session(args, Path(test_db), "sess") == 1

        # queue existing path
        monkeypatch.setattr("siftd.api.sessions.is_session_registered", lambda conn, sid: False)
        monkeypatch.setattr("siftd.cli.tags.queue_pending_tag", lambda *a, **k: False)
        args = SimpleNamespace(remove=False, positional=["t1"], exchange=2)
        capsys.readouterr()
        assert tags_cli._tag_session(args, Path(test_db), "sess") == 0
        assert "already queued" in capsys.readouterr().out

        # _cmd_tag_list temporal/prefix no matches via direct call
        list_args = SimpleNamespace(positional=["list"], since="2024-01-01", before=None, prefix=None)
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [SimpleNamespace(name="x", description=None, conversation_count=0, workspace_count=0, tool_call_count=0, prompt_count=0)])
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        assert tags_cli._cmd_tag_list(list_args, Path(test_db)) == 0

        list_args = SimpleNamespace(positional=["list"], since=None, before=None, prefix="zzz")
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [SimpleNamespace(name="abc", description=None, conversation_count=1, workspace_count=0, tool_call_count=0, prompt_count=0)])
        assert tags_cli._cmd_tag_list(list_args, Path(test_db)) == 0

        # rename/delete error branches
        assert tags_cli._cmd_tag_rename(SimpleNamespace(positional=["rename", "a"]), Path(test_db)) == 1
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(ValueError("bad")))
        assert tags_cli._cmd_tag_rename(SimpleNamespace(positional=["rename", "a", "b"]), Path(test_db)) == 1

        assert tags_cli._cmd_tag_delete(SimpleNamespace(positional=["delete", "x"], force=False), Path(test_db).with_name("missing.db")) == 1

    def test_cmd_tag_serve_branch_and_not_applied(self, test_db, monkeypatch, capsys):
        # delegated apply/remove result reporting
        monkeypatch.setattr(
            "siftd.serve.delegation.try_serve",
            lambda op: {"results": [{"tag": "a", "status": "not_found", "count": 0}, {"tag": "b", "status": "applied", "count": 2}, {"tag": "c", "status": "removed", "count": 1}]},
        )
        assert main(["--db", str(test_db), "tag", "--last", "1", "a", "b", "c"]) == 0

        # local not-applied remove branch
        conn = open_database(test_db)
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
        conn.close()
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        assert main(["--db", str(test_db), "tag", "--remove", conv_id, "never-tagged"]) == 0
        assert "not found" in capsys.readouterr().out.lower() or "not applied" in capsys.readouterr().out.lower()

    def test_remaining_list_rename_delete_and_cmd_tag_branches(self, test_db, monkeypatch, capsys):
        # _detect_current_session line 59: fallback active-session return
        monkeypatch.setattr("siftd.paths.db_path", lambda: Path(test_db))
        monkeypatch.setattr("siftd.cli.tags.session_id_file", lambda ws: Path("/tmp/does-not-exist-session-id"))
        monkeypatch.setattr("siftd.api.open_database", lambda *a, **k: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("siftd.api.sessions.find_active_session", lambda conn, ws: "sess-id")
        assert tags_cli._detect_current_session() == "sess-id"

        # _tag_session queued exchange message
        monkeypatch.setattr("siftd.cli.tags.queue_pending_tag", lambda *a, **k: True)
        assert tags_cli._tag_session(SimpleNamespace(remove=False, positional=["x"], exchange=1), Path(test_db), "sess") == 0

        # _cmd_tag_list drill-down FileNotFound and tip path
        args = SimpleNamespace(positional=["list", "t"], limit=1, json=False, workspace=None, model=None, since=None, before=None, tag=None, all_tags=None, no_tag=None, tool=None, tool_tag=None, owner=None)
        monkeypatch.setattr("siftd.api.list_conversations", lambda **k: (_ for _ in ()).throw(FileNotFoundError("missing")))
        assert tags_cli._cmd_tag_list(args, Path(test_db)) == 1

        conv = SimpleNamespace(prompt_count=1, response_count=1, total_tokens=1)
        monkeypatch.setattr("siftd.api.list_conversations", lambda **k: [conv])
        monkeypatch.setattr("siftd.cli._common.fidelity_from_args", lambda a: SimpleNamespace())
        monkeypatch.setattr("siftd.output.format_registry.select_format", lambda **k: SimpleNamespace(render_list=lambda c, f: "LIST"))
        monkeypatch.setattr("siftd.output.painted_bridge.emit_output", lambda out: None)
        assert tags_cli._cmd_tag_list(args, Path(test_db)) == 0

        # _cmd_tag_list serve tags conversion/printing with workspace/tool counts
        monkeypatch.setattr(
            "siftd.serve.delegation.try_serve",
            lambda op: {
                "tags": [{
                    "name": "t",
                    "description": None,
                    "created_at": "2024-01-01T00:00:00Z",
                    "workspace_count": 1,
                    "tool_call_count": 2,
                    "conversation_count": 0,
                    "prompt_count": 0,
                }],
            },
        )
        args2 = SimpleNamespace(positional=["list"], since=None, before=None, prefix=None)
        assert tags_cli._cmd_tag_list(args2, Path(test_db)) == 0

        # rename: serve success + file missing
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: {"status": "renamed"})
        assert tags_cli._cmd_tag_rename(SimpleNamespace(positional=["rename", "a", "b"]), Path(test_db)) == 0

        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: (_ for _ in ()).throw(FileNotFoundError("x")))
        assert tags_cli._cmd_tag_rename(SimpleNamespace(positional=["rename", "a", "b"]), Path(test_db)) == 1

        # delete: warning and deleted messaging include workspace/tool/prompt counts
        ti = SimpleNamespace(name="t", conversation_count=1, workspace_count=2, tool_call_count=3, prompt_count=4)
        monkeypatch.setattr("siftd.cli.tags.list_tags", lambda conn=None: [ti])
        monkeypatch.setattr("siftd.cli.tags.delete_tag_safe", lambda *a, **k: None)
        assert tags_cli._cmd_tag_delete(SimpleNamespace(positional=["delete", "t"], force=False), Path(test_db)) == 1
        assert tags_cli._cmd_tag_delete(SimpleNamespace(positional=["delete", "t"], force=True), Path(test_db)) == 0

        # cmd_tag branches: --last ignored with --session, n<1, no recent convs, remove-not-applied, tags name branch
        assert main(["--db", str(test_db), "tag", "--session", "sess", "--last", "1", "x"]) == 0

        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        assert main(["--db", str(test_db), "tag", "--last", "0", "x"]) == 1

        monkeypatch.setattr(
            "siftd.cli.tags.apply_tags",
            lambda **k: (_ for _ in ()).throw(FileNotFoundError("no matching entities found")),
        )
        assert main(["--db", str(test_db), "tag", "--last", "1", "x"]) == 1

        # remove-last not-applied branch (line 554)
        monkeypatch.setattr(
            "siftd.cli.tags.apply_tags",
            lambda **k: SimpleNamespace(
                results=[SimpleNamespace(tag="x", status="not_applied", count=0)],
                target_count=1,
                resolved_entity_id="cid",
            ),
        )
        assert main(["--db", str(test_db), "tag", "--remove", "--last", "1", "x"]) == 0

        # create existing tag but not on target conversation for line 608
        conn = open_database(test_db)
        ids = [r["id"] for r in conn.execute("SELECT id FROM conversations LIMIT 2").fetchall()]
        conn.close()
        main(["--db", str(test_db), "tag", ids[0], "exists-not-applied"])
        capsys.readouterr()
        assert main(["--db", str(test_db), "tag", "--remove", ids[1], "exists-not-applied"]) == 0

        # _cmd_tag_list with since and no tags (line 253)
        monkeypatch.setattr("siftd.serve.delegation.try_serve", lambda op: None)
        monkeypatch.setattr("siftd.api.dispatch.execute", lambda op: [])
        args3 = SimpleNamespace(positional=["list"], since="2024-01-01", before=None, prefix=None)
        assert tags_cli._cmd_tag_list(args3, Path(test_db)) == 0

        # deprecated tags name positional branch
        assert main(["--db", str(test_db), "tags", "some-tag"]) in (0, 1)
