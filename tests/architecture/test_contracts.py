"""CLI behavior contract tests.

These tests invoke CLI commands and verify output contracts. They
ensure JSON output is pure, exit codes are correct, and help text
is stable.
"""

import json
import os
import subprocess

import pytest

from architecture.support import REPO_ROOT, SRC, source_files
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
        (["workspaces", "--json"], True),
        (["query", "--json"], True),
        (["search", "--json", "test"], True),
        (["peek", "--json"], True),
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
        cmd = ["uv", "run", "siftd", "--db", str(nonexistent_db), "db", "stats"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Should fail because DB doesn't exist
        assert result.returncode != 0, (
            f"Expected nonzero exit code for missing DB, got {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_success_returns_zero(self, test_db_path):
        """Successful commands return 0."""
        cmd = ["uv", "run", "siftd", "--db", str(test_db_path), "db", "stats"]
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


# =============================================================================
# 3. Command Reference Validation
# =============================================================================


class TestCommandReferences:
    """All 'siftd <subcommand>' references in source code must be valid.

    Rationale: User-facing messages and documentation reference CLI commands.
    Invalid references (typos, renamed commands) confuse users and break
    copy-paste workflows.

    Rule: Any string containing 'siftd <word>' that looks like a command
    invocation must have <word> be a valid subcommand, unless it's in the
    allowlist for backward-compat patterns.
    """

    # Allowlist for intentional backward-compat patterns.
    # Format: (file_suffix, pattern) — pattern must appear in the string
    ALLOWLIST = [
        # storage/tags.py checks for 'siftd ask' to detect historical tool calls
        # in conversation logs (the old command name before rename to 'search')
        ("storage/tags.py", "siftd ask"),
        # cli/tags.py deprecation bridge references 'siftd tags' in warning message
        ("cli/tags.py", "siftd tags"),
    ]

    # Words that look like commands but are actually prose
    # These follow "siftd" in descriptive text, not command examples
    PROSE_WORDS = {
        "is", "was", "will", "can", "should", "must", "may", "has", "have",
        "CLI", "cli", "tool", "adapter", "adapters", "functionality", "storage",
        "data", "database", "generated", "installed", "project", "skill",
    }

    def test_command_references_are_valid(self):
        """All 'siftd <subcommand>' references must be valid CLI commands."""
        import re
        from pathlib import Path

        # Get valid subcommands by parsing --help output. Pin COLUMNS so the
        # 'Advanced (hidden): …' line never wraps — its continuation would drop
        # plumbing commands from the parse and make this test width-dependent.
        env = {**os.environ, "COLUMNS": "80"}
        result = subprocess.run(
            ["uv", "run", "siftd", "--help"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"Failed to get help: {result.stderr}"

        # Extract subcommands from the terse inline lane legend: each lane is one
        # row, `  EXPLORE   query · search · …`, the names following the (uppercase)
        # lane label and ·-separated.
        valid_subcommands = set()
        for line in result.stdout.splitlines():
            match = re.match(r"^ {2}[A-Z][A-Z]+ {2,}(.+)$", line)
            if match:
                valid_subcommands.update(p.strip() for p in match.group(1).split("·") if p.strip())

        # Plumbing commands are hidden from the lane listing but remain valid;
        # the help advertises them on the epilog 'Advanced (hidden): ...' line.
        hidden_match = re.search(r"Advanced \(hidden\):\s*(.+)", result.stdout)
        if hidden_match:
            for cmd in hidden_match.group(1).split(","):
                valid_subcommands.add(cmd.strip())

        assert valid_subcommands, "Failed to parse subcommands from help output"

        # Scan source files for 'siftd <subcommand>' patterns
        # Match 'siftd <word>' - lowercase word suggests a command
        pattern = re.compile(r"siftd\s+([a-z][\w-]*)")

        invalid_references = []

        for py_file in source_files(SRC):
            content = py_file.read_text()
            rel_path = str(py_file.relative_to(REPO_ROOT))

            for match in pattern.finditer(content):
                subcommand = match.group(1)
                full_match = match.group(0)

                # Skip if subcommand is valid
                if subcommand in valid_subcommands:
                    continue

                # Skip prose words (descriptive text, not commands)
                if subcommand in self.PROSE_WORDS:
                    continue

                # Check allowlist
                allowed = False
                for file_suffix, allowed_pattern in self.ALLOWLIST:
                    if rel_path.endswith(file_suffix) and allowed_pattern in full_match:
                        allowed = True
                        break

                if not allowed:
                    # Get line number for better error message
                    line_num = content[:match.start()].count("\n") + 1
                    invalid_references.append(
                        f"{rel_path}:{line_num}: '{full_match}' ('{subcommand}' is not a valid subcommand)"
                    )

        assert not invalid_references, (
            f"Found {len(invalid_references)} invalid 'siftd <subcommand>' reference(s):\n"
            + "\n".join(f"  - {ref}" for ref in invalid_references)
        )


# =============================================================================
# 4. Doctor Fix Command Validation
# =============================================================================


class TestDoctorFixCommands:
    """Doctor fix_command suggestions must reference valid CLI subcommands.

    Rationale: If doctor suggests "siftd foo --bar" to fix an issue,
    that command must actually exist. This catches the bug where doctor
    suggested non-existent flags.
    """

    def test_fix_commands_are_valid_subcommands(self, test_db_path):
        """All doctor fix_command values must be runnable CLI commands."""
        # Get doctor findings as JSON
        cmd = ["uv", "run", "siftd", "--db", str(test_db_path), "doctor", "--json"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if not result.stdout.strip():
            pytest.skip("No doctor output")

        data = json.loads(result.stdout)
        findings = data.get("findings", [])

        invalid_commands = []
        for finding in findings:
            fix_cmd = finding.get("fix_command", "")
            if not fix_cmd or not fix_cmd.startswith("siftd "):
                continue

            # Extract the subcommand (first word after 'siftd')
            parts = fix_cmd.split()
            if len(parts) < 2:
                continue

            subcommand = parts[1]
            # Skip if it looks like a flag (--db, etc.)
            if subcommand.startswith("-"):
                continue

            # Verify the subcommand exists by checking --help
            help_result = subprocess.run(
                ["uv", "run", "siftd", subcommand, "--help"],
                capture_output=True,
                text=True,
            )
            if help_result.returncode != 0:
                invalid_commands.append(fix_cmd)

        assert not invalid_commands, (
            f"Doctor suggested invalid fix commands:\n"
            + "\n".join(f"  - {cmd}" for cmd in invalid_commands)
        )
