"""Tests for siftd data CLI commands (ingest, backfill, migrate, doctor, copy)."""

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from conftest import FIXTURES_DIR

import siftd.cli.data as data_cli
from siftd.cli import main
from siftd.cli.data import _AdapterCounts, _IngestJsonRenderer, _IngestTextRenderer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeEvent:
    """Minimal IngestEvent stand-in for renderer tests."""

    adapter: str = "test_adapter"
    status: str = "ingested"
    reason: str | None = None
    path: str = "/fake/path.jsonl"
    index: int | None = 1
    total: int | None = 2
    workspace_path: str | None = "/project"
    summary: str | None = "test summary"
    exchange_count: int | None = 5
    model: str | None = "claude-3"
    error: str | None = None


@dataclass
class FakeStats:
    """Minimal IngestStats stand-in for renderer tests."""

    files_found: int = 10
    files_ingested: int = 5
    files_skipped: int = 3
    files_replaced: int = 1
    files_errored: int = 1
    conversations: int = 5
    prompts: int = 10
    responses: int = 10
    tool_calls: int = 3
    by_harness: dict = None

    def __post_init__(self):
        if self.by_harness is None:
            self.by_harness = {}


# ---------------------------------------------------------------------------
# _AdapterCounts
# ---------------------------------------------------------------------------


class TestAdapterCounts:
    def test_initial_state(self):
        counts = _AdapterCounts(total=10)
        assert counts.total == 10
        assert counts.processed == 0
        assert counts.new == 0
        assert counts.updated == 0
        assert counts.replaced == 0
        assert counts.skipped == 0
        assert counts.error == 0

    def test_add_ingested(self):
        counts = _AdapterCounts()
        counts.add("ingested", None)
        assert counts.new == 1
        assert counts.processed == 1

    def test_add_updated(self):
        counts = _AdapterCounts()
        counts.add("updated", None)
        assert counts.updated == 1

    def test_add_replaced(self):
        counts = _AdapterCounts()
        counts.add("replaced", None)
        assert counts.replaced == 1

    def test_add_skipped_with_reason(self):
        counts = _AdapterCounts()
        counts.add("skipped", "unchanged")
        counts.add("skipped", "unchanged")
        counts.add("skipped", "binary")
        assert counts.skipped == 3
        assert counts.skip_reasons == {"unchanged": 2, "binary": 1}

    def test_add_error(self):
        counts = _AdapterCounts()
        counts.add("error", None)
        assert counts.error == 1

    def test_updated_total_combines_updated_and_replaced(self):
        counts = _AdapterCounts()
        counts.add("updated", None)
        counts.add("replaced", None)
        counts.add("replaced", None)
        assert counts.updated_total == 3

    def test_none_total_defaults_to_zero(self):
        counts = _AdapterCounts(total=None)
        assert counts.total == 0


# ---------------------------------------------------------------------------
# _IngestTextRenderer
# ---------------------------------------------------------------------------


