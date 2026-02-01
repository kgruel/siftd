"""Snapshot tests for CLI help output stability.

Run with: pytest tests/snapshots/ -v
Update with: pytest tests/snapshots/ --snapshot-update
"""

import subprocess

import pytest


def run_siftd(*args: str) -> str:
    """Run siftd and return stdout."""
    result = subprocess.run(
        ["uv", "run", "siftd", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


# All subcommands to test
SUBCOMMANDS = [
    "ingest",
    "status",
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
        # Normalize dynamic path
        normalized = stdout.replace("/Users/kaygee/.local/share/siftd/siftd.db", "~/.local/share/siftd/siftd.db")
        assert normalized == snapshot

    @pytest.mark.parametrize("subcommand", SUBCOMMANDS)
    def test_subcommand_help(self, subcommand, snapshot):
        """Test each subcommand's --help output."""
        stdout = run_siftd(subcommand, "--help")
        # Normalize any dynamic paths
        normalized = stdout.replace("/Users/kaygee/.local/share/siftd/siftd.db", "~/.local/share/siftd/siftd.db")
        assert normalized == snapshot
