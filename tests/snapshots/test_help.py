"""Snapshot tests for CLI help output stability.

Run with: pytest tests/snapshots/ -v
Update with: pytest tests/snapshots/ --snapshot-update -n 0

SNAPSHOT POLICY
---------------
Snapshots are stored in per-Python-version subdirectories:
  tests/snapshots/__snapshots__/py312/test_help.ambr
  tests/snapshots/__snapshots__/py313/test_help.ambr
  tests/snapshots/__snapshots__/py314/test_help.ambr

Each version writes only to its own directory, so --snapshot-update on one
version never affects another version's snapshots.

When making intentional help text changes, update all supported versions:
  uv run --python 3.12 pytest tests/snapshots/ --snapshot-update -n 0
  uv run --python 3.13 pytest tests/snapshots/ --snapshot-update -n 0
  uv run --python 3.14 pytest tests/snapshots/ --snapshot-update -n 0

When adding a new Python version to the CI matrix:
  1. Add the version to the matrix in .github/workflows/ci.yml.
  2. uv run --python X.Y pytest tests/snapshots/ --snapshot-update -n 0
  3. Commit both changes together.

The per-version directory routing is implemented in tests/conftest.py via a
pytest_configure hook that sets syrupy's snapshot_dirname before session start.

See also: docs/guides/snapshot-policy.md
"""

import os
import subprocess

import pytest

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
    "db",
]


class TestHelpSnapshots:
    """Snapshot test all --help outputs to catch unintended drift."""

    def test_root_help(self, snapshot):
        """Test root siftd --help output."""
        stdout = run_siftd("--help")
        normalized = stdout.replace(HOME, "~")
        assert normalized == snapshot

    @pytest.mark.parametrize("subcommand", SUBCOMMANDS)
    def test_subcommand_help(self, subcommand, snapshot):
        """Test each subcommand's --help output."""
        stdout = run_siftd(subcommand, "--help")
        normalized = stdout.replace(HOME, "~")
        assert normalized == snapshot
