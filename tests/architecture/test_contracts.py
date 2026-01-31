"""CLI behavior contract tests.

These tests invoke CLI commands and verify output contracts. They
ensure JSON output is pure, exit codes are correct, and help text
is stable.
"""

import json
import subprocess
from pathlib import Path

import pytest

from siftd.storage.sqlite import create_database


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def test_db_path(tmp_path):
    """Create a minimal test database for contract tests."""
    db = tmp_path / "test.db"
    conn = create_database(db)
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def nonexistent_db(tmp_path):
    """Return a path to a non-existent database file."""
    return tmp_path / "nonexistent.db"


# =============================================================================
# 1. JSON Purity Tests
# =============================================================================


class TestJsonPurity:
    """Commands with --json must output only valid JSON to stdout.

    Rationale: JSON output is for machine parsing. Any non-JSON text
    (tips, warnings, progress) breaks downstream tooling.

    Rule: stdout is valid JSON. stderr can have anything.
    """

    # Commands that support --json and can run without data
    # Format: (subcommand_args, requires_data)
    COMMANDS_WITH_JSON = [
        (["status", "--json"], False),
        (["adapters", "--json"], False),
        (["doctor", "--json"], False),
        (["tools", "--json"], True),  # May return empty if no tool calls
    ]

    @pytest.mark.parametrize("cmd_suffix,requires_data", COMMANDS_WITH_JSON)
    def test_json_output_is_pure(self, cmd_suffix, requires_data, test_db_path):
        """stdout is valid JSON when --json is used."""
        cmd = ["uv", "run", "siftd", "--db", str(test_db_path)] + cmd_suffix
        result = subprocess.run(cmd, capture_output=True, text=True)

        stdout = result.stdout.strip()
        if stdout:
            try:
                json.loads(stdout)
            except json.JSONDecodeError as e:
                pytest.fail(
                    f"Command {' '.join(cmd_suffix)} produced invalid JSON:\n"
                    f"stdout: {stdout[:500]}...\n"
                    f"stderr: {result.stderr[:200]}...\n"
                    f"error: {e}"
                )
        # Empty stdout is acceptable for some commands with no data


# =============================================================================
# 2. Exit Code Contracts
# =============================================================================


class TestExitCodes:
    """Commands must return correct exit codes.

    Rule:
    - 0 = success
    - 1 = user error (bad args, missing file)
    - 2 = argparse error (invalid arguments)
    """

    def test_missing_db_returns_nonzero(self, nonexistent_db):
        """Commands that need DB return nonzero when DB missing."""
        cmd = ["uv", "run", "siftd", "--db", str(nonexistent_db), "status"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Should fail because DB doesn't exist
        assert result.returncode != 0, (
            f"Expected nonzero exit code for missing DB, got {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_success_returns_zero(self, test_db_path):
        """Successful commands return 0."""
        cmd = ["uv", "run", "siftd", "--db", str(test_db_path), "status"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        assert result.returncode == 0, (
            f"Expected exit code 0, got {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_invalid_args_returns_nonzero(self):
        """Invalid arguments return nonzero (argparse default is 2)."""
        cmd = ["uv", "run", "siftd", "--invalid-flag-that-does-not-exist"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        assert result.returncode != 0, (
            f"Expected nonzero exit code for invalid args, got {result.returncode}"
        )

    def test_help_returns_zero(self):
        """--help returns 0."""
        cmd = ["uv", "run", "siftd", "--help"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        assert result.returncode == 0, (
            f"Expected exit code 0 for --help, got {result.returncode}"
        )

    def test_version_returns_zero(self):
        """--version returns 0."""
        cmd = ["uv", "run", "siftd", "--version"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        assert result.returncode == 0, (
            f"Expected exit code 0 for --version, got {result.returncode}"
        )
