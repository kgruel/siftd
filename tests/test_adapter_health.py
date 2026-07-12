"""Tests for adapter-health warnings: zero-discovery and drop-in import failures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from siftd.api.ingest import IngestRunResult
from siftd.ingestion import IngestStats
from siftd.plugin_discovery import load_dropin_modules


def _ok_validate(mod, origin):
    return None


def _fail_validate(mod, origin):
    return "missing required attributes"


# ---------------------------------------------------------------------------
# load_dropin_modules failures_out kwarg
# ---------------------------------------------------------------------------


class TestLoadDropinModulesFailuresOut:
    def test_import_error_captured(self, tmp_path):
        bad = tmp_path / "bad_adapter.py"
        bad.write_text("raise ImportError('no module named xyz')\n")
        failures: list[tuple[Path, str]] = []
        plugins = load_dropin_modules(tmp_path, "prefix_", _ok_validate, failures_out=failures)
        assert plugins == []
        assert len(failures) == 1
        assert failures[0][0] == bad
        assert "import failed" in failures[0][1]

    def test_validation_failure_captured(self, tmp_path):
        good_import = tmp_path / "mymodule.py"
        good_import.write_text("NAME = 'x'\n")
        failures: list[tuple[Path, str]] = []
        plugins = load_dropin_modules(tmp_path, "prefix_", _fail_validate, failures_out=failures)
        assert plugins == []
        assert len(failures) == 1
        assert failures[0][0] == good_import
        assert "missing required attributes" in failures[0][1]

    def test_successful_load_not_captured(self, tmp_path):
        good = tmp_path / "good.py"
        good.write_text("NAME = 'myadapter'\n")
        failures: list[tuple[Path, str]] = []
        plugins = load_dropin_modules(tmp_path, "prefix_", _ok_validate, failures_out=failures)
        assert len(plugins) == 1
        assert failures == []

    def test_failures_out_none_is_backward_compat(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text("raise ImportError('oops')\n")
        # Should not raise even without failures_out
        plugins = load_dropin_modules(tmp_path, "prefix_", _ok_validate)
        assert plugins == []

    def test_multiple_failures_all_captured(self, tmp_path):
        (tmp_path / "a.py").write_text("raise ImportError('a')\n")
        (tmp_path / "b.py").write_text("raise ImportError('b')\n")
        failures: list[tuple[Path, str]] = []
        load_dropin_modules(tmp_path, "prefix_", _ok_validate, failures_out=failures)
        assert len(failures) == 2
        paths = {f[0] for f in failures}
        assert tmp_path / "a.py" in paths
        assert tmp_path / "b.py" in paths


# ---------------------------------------------------------------------------
# load_all_adapters failures_out kwarg
# ---------------------------------------------------------------------------


class TestLoadAllAdaptersFailuresOut:
    def test_bad_dropin_captured(self, tmp_path):
        from siftd.adapters.registry import load_all_adapters

        bad = tmp_path / "bad_dropin.py"
        bad.write_text("raise ImportError('missing dep')\n")
        failures: list[tuple[Path, str]] = []
        plugins = load_all_adapters(dropin_path=tmp_path, failures_out=failures)
        builtin_names = {p.name for p in plugins}
        assert len(builtin_names) > 0  # builtins still loaded
        assert len(failures) == 1
        assert failures[0][0] == bad
        assert "import failed" in failures[0][1]

    def test_no_dropin_dir_no_failures(self, tmp_path):
        from siftd.adapters.registry import load_all_adapters

        nonexistent = tmp_path / "no_such_dir"
        failures: list[tuple[Path, str]] = []
        plugins = load_all_adapters(dropin_path=nonexistent, failures_out=failures)
        assert failures == []
        assert len(plugins) > 0  # builtins still returned


# ---------------------------------------------------------------------------
# IngestRunResult backward compat
# ---------------------------------------------------------------------------


class TestIngestRunResultDefaults:
    def test_dropin_failures_defaults_to_empty_list(self, tmp_path):
        result = IngestRunResult(
            db_path=tmp_path / "test.db",
            db_created=True,
            mode="ingest",
            adapters=[],
            scan_paths=[],
            stats=IngestStats(),
            elapsed_ms=0,
        )
        assert result.dropin_failures == []


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def _make_result(tmp_path, *, adapters, by_harness=None, dropin_failures=None, adapter_tiers=None):
    stats = IngestStats()
    stats.by_harness = by_harness or {}
    return IngestRunResult(
        db_path=tmp_path / "siftd.db",
        db_created=False,
        mode="ingest",
        adapters=adapters,
        scan_paths=[],
        stats=stats,
        elapsed_ms=100,
        dropin_failures=dropin_failures or [],
        adapter_tiers=adapter_tiers or {},
    )


class TestAdapterHealthRendering:
    def test_zero_discovery_printed_verbose(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["claude-code", "aider"],
            by_harness={"claude-code": {}},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--verbose"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "aider" in err
        assert "found nothing to ingest" in err

    def test_zero_discovery_suppressed_in_quiet_mode(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["aider"],
            by_harness={},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "-q"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "found nothing to ingest" not in out

    def test_dropin_failure_visible_in_quiet_mode(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["claude-code"],
            by_harness={"claude-code": {}},
            dropin_failures=[(Path("/config/adapters/bad.py"), "import failed: no module 'xyz'")],
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "-q"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "bad.py" in err
        assert "failed to load" in err

    def test_no_zero_discovery_when_adapter_has_files(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["claude-code"],
            by_harness={"claude-code": {}},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--verbose"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "found nothing to ingest" not in out

    def test_json_mode_zero_discovery_event(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["aider"],
            by_harness={},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        warning_events = [e for e in events if e.get("type") == "adapter_warning"]
        assert len(warning_events) == 1
        assert warning_events[0]["kind"] == "zero_discovery"
        assert warning_events[0]["adapter"] == "aider"
        assert "found nothing to ingest" in warning_events[0]["message"]

    def test_json_mode_dropin_failure_event(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["claude-code"],
            by_harness={"claude-code": {}},
            dropin_failures=[(Path("/cfg/bad.py"), "import failed: oops")],
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        warning_events = [e for e in events if e.get("type") == "adapter_warning"]
        assert len(warning_events) == 1
        assert warning_events[0]["kind"] == "failed_import"
        assert "/cfg/bad.py" in warning_events[0]["path"]
        assert "failed to load" in warning_events[0]["message"]


class TestNonCoreErrorTagging:
    """File errors from non-core adapters are tagged with their support tier."""

    def test_noncore_errors_tagged_in_terminal_output(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["gemini_cli"],
            by_harness={"gemini_cli": {"errors": 3}},
            adapter_tiers={"gemini_cli": "frozen"},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--verbose"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "gemini_cli" in err
        assert "frozen-tier" in err
        assert "3 file error(s)" in err

    def test_core_errors_not_tagged(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["claude_code"],
            by_harness={"claude_code": {"errors": 2}},
            adapter_tiers={"claude_code": "core"},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--verbose"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "-tier" not in err

    def test_unknown_tier_defaults_to_contrib(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["mystery"],
            by_harness={"mystery": {"errors": 1}},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--verbose"])
        assert rc == 0
        assert "contrib-tier" in capsys.readouterr().err

    def test_json_mode_noncore_errors_event(self, tmp_path, capsys, monkeypatch):
        result = _make_result(
            tmp_path,
            adapters=["opencode"],
            by_harness={"opencode": {"errors": 2}},
            adapter_tiers={"opencode": "contrib"},
        )
        monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
        monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)

        from siftd.cli import main

        rc = main(["--db", str(tmp_path / "siftd.db"), "ingest", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        warning_events = [e for e in events if e.get("type") == "adapter_warning"]
        assert len(warning_events) == 1
        ev = warning_events[0]
        assert ev["kind"] == "noncore_errors"
        assert ev["adapter"] == "opencode"
        assert ev["tier"] == "contrib"
        assert ev["errors"] == 2
