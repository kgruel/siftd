"""Tests for the golden assembly dump (api/golden.py)."""

import json
import time

import pytest
from conftest import FIXTURES_DIR

from siftd.api import dump_fixtures, dump_golden_source, golden_json
from siftd.api.golden import GOLDEN_VERSION
from siftd.errors import UserInputError

TOP_LEVEL_KEYS = {"golden_version", "adapter", "source", "siftd_commit", "conversations", "ledger"}
LEDGER_KEYS = {"units", "records", "synthesized", "empty_conversations", "dispositions", "reasons"}
CLAUDE_MINIMAL = FIXTURES_DIR / "adapters" / "claude_code" / "minimal" / "input.jsonl"


def _covers(expected, actual, path="$"):
    """Every key and value of ``expected`` is in ``actual`` (expected.json omits defaulted fields)."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        for key, value in expected.items():
            assert key in actual, f"{path}.{key} missing"
            _covers(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for i, (e, a) in enumerate(zip(expected, actual)):
            _covers(e, a, f"{path}[{i}]")
    else:
        assert expected == actual, path


class TestDumpSource:

    def test_claude_code_minimal_shape(self):
        doc = dump_golden_source(CLAUDE_MINIMAL, root=FIXTURES_DIR, commit="abc123")
        assert set(doc) == TOP_LEVEL_KEYS
        assert doc["golden_version"] == GOLDEN_VERSION == 1
        assert doc["adapter"] == "claude_code"
        assert doc["source"] == "adapters/claude_code/minimal/input.jsonl"
        assert doc["siftd_commit"] == "abc123"
        (conversation,) = doc["conversations"]
        assert conversation["external_id"].startswith("claude_code::")
        assert conversation["harness"]["name"] == "claude_code"
        assert conversation["prompts"][0]["responses"]

    def test_ledger_sums(self):
        ledger = dump_golden_source(CLAUDE_MINIMAL, root=FIXTURES_DIR)["ledger"]
        assert set(ledger) == LEDGER_KEYS
        assert ledger["records"] == sum(ledger["dispositions"].values())
        assert all(":" in key for key in ledger["dispositions"])
        assert all(":" in key for key in ledger["reasons"])
        assert ledger["units"] > 0

    def test_json_round_trip(self):
        doc = dump_golden_source(CLAUDE_MINIMAL, root=FIXTURES_DIR)
        text = golden_json(doc)
        assert json.loads(text) == json.loads(json.dumps(doc))  # tuples are arrays
        assert golden_json(json.loads(text)) == text
        assert text.startswith('{\n "adapter"')  # sorted keys, indent 1

    def test_non_ascii_is_kept(self):
        assert "é" in golden_json({"k": "é"}) and "\\u" not in golden_json({"k": "é"})

    def test_legacy_adapter_has_null_ledger(self):
        gemini = FIXTURES_DIR / "adapters" / "gemini_cli" / "minimal" / "input.json"
        doc = dump_golden_source(gemini, adapter="gemini_cli", root=FIXTURES_DIR)
        assert doc["ledger"] is None
        assert doc["conversations"]

    def test_sql_fixture_is_materialized(self):
        sql = FIXTURES_DIR / "adapters" / "opencode" / "tool_use" / "setup.sql"
        doc = dump_golden_source(sql, root=FIXTURES_DIR)
        assert doc["adapter"] == "opencode" and doc["ledger"]["records"] > 0

    def test_adapter_inferred_from_name_or_path(self):
        top = FIXTURES_DIR / "codex_cli_minimal.jsonl"
        assert dump_golden_source(top)["adapter"] == "codex_cli"
        assert dump_golden_source(CLAUDE_MINIMAL)["adapter"] == "claude_code"

    def test_errors_are_user_input_errors(self, tmp_path):
        with pytest.raises(UserInputError):
            dump_golden_source(tmp_path / "missing.jsonl")
        with pytest.raises(UserInputError, match="Unknown adapter"):
            dump_golden_source(CLAUDE_MINIMAL, adapter="nope")
        stray = tmp_path / "x.txt"
        stray.write_text("x")
        with pytest.raises(UserInputError, match="No built-in adapter"):
            dump_golden_source(stray)
        with pytest.raises(UserInputError, match="not under the fixtures root"):
            dump_golden_source(stray, adapter="aider", root=FIXTURES_DIR)

    def test_aider_does_not_depend_on_host_zone_or_checkout_location(self, monkeypatch):
        aider = FIXTURES_DIR / "adapters" / "aider" / "minimal" / ".aider.chat.history.md"
        monkeypatch.setenv("TZ", "Pacific/Auckland")
        time.tzset()
        try:
            doc = dump_golden_source(aider, root=FIXTURES_DIR, commit="x")
        finally:
            monkeypatch.undo()
            time.tzset()
        (conversation,) = doc["conversations"]
        assert conversation["workspace_path"] == "tests/fixtures/adapters/aider/minimal"
        assert conversation["started_at"] == "2025-07-15T14:32:01+00:00"


class TestDumpFixtures:

    @pytest.fixture(scope="class")
    def dumped(self, tmp_path_factory):
        out = tmp_path_factory.mktemp("golden")
        return dump_fixtures(FIXTURES_DIR, out, commit="abc123"), out

    def test_every_case_and_top_level_file_is_written(self, dumped):
        result, out = dumped
        cases = {(p.parent.name, p.name) for p in (FIXTURES_DIR / "adapters").glob("*/*") if p.is_dir()}
        written = {(f.adapter, f.case) for f in result.files}
        assert cases <= written
        assert {(a, a + "_minimal") for a in
                ("claude_code", "codex_cli", "copilot_cli", "gemini_cli", "pi_agent")} <= written
        assert all(f.path.is_file() and f.path.parent.name == f.adapter for f in result.files)
        assert result.empty == []
        assert set(result.adapters) == {p.name for p in (FIXTURES_DIR / "adapters").iterdir()}

    def test_files_are_golden_documents(self, dumped):
        result, _ = dumped
        for item in result.files:
            doc = json.loads(item.path.read_text(encoding="utf-8"))
            assert set(doc) == TOP_LEVEL_KEYS, item.path
            assert doc["adapter"] == item.adapter and doc["siftd_commit"] == "abc123"
            assert (FIXTURES_DIR / doc["source"]).is_file(), doc["source"]
            if doc["ledger"] is not None:
                assert doc["ledger"]["records"] == sum(doc["ledger"]["dispositions"].values()), item.path

    def test_agrees_with_the_adapter_golden_fixtures(self, dumped):
        """The dump is the same parse the per-adapter expected.json files pin."""
        result, _ = dumped
        checked = 0
        for item in result.files:
            expected = FIXTURES_DIR / "adapters" / item.adapter / item.case / "expected.json"
            if not expected.is_file():
                continue
            doc = json.loads(item.path.read_text(encoding="utf-8"))
            _covers(json.loads(expected.read_text(encoding="utf-8")), doc["conversations"])
            checked += 1
        assert checked >= 49

    def test_undiscovered_cases_are_reported(self, dumped):
        result, _ = dumped
        assert "opencode" in result.undiscovered and "claude_code" not in result.undiscovered

    def test_second_run_is_identical(self, dumped, tmp_path):
        result, out = dumped
        dump_fixtures(FIXTURES_DIR, tmp_path, commit="abc123")
        for item in result.files:
            assert (tmp_path / item.adapter / f"{item.case}.json").read_bytes() == item.path.read_bytes()

    def test_not_a_fixtures_directory(self, tmp_path):
        with pytest.raises(UserInputError, match="no adapters/"):
            dump_fixtures(tmp_path, tmp_path / "out")

    def test_unknown_adapter_directory(self, tmp_path):
        (tmp_path / "adapters" / "nonesuch" / "c").mkdir(parents=True)
        with pytest.raises(UserInputError, match="nonesuch"):
            dump_fixtures(tmp_path, tmp_path / "out")