class TestIngestTextRenderer:
    def test_quiet_mode_suppresses_per_file_output(self, capsys):
        renderer = _IngestTextRenderer(verbose=False, quiet=True)
        event = FakeEvent(index=1, total=1)
        renderer.handle_event(event)
        assert capsys.readouterr().out == ""

    def test_normal_mode_prints_adapter_header_and_line(self, capsys):
        renderer = _IngestTextRenderer(verbose=False)
        event = FakeEvent(index=1, total=2)
        renderer.handle_event(event)
        out = capsys.readouterr().out
        assert "test_adapter (2 files)" in out
        assert "new" in out

    def test_skipped_events_not_printed(self, capsys):
        renderer = _IngestTextRenderer(verbose=False)
        # First event starts the adapter, second is skipped
        renderer.handle_event(FakeEvent(status="ingested", index=1, total=2))
        capsys.readouterr()  # clear first event output
        renderer.handle_event(FakeEvent(status="skipped", reason="unchanged", index=2, total=2))
        out = capsys.readouterr().out
        # Skipped files don't get their own line, but the done summary appears
        assert "skipped 1" in out

    def test_adapter_done_summary_on_last_event(self, capsys):
        renderer = _IngestTextRenderer(verbose=False)
        renderer.handle_event(FakeEvent(status="ingested", index=1, total=2))
        renderer.handle_event(FakeEvent(status="skipped", reason="unchanged", index=2, total=2))
        out = capsys.readouterr().out
        assert "totals:" in out
        assert "new 1" in out
        assert "skipped 1" in out

    def test_verbose_shows_skip_reasons(self, capsys):
        renderer = _IngestTextRenderer(verbose=True)
        renderer.handle_event(FakeEvent(status="ingested", index=1, total=3))
        renderer.handle_event(FakeEvent(status="skipped", reason="unchanged", index=2, total=3))
        renderer.handle_event(FakeEvent(status="skipped", reason="binary", index=3, total=3))
        out = capsys.readouterr().out
        assert "unchanged" in out
        assert "binary" in out

    def test_print_summary_no_files_found(self, capsys):
        renderer = _IngestTextRenderer(verbose=False)
        stats = FakeStats(files_found=0, conversations=0, prompts=0, responses=0, tool_calls=0)
        renderer.print_summary(stats)
        assert "No files found" in capsys.readouterr().out

    def test_print_summary_all_up_to_date(self, capsys):
        renderer = _IngestTextRenderer(verbose=False)
        # Simulate an adapter that only skipped files
        counts = _AdapterCounts(total=5)
        for _ in range(5):
            counts.add("skipped", "unchanged")
        renderer._counts["test_adapter"] = counts
        stats = FakeStats(
            files_found=5, files_ingested=0, files_replaced=0,
            conversations=0, prompts=0, responses=0, tool_calls=0,
        )
        renderer.print_summary(stats)
        assert "all up to date" in capsys.readouterr().out

    def test_print_summary_quiet_shows_totals_line(self, capsys):
        renderer = _IngestTextRenderer(verbose=False, quiet=True)
        counts = _AdapterCounts(total=2)
        counts.add("ingested", None)
        counts.add("ingested", None)
        renderer._counts["test_adapter"] = counts
        stats = FakeStats(conversations=2, prompts=4, responses=4, tool_calls=1)
        renderer.print_summary(stats)
        out = capsys.readouterr().out
        assert "2 conversations" in out
        assert "4 prompts" in out

    def test_status_label_mapping(self):
        assert _IngestTextRenderer._status_label("ingested") == "new"
        assert _IngestTextRenderer._status_label("replaced") == "updated"
        assert _IngestTextRenderer._status_label("error") == "error"
        assert _IngestTextRenderer._status_label("skipped") == "skipped"

    def test_fit_truncates_long_text(self):
        assert _IngestTextRenderer._fit("hello world", 5) == "he..."
        assert _IngestTextRenderer._fit("hi", 10) == "hi        "

    def test_fit_narrow_width(self):
        # Width <= 3 doesn't add ellipsis
        assert _IngestTextRenderer._fit("hello", 3) == "hel"


# ---------------------------------------------------------------------------
# _IngestJsonRenderer
# ---------------------------------------------------------------------------


class TestIngestJsonRenderer:
    def test_handle_db_emits_json(self, capsys):
        renderer = _IngestJsonRenderer()
        renderer.handle_db(db=Path("/test/db.sqlite"), is_new=True)
        line = capsys.readouterr().out.strip()
        data = json.loads(line)
        assert data["type"] == "db"
        assert data["path"] == "/test/db.sqlite"
        assert data["state"] == "created"

    def test_handle_db_existing(self, capsys):
        renderer = _IngestJsonRenderer()
        renderer.handle_db(db=Path("/test/db.sqlite"), is_new=False)
        data = json.loads(capsys.readouterr().out.strip())
        assert data["state"] == "existing"

    def test_handle_event_emits_adapter_start_and_file(self, capsys):
        renderer = _IngestJsonRenderer()
        event = FakeEvent(index=1, total=3)
        renderer.handle_event(event)
        lines = capsys.readouterr().out.strip().split("\n")
        assert len(lines) == 2
        start = json.loads(lines[0])
        assert start["type"] == "adapter_start"
        assert start["adapter"] == "test_adapter"
        file_event = json.loads(lines[1])
        assert file_event["type"] == "file"
        assert file_event["status"] == "ingested"

    def test_handle_event_skipped_omits_extra_fields(self, capsys):
        renderer = _IngestJsonRenderer()
        event = FakeEvent(status="skipped", reason="unchanged", index=1, total=2)
        renderer.handle_event(event)
        lines = capsys.readouterr().out.strip().split("\n")
        # Lines: adapter_start, file
        file_event = json.loads(lines[-1])
        assert file_event["status"] == "skipped"
        assert "workspace" not in file_event

    def test_handle_event_emits_adapter_summary_on_last(self, capsys):
        renderer = _IngestJsonRenderer()
        renderer.handle_event(FakeEvent(status="ingested", index=1, total=2))
        renderer.handle_event(FakeEvent(status="skipped", reason="unchanged", index=2, total=2))
        lines = capsys.readouterr().out.strip().split("\n")
        last = json.loads(lines[-1])
        assert last["type"] == "adapter_summary"
        assert last["new"] == 1
        assert last["skipped"] == 1

    def test_handle_summary(self, capsys):
        renderer = _IngestJsonRenderer()
        stats = FakeStats()
        renderer.handle_summary(stats)
        data = json.loads(capsys.readouterr().out.strip())
        assert data["type"] == "summary"
        assert data["conversations"] == 5
        assert data["files"]["found"] == 10


