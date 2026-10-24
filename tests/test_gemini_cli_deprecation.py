"""gemini_cli is deprecated, and the notice reaches users where they meet the adapter.

Gemini CLI became Antigravity CLI. The adapter stays loaded until
GEMINI_CLI_REMOVAL (siftd.api.compat), and the notice is shown in the two places
a user sees the adapter: the `siftd adapters` listing and the ingest summary
when gemini_cli contributes sources. Both read DEPRECATED_ADAPTERS, so the
version a user is told is the version the ledger holds.
"""

from __future__ import annotations

import json

import pytest

from siftd.api import compat
from siftd.api.ingest import IngestRunResult
from siftd.cli import main
from siftd.ingestion import IngestStats


def _result(tmp_path, *, by_harness):
    stats = IngestStats()
    stats.by_harness = by_harness
    return IngestRunResult(
        db_path=tmp_path / "siftd.db",
        db_created=False,
        mode="ingest",
        adapters=list(by_harness),
        scan_paths=[],
        stats=stats,
        elapsed_ms=0,
    )


def _run_ingest(tmp_path, monkeypatch, result, argv_tail):
    monkeypatch.setattr("siftd.api.run_ingest", lambda **kw: result)
    monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)
    return main(["--db", str(tmp_path / "siftd.db"), "ingest", *argv_tail])


def test_ledger_names_gemini_cli_with_the_successor():
    deprecation = compat.adapter_deprecation("gemini_cli")
    assert deprecation is not None
    assert deprecation.removed_in == compat.GEMINI_CLI_REMOVAL == "0.13.0"
    assert deprecation.successor == "antigravity_cli"


def test_only_gemini_cli_is_deprecated():
    assert compat.adapter_deprecation("claude_code") is None
    assert compat.adapter_deprecation("antigravity_cli") is None


class TestAdaptersListing:
    def test_text_listing_names_the_deprecation(self, capsys):
        assert main(["adapters"]) == 0
        captured = capsys.readouterr()
        assert "Adapter 'gemini_cli' is deprecated and is removed in 0.13.0" in captured.err
        assert "antigravity_cli" in captured.err

    def test_json_listing_carries_the_deprecation_per_adapter(self, capsys):
        assert main(["adapters", "--json"]) == 0
        by_name = {a["name"]: a for a in json.loads(capsys.readouterr().out)}
        assert by_name["gemini_cli"]["deprecated"] == {
            "removed_in": "0.13.0",
            "successor": "antigravity_cli",
        }
        assert by_name["claude_code"]["deprecated"] is None


class TestIngestSummary:
    def test_notice_when_gemini_cli_contributed_sources(self, tmp_path, capsys, monkeypatch):
        result = _result(tmp_path, by_harness={"gemini_cli": {}, "claude_code": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, []) == 0
        err = capsys.readouterr().err
        assert "Adapter 'gemini_cli' is deprecated and is removed in 0.13.0" in err
        assert "Its successor is 'antigravity_cli'." in err

    def test_no_notice_when_gemini_cli_saw_no_sources(self, tmp_path, capsys, monkeypatch):
        result = _result(tmp_path, by_harness={"claude_code": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, []) == 0
        assert "deprecated" not in capsys.readouterr().err

    def test_notice_survives_quiet_mode(self, tmp_path, capsys, monkeypatch):
        """A deprecation is a standing warning, not chatter."""
        result = _result(tmp_path, by_harness={"gemini_cli": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, ["-q"]) == 0
        assert "removed in 0.13.0" in capsys.readouterr().err

    def test_json_mode_event(self, tmp_path, capsys, monkeypatch):
        result = _result(tmp_path, by_harness={"gemini_cli": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, ["--json"]) == 0
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        deprecated = [e for e in events if e.get("kind") == "deprecated"]
        assert len(deprecated) == 1
        assert deprecated[0]["adapter"] == "gemini_cli"
        assert deprecated[0]["removed_in"] == "0.13.0"
        assert deprecated[0]["successor"] == "antigravity_cli"


@pytest.mark.parametrize("name", sorted(compat.DEPRECATED_ADAPTERS))
def test_adapter_docstring_states_the_ledger_version(name):
    """The adapter module tells a reader the same version the ledger holds."""
    import importlib

    module = importlib.import_module(f"siftd.adapters.{name}")
    assert f"removed in {compat.DEPRECATED_ADAPTERS[name].removed_in}" in (module.__doc__ or "")
