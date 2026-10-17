"""Tests for 'siftd dump' — golden assembly files."""

import json

import pytest
from conftest import FIXTURES_DIR

from siftd.cli import main

CLAUDE_MINIMAL = str(FIXTURES_DIR / "adapters" / "claude_code" / "minimal" / "input.jsonl")


class TestDumpGolden:

    def test_stdout(self, capsys):
        assert main(["dump", "--golden", CLAUDE_MINIMAL, "--adapter", "claude_code"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert set(doc) == {"golden_version", "adapter", "source", "siftd_commit", "conversations", "ledger"}
        assert doc["adapter"] == "claude_code"

    def test_out_file(self, tmp_path, capsys):
        out = tmp_path / "nested" / "s.json"
        assert main(["dump", "--golden", CLAUDE_MINIMAL, "--out", str(out), "--root", str(FIXTURES_DIR)]) == 0
        assert capsys.readouterr().out == ""
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["source"] == "adapters/claude_code/minimal/input.jsonl"
        assert doc["ledger"]["records"] == sum(doc["ledger"]["dispositions"].values())

    def test_unknown_adapter_exits_2(self, capsys):
        assert main(["dump", "--golden", CLAUDE_MINIMAL, "--adapter", "nope"]) == 2


class TestDumpGoldenFixtures:

    def test_writes_one_file_per_source(self, tmp_path):
        assert main(["dump", "--golden-fixtures", str(FIXTURES_DIR), "--out", str(tmp_path)]) == 0
        assert (tmp_path / "claude_code" / "minimal.json").is_file()
        assert (tmp_path / "claude_code" / "claude_code_minimal.json").is_file()
        assert (tmp_path / "opencode" / "tool_use.json").is_file()

    def test_requires_out(self):
        assert main(["dump", "--golden-fixtures", str(FIXTURES_DIR)]) == 2

    def test_adapter_flag_is_for_single_sources(self, tmp_path):
        assert main(["dump", "--golden-fixtures", str(FIXTURES_DIR), "--out", str(tmp_path),
                     "--adapter", "aider"]) == 2


class TestDumpParser:

    def test_a_mode_is_required_and_modes_exclude_each_other(self):
        with pytest.raises(SystemExit):
            main(["dump"])
        with pytest.raises(SystemExit):
            main(["dump", "--golden", CLAUDE_MINIMAL, "--golden-fixtures", str(FIXTURES_DIR)])