# ---------------------------------------------------------------------------
# cmd_ingest
# ---------------------------------------------------------------------------


class TestCmdIngest:
    def test_ingest_json_mode(self, tmp_path, capsys):
        """--json emits structured JSON events."""
        db_path = tmp_path / "new.db"
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        rc = main([
            "--db", str(db_path),
            "ingest", "--json",
            "--adapter", "claude_code",
            "--path", str(tmp_path / "projects"),
        ])

        assert rc == 0
        out = capsys.readouterr().out
        lines = [json.loads(line) for line in out.strip().split("\n") if line.strip()]
        types = [line["type"] for line in lines]
        assert "db" in types
        assert "summary" in types

    def test_ingest_quiet_mode(self, tmp_path, capsys):
        """--quiet suppresses per-file output."""
        db_path = tmp_path / "test.db"

        rc = main([
            "--db", str(db_path),
            "ingest", "--quiet",
            "--adapter", "claude_code",
            "--path", "/nonexistent/path",
        ])

        assert rc == 0
        out = capsys.readouterr().out
        # Quiet mode: no "Creating database" or per-file lines
        assert "Creating database" not in out

    def test_ingest_rebuild_fts(self, test_db, capsys):
        """--rebuild-fts rebuilds index without ingesting."""
        rc = main(["--db", str(test_db), "ingest", "--rebuild-fts"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Rebuilding FTS index" in out
        assert "FTS index rebuilt" in out

    def test_ingest_rebuild_fts_json(self, test_db, capsys):
        """--rebuild-fts --json emits JSON events."""
        rc = main(["--db", str(test_db), "ingest", "--rebuild-fts", "--json"])
        assert rc == 0
        lines = capsys.readouterr().out.strip().split("\n")
        events = [json.loads(line) for line in lines]
        types = [e["type"] for e in events]
        assert "fts_rebuild" in types


# ---------------------------------------------------------------------------
# cmd_backfill
# ---------------------------------------------------------------------------


class TestCmdBackfill:
    def test_backfill_default_response_attributes(self, test_db, capsys):
        """Default backfill runs response attribute backfill."""
        rc = main(["--db", str(test_db), "backfill"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "cache tokens" in out.lower() or "attributes" in out.lower()

    def test_backfill_filter_binary_dry_run(self, test_db, capsys):
        """--filter-binary --dry-run previews without changing data."""
        rc = main(["--db", str(test_db), "backfill", "--filter-binary", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry run" in out.lower()

    def test_backfill_filter_binary(self, test_db, capsys):
        """--filter-binary runs without error."""
        rc = main(["--db", str(test_db), "backfill", "--filter-binary"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Filtered:" in out

    def test_backfill_dry_run_warning_without_filter_binary(self, test_db, capsys):
        """--dry-run without --filter-binary warns."""
        rc = main(["--db", str(test_db), "backfill", "--dry-run"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "--dry-run ignored" in err


# ---------------------------------------------------------------------------
# cmd_migrate
# ---------------------------------------------------------------------------


class TestCmdMigrate:
    def test_migrate_status_display(self, test_db, capsys):
        """Default migrate shows workspace identity status."""
        rc = main(["--db", str(test_db), "migrate"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Workspace identity status" in out
        assert "Total workspaces:" in out

    def test_migrate_missing_db(self, tmp_path, capsys):
        """Migrate with missing database returns error."""
        rc = main(["--db", str(tmp_path / "missing.db"), "migrate"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower() or "Database" in out

    def test_migrate_merge_workspaces_dry_run(self, test_db, capsys):
        """--merge-workspaces --dry-run runs without modifying data."""
        rc = main(["--db", str(test_db), "migrate", "--merge-workspaces", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Backfilling" in out or "Step 1" in out


# ---------------------------------------------------------------------------
# cmd_copy
# ---------------------------------------------------------------------------


class TestCmdCopy:
    def test_copy_adapter_no_name_lists_available(self, capsys):
        """siftd copy adapter (no name) lists available adapters."""
        rc = main(["copy", "adapter"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "Available adapters:" in out

    def test_copy_query_no_name_lists_available(self, capsys):
        """siftd copy query (no name) lists available queries."""
        rc = main(["copy", "query"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "Usage:" in out

    def test_copy_adapter_by_name(self, tmp_path, monkeypatch, capsys):
        """siftd copy adapter <name> copies to config dir."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        rc = main(["copy", "adapter", "claude_code"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Copied" in out
        assert "claude_code" in out

    def test_copy_adapter_refuses_overwrite(self, tmp_path, monkeypatch, capsys):
        """siftd copy adapter refuses overwrite without --force."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        main(["copy", "adapter", "claude_code"])
        rc = main(["copy", "adapter", "claude_code"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "Error" in out

    def test_copy_adapter_force_overwrite(self, tmp_path, monkeypatch, capsys):
        """siftd copy adapter --force overwrites existing."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        main(["copy", "adapter", "claude_code"])
        rc = main(["copy", "adapter", "claude_code", "--force"])
        assert rc == 0

    def test_copy_adapter_all(self, tmp_path, monkeypatch, capsys):
        """siftd copy adapter --all copies all adapters."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        rc = main(["copy", "adapter", "--all"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Copied adapters:" in out

    def test_copy_nonexistent_adapter(self, tmp_path, monkeypatch, capsys):
        """siftd copy adapter <bad-name> returns error."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        rc = main(["copy", "adapter", "nonexistent_xyz"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "Error" in out


# ---------------------------------------------------------------------------
# cmd_doctor
# ---------------------------------------------------------------------------


class TestCmdDoctor:
    def test_doctor_list(self, capsys):
        """siftd doctor list shows available checks."""
        rc = main(["doctor", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ingest-pending" in out

    def test_doctor_list_json(self, capsys):
        """siftd doctor list --json returns JSON."""
        rc = main(["doctor", "list", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        assert all("name" in item for item in data)

    def test_doctor_run_default(self, test_db, capsys):
        """siftd doctor runs all checks."""
        rc = main(["--db", str(test_db), "doctor"])
        # rc depends on whether issues are found
        assert rc in (0, 1)

    def test_doctor_run_json(self, test_db, capsys):
        """siftd doctor --json returns structured findings."""
        rc = main(["--db", str(test_db), "doctor", "--json"])
        assert rc in (0, 1)
        data = json.loads(capsys.readouterr().out)
        assert "findings" in data
        assert "summary" in data
        assert isinstance(data["findings"], list)

    def test_doctor_run_specific_check(self, test_db, capsys):
        """siftd doctor run <check> runs only that check."""
        from siftd.api import list_checks

        checks = list_checks()
        if checks:
            check_name = checks[0].name
            rc = main(["--db", str(test_db), "doctor", "run", check_name])
            assert rc in (0, 1)

    def test_doctor_run_unknown_check(self, test_db, capsys):
        """siftd doctor run <unknown> returns error."""
        rc = main(["--db", str(test_db), "doctor", "run", "nonexistent_check_xyz"])
        assert rc == 1

    def test_doctor_fix_shows_fix_commands(self, test_db, capsys):
        """siftd doctor fix shows fix suggestions."""
        rc = main(["--db", str(test_db), "doctor", "fix"])
        assert rc in (0, 1)

    def test_doctor_strict_mode(self, test_db, capsys):
        """--strict exits 1 on warnings too."""
        rc_normal = main(["--db", str(test_db), "doctor", "--json"])
        data = json.loads(capsys.readouterr().out)
        warning_count = data["summary"]["warning"]

        rc_strict = main(["--db", str(test_db), "doctor", "--json", "--strict"])
        capsys.readouterr()  # consume output

        if warning_count > 0 and data["summary"]["error"] == 0:
            # Strict should fail on warnings, normal should pass
            assert rc_normal == 0
            assert rc_strict == 1

    def test_doctor_pending_tags_warning_without_fix(self, test_db, capsys):
        """--pending-tags without 'fix' subcommand warns."""
        main(["--db", str(test_db), "doctor", "--pending-tags"])
        err = capsys.readouterr().err
        assert "--pending-tags ignored" in err

    def test_doctor_fix_pending_tags(self, test_db, capsys):
        """siftd doctor fix --pending-tags runs cleanup."""
        rc = main(["--db", str(test_db), "doctor", "fix", "--pending-tags"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "stale" in out.lower() or "clean" in out.lower()

    def test_doctor_fix_pending_tags_json(self, test_db, capsys):
        """siftd doctor fix --pending-tags --json returns JSON."""
        rc = main(["--db", str(test_db), "doctor", "fix", "--pending-tags", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "sessions_deleted" in data
        assert "tags_deleted" in data

    def test_doctor_fix_pending_tags_missing_db(self, tmp_path, capsys):
        """doctor fix --pending-tags with missing DB returns error."""
        rc = main(["--db", str(tmp_path / "missing.db"), "doctor", "fix", "--pending-tags"])
        assert rc == 1

    def test_doctor_legacy_checks_alias(self, capsys):
        """Legacy 'siftd doctor checks' maps to list."""
        rc = main(["doctor", "checks"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ingest-pending" in out


class TestDataDirectBranches:
    def test_copy_query_and_formatter_branches(self, monkeypatch, capsys):
        monkeypatch.setattr("siftd.api.list_builtin_queries", lambda: [])
        rc = data_cli.cmd_copy(SimpleNamespace(resource_type="query", name=None, force=False, all=True))
        assert rc == 1

        monkeypatch.setattr("siftd.api.list_builtin_queries", lambda: ["cost"])
        monkeypatch.setattr("siftd.api.copy_query", lambda n, force=False: f"/tmp/{n}.sql")
        rc = data_cli.cmd_copy(SimpleNamespace(resource_type="query", name=None, force=False, all=False))
        assert rc == 1
        assert "Available queries" in capsys.readouterr().out

        rc = data_cli.cmd_copy(SimpleNamespace(resource_type="query", name="cost", force=False, all=False))
        assert rc == 0

        monkeypatch.setattr("siftd.api.list_builtin_formatters", lambda: [])
        rc = data_cli.cmd_copy(SimpleNamespace(resource_type="formatter", name=None, force=False, all=True))
        assert rc == 1

        monkeypatch.setattr("siftd.api.list_builtin_formatters", lambda: ["markdown"])
        monkeypatch.setattr("siftd.api.copy_formatter", lambda n, force=False: f"/tmp/{n}.py")
        rc = data_cli.cmd_copy(SimpleNamespace(resource_type="formatter", name="markdown", force=False, all=False))
        assert rc == 0

        rc = data_cli.cmd_copy(SimpleNamespace(resource_type="unknown", name=None, force=False, all=False))
        assert rc == 1

    def test_doctor_fix_paths_and_registry_fixes(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr("siftd.doctor.fixes.load_findings_cache", lambda: [])
        rc = data_cli._doctor_fix(SimpleNamespace(db=str(test_db)))
        assert rc == 1

        monkeypatch.setattr("siftd.doctor.fixes.load_findings_cache", lambda: [{"fix_command": "unknown"}])
        monkeypatch.setattr("siftd.doctor.fixes.clear_findings_cache", lambda: None)
        monkeypatch.setattr("siftd.api.open_database", lambda *_a, **_k: SimpleNamespace(close=lambda: None))
        rc = data_cli._doctor_fix(SimpleNamespace(db=str(test_db)))
        assert rc == 0

        # _fix helpers
        monkeypatch.setattr("siftd.adapters.registry.load_all_adapters", lambda: [SimpleNamespace(module="m")])
        monkeypatch.setattr("siftd.ingestion.orchestration.ingest_all", lambda conn, mods: SimpleNamespace(files_ingested=1, files_skipped=2))
        assert "1 file" in data_cli._fix_ingest(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.search.rebuild_fts_index", lambda conn: None)
        assert "FTS index rebuilt" in data_cli._fix_rebuild_fts(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.search.build_index", lambda **k: {"chunks_added": 3})
        assert "3 chunk" in data_cli._fix_search_index(object(), Path("/d"))
        assert "3 chunk" in data_cli._fix_search_rebuild(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.migrations.migrate_blobs", lambda conn: {"migrated": 2, "blobs_created": 4})
        assert "2 result" in data_cli._fix_migrate_blobs(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.migrations.backfill_git_remotes", lambda conn: {"updated": 5})
        assert "5 workspace" in data_cli._fix_backfill_git_remote(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.migrations.merge_duplicate_workspaces", lambda conn: {"workspaces_merged": 6})
        assert "6 workspace" in data_cli._fix_merge_workspaces(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.sessions.cleanup_stale_sessions", lambda *_a, **_k: (7, 8))
        assert "7 session" in data_cli._fix_pending_tags(object(), Path("/d"))

    def test_doctor_run_json_plain_and_painted_error_paths(self, test_db, monkeypatch):
        args = SimpleNamespace(db=str(test_db), json=False, strict=False)

        monkeypatch.setattr("siftd.api.run_checks", lambda **k: (_ for _ in ()).throw(FileNotFoundError("missing")))
        assert data_cli._doctor_run_json(args, None, False, Path(test_db)) == 1
        assert data_cli._doctor_run_plain(args, None, False, Path(test_db)) == 1

        monkeypatch.setattr("siftd.api.run_checks", lambda **k: (_ for _ in ()).throw(ValueError("bad")))
        assert data_cli._doctor_run_json(args, None, False, Path(test_db)) == 1
        assert data_cli._doctor_run_plain(args, None, False, Path(test_db)) == 1

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def render(self, *_a, **_k):
                return None

            def finalize(self, *_a, **_k):
                return None

        class _Theme:
            def __enter__(self):
                return None

            def __exit__(self, *_a):
                return False

        monkeypatch.setitem(
            __import__("sys").modules,
            "painted",
            SimpleNamespace(InPlaceRenderer=_R, use_theme=lambda *_a, **_k: _Theme()),
        )
        monkeypatch.setattr("siftd.api.list_checks", lambda: [SimpleNamespace(name="c1")])
        monkeypatch.setattr("siftd.doctor.view.render_progress_block", lambda *_a, **_k: "blk")
        monkeypatch.setitem(__import__("sys").modules, "siftd.output.theme", SimpleNamespace(siftd_theme=object()))
        monkeypatch.setattr("siftd.api.run_checks", lambda **k: [])
        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda findings: None)
        assert data_cli._doctor_run_painted(args, ["c1"], False, Path(test_db)) == 0

    def test_migrate_merge_verbose_and_dry_run_outputs(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr(
            "siftd.api.migrations.backfill_git_remotes",
            lambda conn, on_progress, dry_run: (on_progress("progress"), {"checked": 1, "updated": 1, "skipped_missing": 0, "skipped_no_git": 0})[1],
        )
        monkeypatch.setattr("siftd.api.migrations.verify_workspace_identity", lambda conn: {"duplicate_groups": 1, "duplicate_workspaces": 2, "total": 2, "with_remote": 1, "without_remote": 1})
        monkeypatch.setattr("siftd.api.migrations.merge_duplicate_workspaces", lambda conn, on_progress, dry_run: (on_progress("merging"), {"workspaces_merged": 1, "conversations_moved": 2})[1])
        rc = main(["--db", str(test_db), "migrate", "--merge-workspaces", "--dry-run", "-v"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "progress" in out and "Would merge" in out

        monkeypatch.setattr("siftd.api.migrations.verify_workspace_identity", lambda conn: {"duplicate_groups": 0, "duplicate_workspaces": 0, "total": 1, "with_remote": 1, "without_remote": 0})
        rc = main(["--db", str(test_db), "migrate", "--merge-workspaces"])
        assert rc == 0

    def test_ingest_and_backfill_remaining_branches(self, test_db, monkeypatch, capsys):
        # cmd_ingest: unmatched adapter in json mode
        monkeypatch.setattr("siftd.adapters.registry.load_all_adapters", lambda: [])
        monkeypatch.setattr("siftd.api.create_database", lambda db: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)
        rc = main(["--db", str(test_db), "ingest", "--json", "--adapter", "nope"])
        assert rc == 1

        # cmd_backfill: shell-tags, derivative-tags, and filter-binary error/notice prints
        monkeypatch.setattr("siftd.backfill.backfill_shell_tags", lambda conn: {"git": 2})
        rc = main(["--db", str(test_db), "backfill", "--shell-tags"])
        assert rc == 0

        monkeypatch.setattr("siftd.backfill.backfill_derivative_tags", lambda conn: 1)
        rc = main(["--db", str(test_db), "backfill", "--derivative-tags"])
        assert rc == 0

        monkeypatch.setattr("siftd.backfill.backfill_filter_binary", lambda conn, dry_run=False: {"filtered": 1, "skipped": 0, "errors": 2})
        rc = main(["--db", str(test_db), "backfill", "--filter-binary", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Errors" in out and "Run without --dry-run" in out
