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
    # The autouse DB sandbox sets XDG_DATA_HOME to a throwaway dir so no test
    # opens the real database. --help is display-only (it never opens the DB),
    # but it *renders* the default db_path() in the help text — so drop the
    # sandbox override here to keep the rendered default at the canonical
    # ~/.local/share/siftd/siftd.db, which the HOME→~ normalization expects.
    env.pop("XDG_DATA_HOME", None)
    result = subprocess.run(
        ["uv", "run", "siftd", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    # Guard against an argparse error (exit 2, message on stderr, empty stdout)
    # masquerading as a passing empty snapshot — every --help here must succeed.
    assert result.returncode == 0, (
        f"`siftd {' '.join(args)}` exited {result.returncode}: {result.stderr.strip()}"
    )
    # The brand masthead bakes the running version into root --help; normalize it
    # so a version bump doesn't churn the snapshot (this pins help STRUCTURE, not
    # the version number — the same reason HOME is normalized to ~).
    from siftd.cli._common import _get_version

    return result.stdout.replace(f"siftd {_get_version()}", "siftd X.Y.Z")


# All top-level subcommands to test. (path/status/workspaces are NOT here — they
# are db/auth sub-verbs; running them at top level is an argparse error, which the
# run_siftd returncode guard now rejects. Branch sub-verbs are covered below.)
SUBCOMMANDS = [
    "ingest",
    "search",
    "embed",
    "install",
    "register",
    "session-id",
    "tag",
    "id",
    "query",
    "show",
    "report",
    "backfill",
    "config",
    "adapters",
    "copy",
    "doctor",
    "peek",
    "export",
    "db",
]

# A sampling of branch sub-verbs — exercises the help grammar one level down
# (breadcrumb path, the leaf's own groups) the way the formerly-dead top-level
# path/status/workspaces entries never did.
BRANCH_SUBCOMMANDS = [
    ("db", "path"),
    ("db", "stats"),
    ("db", "workspaces"),
    ("auth", "status"),
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

    @pytest.mark.parametrize("parent,sub", BRANCH_SUBCOMMANDS)
    def test_branch_subcommand_help(self, parent, sub, snapshot):
        """Branch sub-verb --help renders through the same grammar one level down."""
        stdout = run_siftd(parent, sub, "--help")
        normalized = stdout.replace(HOME, "~")
        assert normalized == snapshot

    def test_db_restore_help(self, snapshot):
        """siftd db restore --help includes --dry-run."""
        stdout = run_siftd("db", "restore", "--help")
        normalized = stdout.replace(HOME, "~")
        assert normalized == snapshot

    def test_db_receive_help(self, snapshot):
        """siftd db receive --help includes --dry-run."""
        stdout = run_siftd("db", "receive", "--help")
        normalized = stdout.replace(HOME, "~")
        assert normalized == snapshot
