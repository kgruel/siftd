"""A deprecated adapter's notice reaches users where they meet the adapter.

The notice is shown in the two places a user sees an adapter: the `siftd
adapters` listing (and its JSON `deprecated` field) and the ingest summary when
the adapter contributed sources. Both read DEPRECATED_ADAPTERS
(siftd.api.compat), so the version a user is told is the version the ledger
holds. The registry is empty since gemini_cli left in 0.13.0; these plant an
entry on a loaded adapter so the mechanism stays covered for the next one.
"""

from __future__ import annotations

import json

import pytest

from siftd.api import compat
from siftd.api.ingest import IngestRunResult
from siftd.cli import main
from siftd.ingestion import IngestStats

PLANTED = compat.Deprecation(successor="claude_code", removed_in="9.9.9")


@pytest.fixture
def aider_deprecated(monkeypatch):
    monkeypatch.setattr(compat, "DEPRECATED_ADAPTERS", {"aider": PLANTED})


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


def test_lookup_reads_the_registry(aider_deprecated):
    assert compat.adapter_deprecation("aider") is PLANTED
    assert compat.adapter_deprecation("claude_code") is None


class TestAdaptersListing:
    def test_text_listing_names_the_deprecation(self, aider_deprecated, capsys):
        assert main(["adapters"]) == 0
        err = capsys.readouterr().err
        assert "Adapter 'aider' is deprecated and is removed in 9.9.9" in err
        assert "Its successor is 'claude_code'." in err

    def test_json_listing_carries_the_deprecation_per_adapter(self, aider_deprecated, capsys):
        assert main(["adapters", "--json"]) == 0
        by_name = {a["name"]: a for a in json.loads(capsys.readouterr().out)}
        assert by_name["aider"]["deprecated"] == {"removed_in": "9.9.9", "successor": "claude_code"}
        assert by_name["claude_code"]["deprecated"] is None

    def test_no_notice_without_an_entry(self, capsys):
        assert main(["adapters"]) == 0
        assert "deprecated" not in capsys.readouterr().err


class TestIngestSummary:
    def test_notice_when_the_adapter_contributed_sources(self, aider_deprecated, tmp_path, capsys, monkeypatch):
        result = _result(tmp_path, by_harness={"aider": {}, "claude_code": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, []) == 0
        err = capsys.readouterr().err
        assert "Adapter 'aider' is deprecated and is removed in 9.9.9" in err
        assert "Its successor is 'claude_code'." in err

    def test_no_notice_when_the_adapter_saw_no_sources(self, aider_deprecated, tmp_path, capsys, monkeypatch):
        result = _result(tmp_path, by_harness={"claude_code": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, []) == 0
        assert "deprecated" not in capsys.readouterr().err

    def test_notice_survives_quiet_mode(self, aider_deprecated, tmp_path, capsys, monkeypatch):
        """A deprecation is a standing warning, not chatter."""
        result = _result(tmp_path, by_harness={"aider": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, ["-q"]) == 0
        assert "removed in 9.9.9" in capsys.readouterr().err

    def test_json_mode_event(self, aider_deprecated, tmp_path, capsys, monkeypatch):
        result = _result(tmp_path, by_harness={"aider": {}})
        assert _run_ingest(tmp_path, monkeypatch, result, ["--json"]) == 0
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        deprecated = [e for e in events if e.get("kind") == "deprecated"]
        assert len(deprecated) == 1
        assert deprecated[0]["adapter"] == "aider"
        assert deprecated[0]["removed_in"] == "9.9.9"
        assert deprecated[0]["successor"] == "claude_code"


@pytest.mark.parametrize("name", sorted(compat.DEPRECATED_ADAPTERS))
def test_adapter_docstring_states_the_ledger_version(name):
    """The adapter module tells a reader the same version the ledger holds.

    Collects no case while the registry is empty; it wakes when an entry lands.
    """
    import importlib

    module = importlib.import_module(f"siftd.adapters.{name}")
    assert f"removed in {compat.DEPRECATED_ADAPTERS[name].removed_in}" in (module.__doc__ or "")
