"""Tests for siftd data CLI commands (ingest, backfill, migrate, doctor, copy)."""

import io
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FIXTURES_DIR

import siftd.cli.data as data_cli
from siftd.cli import main
from siftd.cli.data import _AdapterCounts, _IngestJsonRenderer, _IngestTextRenderer
from siftd.output.live import LiveRegion
from siftd.output.progress_view import ProgressConsumer


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal (drives the live path active)."""

    def isatty(self) -> bool:
        return True

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
        # Empty-states route through the status vocabulary (ℹ, stderr).
        assert "No files found" in capsys.readouterr().err

    def test_print_summary_all_up_to_date(self, capsys):
        renderer = _IngestTextRenderer(verbose=False)
        # Simulate an adapter that only skipped files
        counts = _AdapterCounts(total=5)
        for _ in range(5):
            counts.add("skipped", "unchanged")
        renderer._counts["test_adapter"] = counts
        stats = FakeStats(
            files_found=5, files_ingested=0, files_replaced=0, files_errored=0,
            conversations=0, prompts=0, responses=0, tool_calls=0,
        )
        renderer.print_summary(stats)
        # Empty-states route through the status vocabulary (ℹ, stderr).
        assert "all up to date" in capsys.readouterr().err

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

    def test_print_summary_table_shows_per_adapter_content(self, capsys):
        # The summary pivots to per-adapter CONTENT yield (from by_harness) —
        # not the file disposition the bars already carried.
        renderer = _IngestTextRenderer(verbose=False)
        stats = FakeStats(
            files_found=5, files_errored=0, conversations=3, prompts=8, responses=8, tool_calls=2,
            by_harness={
                "claude_code": {"conversations": 2, "prompts": 6, "responses": 6, "tool_calls": 2},
                "aider": {"conversations": 1, "prompts": 2, "responses": 2, "tool_calls": 0},
            },
        )
        renderer.print_summary(stats)
        out = capsys.readouterr().out
        for header in ("ADAPTER", "CONVERSATIONS", "PROMPTS", "RESPONSES", "TOOL_CALLS"):
            assert header in out
        assert "NEW" not in out and "SKIPPED" not in out  # no longer file disposition
        assert "claude_code" in out and "aider" in out
        assert "3 conversations" in out  # grand-total footer

    def test_print_summary_table_excludes_adapters_with_no_content(self, capsys):
        # An all-skip adapter yields nothing → no row in the content table.
        renderer = _IngestTextRenderer(verbose=False)
        stats = FakeStats(
            files_found=10, files_errored=0, conversations=2, prompts=4, responses=4, tool_calls=1,
            by_harness={
                "claude_code": {"conversations": 2, "prompts": 4, "responses": 4, "tool_calls": 1},
                "vscode": {"conversations": 0, "prompts": 0, "responses": 0, "tool_calls": 0},
            },
        )
        renderer.print_summary(stats)
        out = capsys.readouterr().out
        assert "claude_code" in out
        assert "vscode" not in out

    def test_print_summary_warns_when_errors_and_nothing_landed(self, capsys):
        # Files errored and nothing new ingested → a warning, not "all up to date".
        renderer = _IngestTextRenderer(verbose=False)
        stats = FakeStats(
            files_found=3, files_errored=2,
            conversations=0, prompts=0, responses=0, tool_calls=0,
        )
        renderer.print_summary(stats)
        err = capsys.readouterr().err
        assert "errored" in err
        assert "all up to date" not in err

    def test_active_ingest_bars_paint_to_tty(self, monkeypatch):
        # The active live path: handle_event feeds the ProgressConsumer, which
        # drives the REAL InPlaceRenderer (against a fake-TTY sink); `with
        # consumer` deposits the final bar frame on a clean exit.
        monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: True)
        stream = _FakeTTY()
        consumer = ProgressConsumer(shape="bars", live=LiveRegion(stream=stream))
        assert consumer.active
        renderer = _IngestTextRenderer(verbose=False)
        renderer.attach_consumer(consumer)
        with consumer:
            renderer.handle_event(FakeEvent(adapter="claude_code", status="ingested", index=1, total=2))
            renderer.handle_event(
                FakeEvent(adapter="claude_code", status="skipped", reason="unchanged", index=2, total=2)
            )
        out = stream.getvalue()
        assert "━" in out  # a (thin) progress bar was painted
        assert "claude_code" in out  # the adapter label rode the bar row

    def test_ingest_consumer_lifecycle_survives_empty_frames(self, monkeypatch):
        renderer = _IngestTextRenderer(verbose=False)

        # Inactive consumer (not a TTY): attach + `with consumer` is a clean
        # no-op even with no events fed.
        inactive = ProgressConsumer(shape="bars", live=LiveRegion(stream=io.StringIO()))
        renderer.attach_consumer(inactive)
        with inactive:
            pass

        # Active but zero adapters fed → the deposited final block is empty; the
        # clean-exit finalize on an empty Block must not raise.
        monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: True)
        active = ProgressConsumer(shape="bars", live=LiveRegion(stream=_FakeTTY()))
        renderer.attach_consumer(active)
        with active:
            pass

    def test_progress_event_maps_error_adapter(self):
        # The boundary helper: a completed adapter that hit a file error maps to a
        # status="error" event (the row's ✗ glyph) with err in the amber tally —
        # severity rides the glyph, the err count stays on the metric thread.
        counts = _AdapterCounts(total=2)
        counts.add("ingested", None)
        counts.add("error", None)
        ev = _IngestTextRenderer._progress_event("codex", counts, done=True, terminal=True)
        assert ev.group == "codex"
        assert ev.status == "error"
        assert ev.index == 2 and ev.total == 2
        assert ev.tally == {"new": 1, "upd": 0, "skip": 0, "err": 1}
        assert ev.terminal is True

    def test_progress_event_omits_err_clean_and_sweeps_zero_total(self):
        # No errors → no err key, status progress while in flight.
        counts = _AdapterCounts(total=2)
        counts.add("ingested", None)
        ev = _IngestTextRenderer._progress_event("aider", counts, done=False, terminal=False)
        assert ev.status == "progress"
        assert "err" not in ev.tally
        assert ev.tally == {"new": 1, "upd": 0, "skip": 0}
        # A zero-total adapter maps total→None so the consumer draws the
        # indeterminate sweep instead of dividing by zero on a 0/0 bar.
        zero = _AdapterCounts(total=0)
        ev0 = _IngestTextRenderer._progress_event("x", zero, done=False, terminal=False)
        assert ev0.total is None

    def test_progress_event_done_clean_is_status_done(self):
        counts = _AdapterCounts(total=3)
        for _ in range(3):
            counts.add("ingested", None)
        ev = _IngestTextRenderer._progress_event("claude_code", counts, done=True, terminal=True)
        assert ev.status == "done"  # done + no errors → ✓, not ✗
        assert "err" not in ev.tally

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

    def test_ingest_rebuild_fts(self, test_db, capsys, monkeypatch):
        """--rebuild-fts rebuilds index without ingesting."""
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
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

    def test_ingest_rebuild_fts_auto_quiet_emits_hint(self, test_db, capsys, monkeypatch):
        """When piped, --rebuild-fts is quiet by default and emits the behavior-change hint."""
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        rc = main(["--db", str(test_db), "ingest", "--rebuild-fts"])
        assert rc == 0
        out, err = capsys.readouterr()
        assert "Rebuilding FTS index" not in out
        assert "FTS index rebuilt" not in out
        assert "quieted" in err


class TestCmdIngestAlreadyRunning:
    """A second concurrent ingest skips: correct outcome, not an error.

    Cron schedules that fire two ingests on the same minute are what corrupted
    the bookkeeping in the first place (kgruel/siftd#29), so the overlap must
    stay quiet enough that nobody silences the job to make noise stop.
    """

    @staticmethod
    def _args(db_path, extra):
        return ["--db", str(db_path), "ingest", "--adapter", "claude_code",
                "--path", "/nonexistent/path", *extra]

    def test_reports_and_exits_zero(self, tmp_path, capsys, monkeypatch):
        from siftd.api import ingest as ingest_api

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        db_path = tmp_path / "test.db"
        with ingest_api._ingest_lock(db_path) as held:
            assert held is True
            rc = main(self._args(db_path, []))

        assert rc == 0
        assert "Another ingest is already running" in capsys.readouterr().err

    def test_quiet_says_nothing(self, tmp_path, capsys, monkeypatch):
        from siftd.api import ingest as ingest_api

        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        db_path = tmp_path / "test.db"
        with ingest_api._ingest_lock(db_path):
            rc = main(self._args(db_path, ["--quiet"]))

        assert rc == 0
        out, err = capsys.readouterr()
        assert "already running" not in out + err

    def test_scoped_run_says_so_even_when_auto_quieted(self, tmp_path, capsys, monkeypatch):
        """A scoped ingest that did nothing must say so on a pipe.

        The lock is per-database — never per-adapter or per-path — so a run that
        asked for specific work did none of it. Auto-quiet fires on any non-TTY,
        which is every ``siftd ingest --path … && siftd search …`` in a script,
        and swallowing the notice there makes the chain look like it worked.
        """
        from siftd.api import ingest as ingest_api

        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        db_path = tmp_path / "test.db"
        with ingest_api._ingest_lock(db_path):
            rc = main(self._args(db_path, []))

        assert rc == 0
        err = capsys.readouterr().err
        assert "Another ingest is already running" in err
        assert "were not ingested" in err

    def test_json_mode_emits_a_skipped_event(self, tmp_path, capsys, monkeypatch):
        from siftd.api import ingest as ingest_api

        db_path = tmp_path / "test.db"
        with ingest_api._ingest_lock(db_path):
            rc = main(self._args(db_path, ["--json"]))

        assert rc == 0
        events = [
            json.loads(line)
            for line in capsys.readouterr().out.strip().split("\n")
            if line.strip()
        ]
        assert {"type": "skipped", "reason": "locked"}.items() <= next(
            e.items() for e in events if e["type"] == "skipped"
        )


# ---------------------------------------------------------------------------
# cmd_ingest — TTY / auto-quiet behavior
# ---------------------------------------------------------------------------


class TestCmdIngestQuietDefaults:
    """Auto-quiet: quiet by default when not a TTY; verbose by default when TTY."""

    def _run(self, tmp_path, extra_args, monkeypatch, isatty):
        monkeypatch.setattr("sys.stdout.isatty", lambda: isatty)
        db_path = tmp_path / "test.db"
        return main([
            "--db", str(db_path),
            "ingest",
            "--adapter", "claude_code",
            "--path", "/nonexistent/path",
            *extra_args,
        ])

    def test_piped_auto_quiet_suppresses_progress(self, tmp_path, capsys, monkeypatch):
        """When not a TTY and no flags, per-file output is suppressed."""
        rc = self._run(tmp_path, [], monkeypatch, isatty=False)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Creating database" not in out
        assert "Ingesting" not in out

    def test_piped_auto_quiet_emits_hint_to_stderr(self, tmp_path, capsys, monkeypatch):
        """Auto-quiet emits a one-time behavior-change hint to stderr."""
        rc = self._run(tmp_path, [], monkeypatch, isatty=False)
        assert rc == 0
        err = capsys.readouterr().err
        assert "quieted" in err
        assert "-v" in err

    def test_tty_verbose_by_default(self, tmp_path, capsys, monkeypatch):
        """When stdout is a TTY, verbose behavior is unchanged (no hint)."""
        rc = self._run(tmp_path, [], monkeypatch, isatty=True)
        assert rc == 0
        out, err = capsys.readouterr()
        assert "Creating database" in out
        assert "Ingesting" in out
        assert "quieted" not in err

    def test_explicit_quiet_suppresses_hint(self, tmp_path, capsys, monkeypatch):
        """Explicit -q: quiet mode active, but no auto-quiet hint."""
        rc = self._run(tmp_path, ["-q"], monkeypatch, isatty=False)
        assert rc == 0
        err = capsys.readouterr().err
        assert "quieted" not in err

    def test_explicit_verbose_overrides_auto_quiet(self, tmp_path, capsys, monkeypatch):
        """Explicit -v: verbose output even when piped; no hint."""
        rc = self._run(tmp_path, ["-v"], monkeypatch, isatty=False)
        assert rc == 0
        out, err = capsys.readouterr()
        assert "Creating database" in out
        assert "Ingesting" in out
        assert "quieted" not in err


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
        assert "Filtered" in out

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
        assert "Total workspaces" in out

    def test_migrate_missing_db(self, tmp_path, capsys):
        """Migrate with missing database returns error."""
        rc = main(["--db", str(tmp_path / "missing.db"), "migrate"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err.lower() or "Database" in err

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
        err = capsys.readouterr().err
        assert err.strip()  # an error callout was reported to stderr

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
        assert "Copied adapters" in out

    def test_copy_nonexistent_adapter(self, tmp_path, monkeypatch, capsys):
        """siftd copy adapter <bad-name> returns error."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        rc = main(["copy", "adapter", "nonexistent_xyz"])
        assert rc == 1
        err = capsys.readouterr().err
        assert err.strip()  # an error callout was reported to stderr


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
        """siftd doctor --json returns structured findings.

        Every finding includes a "target" key (None for query-scope
        findings; entity id for row-scope). Additive schema check —
        guards against accidental removal of the target field.
        """
        rc = main(["--db", str(test_db), "doctor", "--json"])
        assert rc in (0, 1)
        data = json.loads(capsys.readouterr().out)
        assert "findings" in data
        assert "summary" in data
        assert isinstance(data["findings"], list)
        for f in data["findings"]:
            assert "target" in f, f

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

    @pytest.mark.slow
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
        """siftd doctor fix --pending-tags on a clean DB reports nothing to do."""
        rc = main(["--db", str(test_db), "doctor", "fix", "--pending-tags"])
        assert rc == 0
        captured = capsys.readouterr()
        text = (captured.out + captured.err).lower()
        assert "no pending tags to apply" in text

    def test_doctor_fix_pending_tags_json(self, test_db, capsys):
        """siftd doctor fix --pending-tags --json returns the recovery shape."""
        rc = main(["--db", str(test_db), "doctor", "fix", "--pending-tags", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data == {
            "applied": [],
            "unresolved": [],
            "discarded": [],
            "stale_sessions_pruned": 0,
        }

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


class TestDoctorFixPendingTagsRecovery:
    """`doctor fix --pending-tags` applies queued tags; it never deletes silently.

    The queue is keyed by the bare harness session id while conversations
    carry the adapter-prefixed `external_id`, and a settled session never
    re-ingests — so this fix is the only path that ever applies those rows.
    Everything here drives the argparse layer, because the `--pending-tags`
    / `--discard-unresolved` pairing is parse-time behavior.
    """

    @pytest.fixture
    def pending_db(self, tmp_path):
        """A DB with ingested conversations and a queue of unapplied tags."""
        from siftd.storage.sessions import queue_tag
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_model,
            insert_conversation,
            insert_prompt,
            insert_response,
        )

        db_path = tmp_path / "pending.db"
        conn = create_database(db_path)
        harness_id = get_or_create_harness(conn, "live_test", source="test", log_format="jsonl")
        model_id = get_or_create_model(conn, "claude-3-opus-20240229")

        ids = {}
        # Parent + subagent rows for the same session: the tag must land on
        # the parent, which is what the ingest drain would have done.
        ids["parent"] = insert_conversation(
            conn, external_id="live_test::SESSION-A", harness_id=harness_id,
            workspace_id=None, started_at="2024-01-15T10:00:00Z",
        )
        ids["subagent"] = insert_conversation(
            conn, external_id="live_test::SESSION-A::agent::sub1", harness_id=harness_id,
            workspace_id=None, started_at="2024-01-15T10:05:00Z",
        )
        # A session with events, for the late-bound marker path.
        ids["marked"] = insert_conversation(
            conn, external_id="live_test::SESSION-B", harness_id=harness_id,
            workspace_id=None, started_at="2024-01-16T10:00:00Z",
        )
        prompt_id = insert_prompt(conn, ids["marked"], "p1", "2024-01-16T10:00:00Z")
        ids["response"] = insert_response(
            conn, ids["marked"], prompt_id, model_id, None, "r1", "2024-01-16T10:00:01Z",
        )
        # A subagent-only session: nothing to tag at the parent level.
        ids["orphan_subagent"] = insert_conversation(
            conn, external_id="live_test::SESSION-C::agent::sub9", harness_id=harness_id,
            workspace_id=None, started_at="2024-01-17T10:00:00Z",
        )

        queue_tag(conn, "SESSION-A", "keeper")
        queue_tag(conn, "SESSION-B", "on-last-response", last_marker="last_response")
        # Session resolves, target does not: SESSION-B ran no tool. A later
        # ingest may still land it, so it is neither fixable nor discardable.
        queue_tag(conn, "SESSION-B", "on-last-tool-call", last_marker="last_tool_call")
        queue_tag(conn, "SESSION-C", "subagent-only")
        queue_tag(conn, "01KZKF9APH6N", "typo-key")  # resolves to nothing
        conn.commit()
        conn.close()
        return db_path, ids

    @staticmethod
    def _assignments(db_path):
        from siftd.storage.sqlite import open_database

        conn = open_database(db_path, read_only=True)
        try:
            return {
                (r["name"], r["target_kind"], r["target_id"])
                for r in conn.execute(
                    "SELECT t.name, a.target_kind, a.target_id "
                    "FROM tag_assignments a JOIN tags t ON t.id = a.tag_id"
                ).fetchall()
            }
        finally:
            conn.close()

    @staticmethod
    def _queued(db_path):
        from siftd.storage.sqlite import open_database

        conn = open_database(db_path, read_only=True)
        try:
            return {
                (r["harness_session_id"], r["tag_name"])
                for r in conn.execute(
                    "SELECT harness_session_id, tag_name FROM pending_tags"
                ).fetchall()
            }
        finally:
            conn.close()

    def test_applies_to_the_parent_conversation(self, pending_db, capsys):
        """A bare session key resolves through the adapter prefix, skipping subagents."""
        db_path, ids = pending_db
        rc = main(["--db", str(db_path), "doctor", "fix", "--pending-tags"])
        assert rc == 0

        assignments = self._assignments(db_path)
        assert ("keeper", "conversation", ids["parent"]) in assignments
        assert ("keeper", "conversation", ids["subagent"]) not in assignments
        # Applied rows are consumed; nothing else is.
        assert ("SESSION-A", "keeper") not in self._queued(db_path)

    def test_last_marker_resolves_to_the_event(self, pending_db, capsys):
        """Marker rows reuse the drain's resolution rather than degrading to the conversation."""
        db_path, ids = pending_db
        main(["--db", str(db_path), "doctor", "fix", "--pending-tags"])

        assignments = self._assignments(db_path)
        assert ("on-last-response", "response", ids["response"]) in assignments

    def test_unresolvable_rows_are_kept_and_named(self, pending_db, capsys):
        """No conversation → the row survives and its key is reported, not swallowed."""
        db_path, _ = pending_db
        rc = main(["--db", str(db_path), "doctor", "fix", "--pending-tags"])
        assert rc == 0

        queued = self._queued(db_path)
        assert ("01KZKF9APH6N", "typo-key") in queued
        # A session whose only conversation is a subagent stays queued too:
        # tagging the subagent would be the wrong target, not a partial win.
        assert ("SESSION-C", "subagent-only") in queued

        captured = capsys.readouterr()
        text = captured.out + captured.err
        assert "01KZKF9APH6N" in text
        assert "kept, not deleted" in text
        # The old wording ("cleaned up") described deletion as repair.
        assert "leaned up" not in text

    def test_discard_is_opt_in_and_says_discard(self, pending_db, capsys):
        """--discard-unresolved deletes the stranded rows and calls it discarding."""
        db_path, _ = pending_db
        rc = main([
            "--db", str(db_path), "doctor", "fix", "--pending-tags", "--discard-unresolved",
        ])
        assert rc == 0
        assert ("01KZKF9APH6N", "typo-key") not in self._queued(db_path)
        assert ("SESSION-C", "subagent-only") not in self._queued(db_path)

        captured = capsys.readouterr()
        assert "iscarded" in captured.out + captured.err

    def test_discard_spares_rows_still_waiting_on_a_target(self, pending_db, capsys):
        """The flag clears rows that match no conversation — not rows mid-flight.

        A row whose session resolved is one ingest away from landing; the next
        turn of the transcript may be the tool call it names. Sweeping it up
        with the genuinely stranded rows would be losing a live tag under a
        flag whose whole point is that the loss is deliberate.
        """
        db_path, _ = pending_db
        rc = main([
            "--db", str(db_path), "doctor", "fix", "--pending-tags", "--discard-unresolved",
        ])
        assert rc == 0
        assert ("SESSION-B", "on-last-tool-call") in self._queued(db_path)

    def test_a_discarded_row_is_reported_once_as_discarded(self, pending_db, capsys):
        """A deleted row is never also listed as kept, in either channel.

        `unresolved` used to keep the rows the discard had just deleted, so
        both renderers printed them under "kept, not deleted" alongside the
        discard count, and `--json` offered them as still-queued work. The two
        lists partition what was not applied, so a row lands in exactly one.
        """
        db_path, _ = pending_db
        rc = main([
            "--db", str(db_path), "doctor", "fix", "--pending-tags",
            "--discard-unresolved", "--json",
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)

        discarded = {u["session"] for u in data["discarded"]}
        unresolved = {u["session"] for u in data["unresolved"]}
        assert "01KZKF9APH6N" in discarded
        assert discarded & unresolved == set()
        # The survivor is the target-pending row, and it is listed as kept.
        assert unresolved == {"SESSION-B"}
        # The discarded entries carry the same detail as the kept ones.
        assert all(u["reason"] and u["kind"] == "session-unresolvable" for u in data["discarded"])

    def test_text_channel_never_lists_a_discarded_row_as_kept(self, pending_db, capsys):
        """Same partition, in the human channel."""
        db_path, _ = pending_db
        rc = main([
            "--db", str(db_path), "doctor", "fix", "--pending-tags", "--discard-unresolved",
        ])
        assert rc == 0
        text = capsys.readouterr()
        out = text.out + text.err

        assert "Discarded 2 pending tag(s)" in out
        # Named, because a delete is the outcome most worth being able to
        # chase afterwards.
        assert "01KZKF9APH6N" in out
        assert out.count("01KZKF9APH6N") == 1
        # The kept-rows heading describes only what survived.
        kept_heading = "match no ingested conversation — kept, not deleted"
        assert kept_heading not in out

    def test_existing_assignment_counts_as_applied(self, pending_db, capsys):
        """A hand-recovered tag makes its queue row satisfied, not failed."""
        from siftd.storage.sqlite import open_database
        from siftd.storage.tags import apply_tag, get_or_create_tag

        db_path, ids = pending_db
        conn = open_database(db_path)
        apply_tag(conn, "conversation", ids["parent"], get_or_create_tag(conn, "keeper"))
        conn.commit()
        conn.close()

        rc = main(["--db", str(db_path), "doctor", "fix", "--pending-tags", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        applied = {a["tag"]: a for a in data["applied"]}
        assert applied["keeper"]["already_present"] is True
        assert applied["keeper"]["target_id"] == ids["parent"]
        assert ("SESSION-A", "keeper") not in self._queued(db_path)

    def test_registered_session_is_left_alone(self, pending_db, capsys):
        """A live session's queue still belongs to the ingest drain."""
        from siftd.storage.sessions import register_session
        from siftd.storage.sqlite import open_database

        db_path, ids = pending_db
        conn = open_database(db_path)
        register_session(conn, "SESSION-A", "live_test")
        conn.commit()
        conn.close()

        main(["--db", str(db_path), "doctor", "fix", "--pending-tags"])

        assert ("SESSION-A", "keeper") in self._queued(db_path)
        assert ("keeper", "conversation", ids["parent"]) not in self._assignments(db_path)

    def test_prefixed_registration_shields_bare_queued_rows(self, pending_db, capsys):
        """A live session is protected whichever key form registered it.

        The shipped session-start hook registers `<adapter>::<uuid>` while
        `siftd tag --session <uuid>` queues the bare uuid, so an exact-key
        orphan scope declared a still-live session abandoned — and recovery
        then resolved its `--last-*` markers against a half-written
        transcript, pinning the tag to a non-final turn.
        """
        from siftd.storage.sessions import register_session
        from siftd.storage.sqlite import open_database

        db_path, ids = pending_db
        conn = open_database(db_path)
        register_session(conn, "live_test::SESSION-A", "live_test")
        conn.commit()
        conn.close()

        main(["--db", str(db_path), "doctor", "fix", "--pending-tags"])

        assert ("SESSION-A", "keeper") in self._queued(db_path)
        assert ("keeper", "conversation", ids["parent"]) not in self._assignments(db_path)

    def test_bare_registration_shields_prefixed_queued_rows(self, pending_db, capsys):
        """The same, the other way round: either side may carry the prefix."""
        from siftd.storage.sessions import queue_tag, register_session
        from siftd.storage.sqlite import open_database

        db_path, ids = pending_db
        conn = open_database(db_path)
        queue_tag(conn, "live_test::SESSION-A", "prefixed-row")
        register_session(conn, "SESSION-A", "live_test")
        conn.commit()
        conn.close()

        main(["--db", str(db_path), "doctor", "fix", "--pending-tags"])

        assert ("live_test::SESSION-A", "prefixed-row") in self._queued(db_path)
        assert ("prefixed-row", "conversation", ids["parent"]) not in self._assignments(db_path)

    def test_json_reports_unresolved_keys(self, pending_db, capsys):
        """The machine channel carries the full unresolved list, with reasons."""
        db_path, _ = pending_db
        rc = main(["--db", str(db_path), "doctor", "fix", "--pending-tags", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["discarded"] == []
        unresolved = {u["session"]: u["reason"] for u in data["unresolved"]}
        assert "01KZKF9APH6N" in unresolved
        assert unresolved["01KZKF9APH6N"]

    def test_stale_registration_is_pruned_then_recovered(self, pending_db, capsys):
        """The advertised prune → in-scope → applied sequence, end to end.

        `recover_pending_tags` prunes stale registrations first *so that* their
        queued tags come into scope, and the check advertises it ("idle for
        over 48 hours — the fix prunes them"). Only the fresh-session negative
        case was covered, so the DELETE was never exercised against a real row.
        """
        from datetime import UTC, datetime, timedelta

        from siftd.storage.sessions import register_session
        from siftd.storage.sqlite import open_database

        db_path, ids = pending_db
        conn = open_database(db_path)
        register_session(conn, "SESSION-A", "live_test")
        old = (datetime.now(UTC) - timedelta(hours=200)).isoformat()
        conn.execute(
            "UPDATE active_sessions SET last_seen_at = ?, started_at = ? "
            "WHERE harness_session_id = ?",
            (old, old, "SESSION-A"),
        )
        conn.commit()
        conn.close()

        rc = main(["--db", str(db_path), "doctor", "fix", "--pending-tags", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["stale_sessions_pruned"] == 1
        applied = {a["tag"]: a for a in data["applied"]}
        assert applied["keeper"]["target_id"] == ids["parent"]

    def test_exchange_rows_resolve_by_index(self, pending_db, capsys):
        """`tag --session <id> --exchange N` rows recover too, in range and past it."""
        from siftd.storage.sessions import queue_tag
        from siftd.storage.sqlite import open_database

        db_path, ids = pending_db
        conn = open_database(db_path)
        prompt_id = conn.execute(
            "SELECT id FROM events WHERE conversation_id = ? AND kind = 'prompt'",
            (ids["marked"],),
        ).fetchone()["id"]
        queue_tag(conn, "SESSION-B", "on-exchange-1", entity_type="exchange", exchange_index=1)
        queue_tag(conn, "SESSION-B", "past-end", entity_type="exchange", exchange_index=9)
        conn.commit()
        conn.close()

        rc = main(["--db", str(db_path), "doctor", "fix", "--pending-tags", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)

        applied = {a["tag"]: a for a in data["applied"]}
        assert applied["on-exchange-1"]["target_kind"] == "exchange"
        assert applied["on-exchange-1"]["target_id"] == prompt_id
        # Past the end resolves to nothing, so the row is kept, not discarded.
        unresolved = {u["tag"]: u["reason"] for u in data["unresolved"]}
        assert "exchange 9" in unresolved["past-end"]
        assert ("SESSION-B", "past-end") in self._queued(db_path)

    def test_doctor_converges_after_the_fix(self, pending_db, capsys):
        """A converged DB reports info, not a warning `--strict` can never clear.

        Both kept buckets are kept by design, so counting either as an
        actionable warning left `doctor --strict` (documented for CI)
        permanently at exit 1 unless the user ran the destructive discard.
        """
        db_path, _ = pending_db
        main(["--db", str(db_path), "doctor", "fix", "--pending-tags"])
        capsys.readouterr()

        rc = main(["--db", str(db_path), "doctor", "run", "pending-tags", "--strict",
                   "--json"])
        report = json.loads(capsys.readouterr().out)
        findings = [f for f in report["findings"] if f["check"] == "pending-tags"]
        severities = {f["severity"] for f in findings}
        assert "warning" not in severities
        assert "info" in severities  # the kept rows are still reported
        assert rc == 0

        # The target-pending residue is reported honestly: not fixable now,
        # but not a dead end either.
        waiting = [f for f in findings if f["context"].get("target_pending_count")]
        assert len(waiting) == 1
        assert waiting[0]["context"]["target_pending_count"] == 1
        assert waiting[0]["fix_available"] is False
        assert "may resolve after further ingest" in waiting[0]["message"]

    def test_check_buckets_match_the_fix_outcome(self, pending_db):
        """Every row the check counts as recoverable is a row the fix applies.

        The check advertises a fix and the fix decides what to apply; when the
        two classify differently, `doctor --strict` either stays red on rows
        nothing can move or goes quiet on rows that needed attention. Assert
        the correspondence over all three buckets rather than trusting that
        two call sites of the same resolvers stay in step.
        """
        from siftd.api.sessions import recover_pending_tags
        from siftd.storage.sessions import count_orphaned_pending_tags
        from siftd.storage.sqlite import open_database

        db_path, _ = pending_db
        conn = open_database(db_path)
        counts = count_orphaned_pending_tags(conn)
        result = recover_pending_tags(conn, max_age_hours=48, commit=True)
        conn.close()

        kept = Counter(u.kind for u in result.unresolved)
        assert (counts.recoverable, counts.target_pending, counts.session_unresolvable) == (
            len(result.applied),
            kept["target-pending"],
            kept["session-unresolvable"],
        )
        # And the fixture really does exercise all three, or the assertion
        # above is satisfied by zeros.
        assert min(counts.recoverable, counts.target_pending, counts.session_unresolvable) > 0

    def test_discard_unresolved_without_pending_tags_is_reported(self, test_db, tmp_path, monkeypatch, capsys):
        """The destructive-sounding flag never applies silently to another fix."""
        # Keep the findings cache (read and cleared by the batch fix path)
        # inside the test's own XDG state.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        main(["--db", str(test_db), "doctor", "fix", "--discard-unresolved"])
        captured = capsys.readouterr()
        assert "--discard-unresolved ignored" in captured.out + captured.err


class TestDataDirectBranches:
    def test_run_fix_steps_plain_log_and_error_count(self, capsys):
        # Non-TTY (capsys): the dissolved spinner prints a plain step-log and
        # returns the error count. A failing step is reported, not raised.
        def boom(conn, db):
            raise RuntimeError("nope")

        steps = [("Good", lambda conn, db: "did 3"), ("Bad", boom)]
        errors, not_applied = data_cli._run_fix_steps(steps, conn=None, db=None)
        assert (errors, not_applied) == (1, 0)
        out = capsys.readouterr().out
        assert "Good: did 3" in out
        assert "Bad: nope" in out

    def test_run_fix_steps_active_paints_spinner_log(self, monkeypatch, capsys):
        # Active TTY: the dissolved spinner paints through the real InPlaceRenderer
        # (pending → resolved in place) rather than the plain per-step print.
        monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: True)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)

        def boom(conn, db):
            raise RuntimeError("nope")

        errors, not_applied = data_cli._run_fix_steps(
            [("Good", lambda conn, db: "ok"), ("Bad", boom)], conn=None, db=None
        )
        assert (errors, not_applied) == (1, 0)
        out = capsys.readouterr().out
        assert "Good: ok" in out and "Bad: nope" in out
        # A live frame was painted (spinner and/or resolved glyph), not a plain log.
        assert "⠋" in out or "✓" in out

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
        monkeypatch.setattr(
            "siftd.api.run_ingest",
            lambda db_path, on_notice=None: SimpleNamespace(
                skipped_locked=False,
                stats=SimpleNamespace(files_ingested=1, files_skipped=2),
            ),
        )
        assert "1 file" in data_cli._fix_ingest(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.search.rebuild_fts_index", lambda conn: None)
        assert "FTS index rebuilt" in data_cli._fix_rebuild_fts(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.search.build_index", lambda **k: {"chunks_added": 3})
        assert "3 chunk" in data_cli._fix_embed(object(), Path("/d"))
        assert "3 chunk" in data_cli._fix_embed_rebuild(object(), Path("/d"))

        monkeypatch.setattr("siftd.api.migrations.backfill_git_remotes", lambda conn: {"updated": 5})
        assert "5 workspace" in data_cli._fix_backfill_git_remote(object(), Path("/d"))

        # The batch path applies and reports; it never discards.
        from siftd.storage.sessions import (
            AppliedPendingTag,
            PendingTagRecovery,
            UnresolvedPendingTag,
        )

        def _fake_recover(conn, **kwargs):
            assert kwargs.get("discard_unresolved", False) is False
            return PendingTagRecovery(
                applied=[AppliedPendingTag("s", "t", "conversation", "c", False)] * 7,
                unresolved=(
                    [UnresolvedPendingTag("s2", "t2", "why", "session-unresolvable")] * 8
                    + [UnresolvedPendingTag("s3", "t3", "no target yet", "target-pending")] * 3
                ),
                discarded=[],
                stale_sessions_pruned=2,
            )

        monkeypatch.setattr("siftd.api.sessions.recover_pending_tags", _fake_recover)
        summary = data_cli._fix_pending_tags(object(), Path("/d"))
        assert "7 tag(s) applied" in summary
        # The batch summary splits the two kept buckets: they call for
        # different next steps, and only one of them is ever discardable.
        assert "3 awaiting a target (kept)" in summary
        assert "8 matching no conversation (kept)" in summary

    def test_doctor_fix_dispatches_embed_through_registry(self, test_db, monkeypatch, capsys):
        """Integration: a cached 'siftd embed' finding resolves through _FIX_REGISTRY and
        actually invokes the embed fixer (not just the function tested in isolation)."""
        monkeypatch.setattr(
            "siftd.doctor.fixes.load_findings_cache",
            lambda: [{"fix_command": "siftd embed", "check": "embeddings_stale", "message": "x"}],
        )
        monkeypatch.setattr("siftd.doctor.fixes.clear_findings_cache", lambda: None)

        called = {}

        def fake_build_index(**kwargs):
            called.update(kwargs)
            return {"chunks_added": 4}

        monkeypatch.setattr("siftd.api.search.build_index", fake_build_index)

        rc = data_cli._doctor_fix(SimpleNamespace(db=str(test_db)))
        assert rc == 0
        assert called, "the 'siftd embed' fixer must run through _FIX_REGISTRY"
        assert called.get("rebuild") is False
        assert Path(called["db_path"]) == Path(test_db)

    def test_doctor_fix_does_not_claim_a_locked_out_ingest_was_applied(
        self, test_db, monkeypatch, capsys
    ):
        """A fix that declined to run is still pending.

        ``doctor fix`` used to mark a lock-out ✓, print "All fixes applied.",
        exit 0 AND clear the findings cache — discarding a finding nothing had
        fixed. The step has to be able to say "not applied".
        """
        monkeypatch.setattr(
            "siftd.doctor.fixes.load_findings_cache",
            lambda: [{"fix_command": "siftd ingest", "check": "ingest_pending", "message": "x"}],
        )
        cleared = []
        monkeypatch.setattr(
            "siftd.doctor.fixes.clear_findings_cache", lambda: cleared.append(True)
        )
        monkeypatch.setattr(
            "siftd.api.run_ingest",
            lambda **kwargs: SimpleNamespace(skipped_locked=True, stats=None),
        )

        rc = data_cli._doctor_fix(SimpleNamespace(db=str(test_db)))

        out = capsys.readouterr()
        combined = out.out + out.err
        assert rc == 0
        assert "not applied" in combined
        assert "All fixes applied" not in combined
        assert not cleared, "a pending fix must stay in the cache"

    def test_fix_ingest_forwards_egress_notice(self, monkeypatch, capsys):
        """_fix_ingest passes on_notice to run_ingest so the remote first-egress disclosure
        prints live, BEFORE content leaves — without it the notice lands on a discarded
        result (F3'). Focused check: on_notice is a callable that prints via status."""
        captured = {}

        def fake_run_ingest(*, db_path, on_notice=None):
            captured["on_notice"] = on_notice
            if on_notice is not None:
                on_notice("Uploading conversation content to remote:openai for the first time.")
            return SimpleNamespace(
                skipped_locked=False,
                stats=SimpleNamespace(files_ingested=0, files_skipped=0),
            )

        monkeypatch.setattr("siftd.api.run_ingest", fake_run_ingest)
        data_cli._fix_ingest(object(), Path("/d"))

        assert callable(captured["on_notice"])
        # status.info writes to stderr.
        assert "Uploading conversation content" in capsys.readouterr().err

    def test_doctor_run_json_plain_and_painted_error_paths(self, test_db, monkeypatch):
        args = SimpleNamespace(db=str(test_db), json=False, strict=False)

        monkeypatch.setattr("siftd.api.run_checks", lambda **k: (_ for _ in ()).throw(FileNotFoundError("missing")))
        assert data_cli._doctor_run_json(args, None, False, Path(test_db)) == 1
        assert data_cli._doctor_run_plain(args, None, False, Path(test_db)) == 1

        monkeypatch.setattr("siftd.api.run_checks", lambda **k: (_ for _ in ()).throw(ValueError("bad")))
        assert data_cli._doctor_run_json(args, None, False, Path(test_db)) == 1
        assert data_cli._doctor_run_plain(args, None, False, Path(test_db)) == 1

        # The painted path with a non-TTY stdout: LiveRegion is inactive, so it
        # paints nothing and just returns the exit code (run_checks → no findings
        # → 0). The active render path is exercised separately below.
        monkeypatch.setattr("siftd.api.list_checks", lambda: [SimpleNamespace(name="c1")])
        monkeypatch.setattr("siftd.api.run_checks", lambda **k: [])
        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda findings: None)
        assert data_cli._doctor_run_painted(args, ["c1"], False, Path(test_db)) == 0

    def test_doctor_run_painted_active_paints_panel_and_report(self, monkeypatch):
        """The active live path: a real LiveRegion + the real view renderers paint
        to a fake TTY. on_check_done drives the panel frames; finalize deposits the
        settled findings report (the check name + tally land in the stream)."""
        from siftd.doctor.checks import Finding

        monkeypatch.setattr("siftd.output.live.supports_unicode", lambda: True)
        stream = _FakeTTY()
        monkeypatch.setattr("sys.stdout", stream)

        warning = Finding(
            check="ingest-errors", severity="warning",
            message="claude_code: 8 file(s) failed", fix_available=True,
            fix_command="siftd doctor --verbose",
        )

        def fake_run_checks(*, checks, db_path, deep, fast, on_check_done):
            on_check_done("schema-current", [])  # a clean check → passed
            on_check_done("ingest-errors", [warning])  # an issue
            return [warning]

        monkeypatch.setattr("siftd.api.run_checks", fake_run_checks)
        monkeypatch.setattr(
            "siftd.api.list_checks",
            lambda: [SimpleNamespace(name="schema-current"), SimpleNamespace(name="ingest-errors")],
        )
        saved: list = []
        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda findings: saved.append(findings))

        args = SimpleNamespace(db=None, json=False, strict=False, no_hints=False)
        rc = data_cli._doctor_run_painted(args, None, False, None)

        assert rc == 0  # a warning, non-strict → does not fail
        out = stream.getvalue()
        assert "ingest-errors" in out  # the issue surfaced (panel + report)
        assert "passed" in out  # the report's severity tally (a report-only token)
        assert "1 passed" in out  # tally counts the clean check → the report deposited
        assert saved == [[warning]]  # the filtered findings were cached for `doctor fix`

    def test_doctor_run_painted_return_codes(self, monkeypatch):
        """The painted path's strict/error return contract (inactive stdout isolates
        the return logic from rendering): an error fails non-strict; a warning fails
        only under --strict. The full-stack strict test uses --json, a different path."""
        from siftd.doctor.checks import Finding

        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda findings: None)
        monkeypatch.setattr("siftd.api.list_checks", lambda: [SimpleNamespace(name="c1")])

        def run_returning(findings):
            def _run(*, checks, db_path, deep, fast, on_check_done):
                return findings
            return _run

        error = Finding(check="c1", severity="error", message="bad", fix_available=False)
        warning = Finding(check="c1", severity="warning", message="careful", fix_available=False)

        # error → fail even non-strict
        monkeypatch.setattr("siftd.api.run_checks", run_returning([error]))
        args = SimpleNamespace(db=None, json=False, strict=False, no_hints=False)
        assert data_cli._doctor_run_painted(args, None, False, None) == 1

        # warning → passes non-strict, fails under --strict
        monkeypatch.setattr("siftd.api.run_checks", run_returning([warning]))
        assert data_cli._doctor_run_painted(SimpleNamespace(db=None, json=False, strict=False, no_hints=False), None, False, None) == 0
        assert data_cli._doctor_run_painted(SimpleNamespace(db=None, json=False, strict=True, no_hints=False), None, False, None) == 1

    def test_migrate_merge_verbose_and_dry_run_outputs(self, test_db, monkeypatch, capsys):
        from siftd.domain.progress import ProgressEvent

        def _emit(on_progress, group, text):
            # The producers emit ProgressEvents (not raw strings) since the
            # progress contract; the verbose plain sink prints event.message.
            on_progress(ProgressEvent(group=group, message=text, status="progress"))

        monkeypatch.setattr(
            "siftd.api.migrations.backfill_git_remotes",
            lambda conn, on_progress, group, dry_run: (_emit(on_progress, group, "progress"), {"checked": 1, "updated": 1, "skipped_missing": 0, "skipped_no_git": 0})[1],
        )
        monkeypatch.setattr("siftd.api.migrations.verify_workspace_identity", lambda conn: {"duplicate_groups": 1, "duplicate_workspaces": 2, "total": 2, "with_remote": 1, "without_remote": 1})
        monkeypatch.setattr("siftd.api.migrations.merge_duplicate_workspaces", lambda conn, on_progress, group, dry_run: (_emit(on_progress, group, "merging"), {"workspaces_merged": 1, "conversations_moved": 2})[1])
        rc = main(["--db", str(test_db), "migrate", "--merge-workspaces", "--dry-run", "-v"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "progress" in out and "Would merge" in out

        monkeypatch.setattr("siftd.api.migrations.verify_workspace_identity", lambda conn: {"duplicate_groups": 0, "duplicate_workspaces": 0, "total": 1, "with_remote": 1, "without_remote": 0})
        rc = main(["--db", str(test_db), "migrate", "--merge-workspaces"])
        assert rc == 0

    def test_ingest_and_backfill_remaining_branches(self, test_db, monkeypatch, capsys):
        # cmd_ingest: unmatched adapter in json mode
        monkeypatch.setattr("siftd.api.ingest.load_all_adapters", lambda **_kw: [])
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)
        rc = main(["--db", str(test_db), "ingest", "--json", "--adapter", "nope"])
        assert rc == 1

        # cmd_backfill: shell-tags, derivative-tags, and filter-binary error/notice prints
        monkeypatch.setattr("siftd.api.backfill.backfill_shell_tags", lambda conn: {"git": 2})
        rc = main(["--db", str(test_db), "backfill", "--shell-tags"])
        assert rc == 0

        monkeypatch.setattr("siftd.api.backfill.backfill_derivative_tags", lambda conn: 1)
        rc = main(["--db", str(test_db), "backfill", "--derivative-tags"])
        assert rc == 0

        monkeypatch.setattr(
            "siftd.api.backfill.backfill_filter_binary",
            lambda conn, dry_run=False: {"filtered": 1, "skipped": 0, "errors": 2},
        )
        rc = main(["--db", str(test_db), "backfill", "--filter-binary", "--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Errors" in captured.out  # the count breakdown stays on stdout
        assert "Run without --dry-run" in captured.err  # the hint is status -> stderr

    def test_copy_all_and_error_paths(self, monkeypatch):
        class _CopyErr(Exception):
            pass

        monkeypatch.setattr("siftd.api.CopyError", _CopyErr)

        monkeypatch.setattr("siftd.api.list_builtin_adapters", lambda: [])
        assert data_cli.cmd_copy(SimpleNamespace(resource_type="adapter", name=None, force=False, all=True)) == 1

        monkeypatch.setattr("siftd.api.list_builtin_adapters", lambda: ["a1", "a2"])
        monkeypatch.setattr("siftd.api.copy_adapter", lambda n, force=False: (_ for _ in ()).throw(_CopyErr("boom")) if n == "a2" else f"/tmp/{n}")
        assert data_cli.cmd_copy(SimpleNamespace(resource_type="adapter", name=None, force=False, all=True)) == 0

        monkeypatch.setattr("siftd.api.list_builtin_queries", lambda: ["q1", "q2"])
        monkeypatch.setattr("siftd.api.copy_query", lambda n, force=False: (_ for _ in ()).throw(_CopyErr("bad")) if n == "q2" else f"/tmp/{n}")
        assert data_cli.cmd_copy(SimpleNamespace(resource_type="query", name=None, force=False, all=True)) == 0

        monkeypatch.setattr("siftd.api.list_builtin_queries", lambda: [])
        assert data_cli.cmd_copy(SimpleNamespace(resource_type="query", name=None, force=False, all=False)) == 1

        monkeypatch.setattr("siftd.api.copy_query", lambda n, force=False: (_ for _ in ()).throw(_CopyErr("nope")))
        assert data_cli.cmd_copy(SimpleNamespace(resource_type="query", name="q", force=False, all=False)) == 1

        monkeypatch.setattr("siftd.api.list_builtin_formatters", lambda: ["f1", "f2"])
        monkeypatch.setattr("siftd.api.copy_formatter", lambda n, force=False: (_ for _ in ()).throw(_CopyErr("bad")) if n == "f2" else f"/tmp/{n}")
        assert data_cli.cmd_copy(SimpleNamespace(resource_type="formatter", name=None, force=False, all=True)) == 0

        monkeypatch.setattr("siftd.api.copy_formatter", lambda n, force=False: (_ for _ in ()).throw(_CopyErr("bad")))
        assert data_cli.cmd_copy(SimpleNamespace(resource_type="formatter", name="f1", force=False, all=False)) == 1

    def test_renderer_and_cmd_doctor_remaining_edges(self, test_db, monkeypatch, capsys):
        # lines 68/236: total update branch
        tr = _IngestTextRenderer(verbose=True)
        tr.handle_event(FakeEvent(index=1, total=1))
        tr.handle_event(FakeEvent(index=2, total=2, status="error", error="oops"))
        jr = _IngestJsonRenderer()
        jr.handle_event(FakeEvent(index=1, total=1))
        jr.handle_event(FakeEvent(index=2, total=2))

        # print_summary verbose reasons + error column
        c = _AdapterCounts(total=2)
        c.add("ingested", None)
        c.add("error", None)
        c.add("skipped", "unchanged")
        tr._counts["a"] = c
        tr.print_summary(FakeStats(conversations=1, prompts=1, responses=1, tool_calls=1))

        # ingest stats cache exception branch
        monkeypatch.setattr(
            "siftd.api.ingest.load_all_adapters",
            lambda **_kw: [SimpleNamespace(name="ok", module="m")],
        )
        monkeypatch.setattr("siftd.api.ingest.wrap_adapter_paths", lambda m, p: m)
        monkeypatch.setattr(
            "siftd.api.ingest.ingest_all",
            lambda conn, adapters, on_event=None, filter_binary=None: FakeStats(),
        )
        monkeypatch.setattr("siftd.api.stats.get_stats", lambda **k: {})
        monkeypatch.setattr("siftd.api.stats.write_stats_cache", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)
        rc = main(["--db", str(test_db), "ingest", "--json", "--adapter", "ok"])
        assert rc == 0

        # cmd_doctor legacy aliases
        assert main(["--db", str(test_db), "doctor", "fixes"]) in (0, 1)
        assert main(["--db", str(test_db), "doctor", "some-check-name"]) in (0, 1)

    def test_doctor_fix_and_run_extra_branches(self, test_db, monkeypatch):
        # _doctor_fix: missing db
        monkeypatch.setattr("siftd.doctor.fixes.load_findings_cache", lambda: [{"fix_command": "siftd ingest"}])
        assert data_cli._doctor_fix(SimpleNamespace(db=str(Path(test_db).with_name("missing.db")))) == 1

        # _doctor_fix: action raises -> error summary branch
        monkeypatch.setattr("siftd.api.open_database", lambda *_a, **_k: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr("siftd.doctor.fixes.clear_findings_cache", lambda: None)
        monkeypatch.setattr("siftd.doctor.fixes.load_findings_cache", lambda: [{"fix_command": "siftd ingest"}])
        monkeypatch.setitem(data_cli._FIX_REGISTRY, "siftd ingest", ("Ingest", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("bad"))))
        assert data_cli._doctor_fix(SimpleNamespace(db=str(test_db))) == 1

        # _doctor_run_plain no findings and show_fixes list
        monkeypatch.setattr("siftd.api.run_checks", lambda **k: [])
        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda findings: None)
        assert data_cli._doctor_run_plain(SimpleNamespace(strict=False), None, False, Path(test_db)) == 0

        f = SimpleNamespace(severity="warning", check="c", message="m", fix_available=True, fix_command="siftd ingest", context={}, channel="both")
        monkeypatch.setattr("siftd.api.run_checks", lambda **k: [f])
        assert data_cli._doctor_run_plain(SimpleNamespace(strict=False), None, True, Path(test_db)) == 0

    def test_doctor_run_router_tty_and_painted_exceptions(self, test_db, monkeypatch):
        # _doctor_run should choose painted on tty
        real_painted = data_cli._doctor_run_painted
        real_stdout = data_cli.sys.stdout
        monkeypatch.setattr("siftd.cli.data._doctor_run_painted", lambda *a, **k: 7)

        class _Std:
            encoding = "utf-8"  # a Unicode-capable TTY routes to painted

            def isatty(self):
                return True

        monkeypatch.setattr(data_cli.sys, "stdout", _Std())
        assert data_cli._doctor_run(SimpleNamespace(db=str(test_db), json=False), None, False) == 7

        # An ASCII-only TTY (e.g. a non-UTF-8 locale) degrades to the plain path
        # rather than rendering garbled box-drawing glyphs.
        monkeypatch.setattr("siftd.cli.data._doctor_run_plain", lambda *a, **k: 9)

        class _AsciiStd:
            encoding = "ascii"

            def isatty(self):
                return True

        monkeypatch.setattr(data_cli.sys, "stdout", _AsciiStd())
        assert data_cli._doctor_run(SimpleNamespace(db=str(test_db), json=False), None, False) == 9

        monkeypatch.setattr("siftd.cli.data._doctor_run_painted", real_painted)
        monkeypatch.setattr(data_cli.sys, "stdout", real_stdout)

        # _doctor_run_painted: drive on_done and exception branches
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

        monkeypatch.setitem(__import__("sys").modules, "painted", SimpleNamespace(InPlaceRenderer=_R, use_theme=lambda *_a, **_k: _Theme()))
        monkeypatch.setitem(__import__("sys").modules, "siftd.output.theme", SimpleNamespace(siftd_theme=object()))
        monkeypatch.setattr("siftd.api.list_checks", lambda: [SimpleNamespace(name="c1")])
        monkeypatch.setattr("siftd.doctor.view.render_progress_block", lambda *_a, **_k: "blk")
        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda findings: None)

        def _run_checks_ok(**kwargs):
            kwargs["on_check_done"]("c1", [])
            return []

        monkeypatch.setattr("siftd.api.run_checks", _run_checks_ok)
        assert data_cli._doctor_run_painted(SimpleNamespace(strict=False), ["c1"], False, Path(test_db)) == 0

        monkeypatch.setattr("siftd.api.run_checks", lambda **k: (_ for _ in ()).throw(FileNotFoundError("missing")))
        assert data_cli._doctor_run_painted(SimpleNamespace(strict=False), ["c1"], False, Path(test_db)) == 1

        monkeypatch.setattr("siftd.api.run_checks", lambda **k: (_ for _ in ()).throw(ValueError("bad")))
        assert data_cli._doctor_run_painted(SimpleNamespace(strict=False), ["c1"], False, Path(test_db)) == 1

    def test_last_missing_branches(self, test_db, monkeypatch, capsys):
        # migrate merge non-dry-run summary lines (505/506)
        monkeypatch.setattr(
            "siftd.api.migrations.backfill_git_remotes",
            lambda conn, on_progress, group, dry_run: {"checked": 1, "updated": 1, "skipped_missing": 0, "skipped_no_git": 0},
        )
        monkeypatch.setattr(
            "siftd.api.migrations.verify_workspace_identity",
            lambda conn: {"duplicate_groups": 1, "duplicate_workspaces": 2, "total": 3, "with_remote": 2, "without_remote": 1},
        )
        monkeypatch.setattr(
            "siftd.api.migrations.merge_duplicate_workspaces",
            lambda conn, on_progress, group, dry_run: {"workspaces_merged": 2, "conversations_moved": 5},
        )
        rc = main(["--db", str(test_db), "migrate", "--merge-workspaces"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Merged 2 workspaces" in out and "Moved 5 conversations" in out

        # migrate status duplicate-groups hint lines (515/518)
        monkeypatch.setattr(
            "siftd.api.migrations.verify_workspace_identity",
            lambda conn: {"duplicate_groups": 2, "duplicate_workspaces": 4, "total": 6, "with_remote": 5, "without_remote": 1},
        )
        rc = main(["--db", str(test_db), "migrate"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Duplicate groups" in captured.out  # breakdown stays on stdout (now a gutter-aligned listing row)
        assert "--merge-workspaces" in captured.err  # the hint is status -> stderr

        # copy formatter usage listing lines (636-641)
        monkeypatch.setattr("siftd.api.list_builtin_formatters", lambda: ["markdown", "json"])
        rc = data_cli.cmd_copy(SimpleNamespace(resource_type="formatter", name=None, force=False, all=False))
        assert rc == 1
        out = capsys.readouterr().out
        assert "Usage: siftd copy formatter" in out and "markdown" in out

        # doctor fix pending tags non-json report: applied + pruned on stdout,
        # the kept-not-deleted warning on stderr.
        from siftd.storage.sessions import (
            AppliedPendingTag,
            PendingTagRecovery,
            UnresolvedPendingTag,
        )

        monkeypatch.setattr(
            "siftd.api.sessions.recover_pending_tags",
            lambda *_a, **_k: PendingTagRecovery(
                applied=[AppliedPendingTag("sess-1", "keeper", "conversation", "conv-1", False)],
                unresolved=[
                    UnresolvedPendingTag(
                        "sess-2", "lost", "no ingested conversation", "session-unresolvable"
                    )
                ],
                discarded=[],
                stale_sessions_pruned=1,
            ),
        )
        rc = data_cli._doctor_fix_pending_tags(SimpleNamespace(db=str(test_db), json=False))
        assert rc == 0
        captured = capsys.readouterr()
        assert "Applied 1 queued tag(s)" in captured.out + captured.err
        assert "Pruned 1 stale session registration(s)" in captured.out + captured.err
        assert "sess-2" in captured.out
