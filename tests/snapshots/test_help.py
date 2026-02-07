"""Snapshot tests for CLI help output stability.

Run with: pytest tests/snapshots/ -v
Update with: pytest tests/snapshots/ --snapshot-update

Note: These tests are skipped in CI because argparse formats help text
differently across Python versions and platforms (line wrapping, spacing).
Run locally to catch unintended help text changes.
"""

import os
import subprocess

import pytest

# Skip in CI - argparse formatting varies by platform/Python version
pytestmark = pytest.mark.skipif(
    os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true",
    reason="Help snapshot tests are platform-specific (argparse formatting varies)",
)

# Get home directory for path normalization (works across different machines/CI)
HOME = os.path.expanduser("~")


def run_siftd(*args: str) -> str:
    """Run siftd and return stdout."""
    # Set fixed terminal width for consistent argparse formatting
    env = os.environ.copy()
    env["COLUMNS"] = "80"
    result = subprocess.run(
        ["uv", "run", "siftd", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return result.stdout


# All subcommands to test
SUBCOMMANDS = [
    "ingest",
    "status",
    "workspaces",
    "search",
    "install",
    "register",
    "session-id",
    "tag",
    "tags",
    "tools",
    "query",
    "backfill",
    "path",
    "config",
    "adapters",
    "copy",
    "doctor",
    "peek",
    "export",
]


class TestHelpSnapshots:
    """Snapshot test all --help outputs to catch unintended drift."""

    def test_root_help(self, snapshot):
        """Test root siftd --help output."""
        stdout = run_siftd("--help")
        # Normalize dynamic path (works across different machines/CI)
        normalized = stdout.replace(HOME, "~")
        assert normalized == snapshot

    @pytest.mark.parametrize("subcommand", SUBCOMMANDS)
    def test_subcommand_help(self, subcommand, snapshot):
        """Test each subcommand's --help output."""
        stdout = run_siftd(subcommand, "--help")
        # Normalize dynamic path (works across different machines/CI)
        normalized = stdout.replace(HOME, "~")
        assert normalized == snapshot
