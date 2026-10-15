"""Every tracked path is a legal filename on every platform siftd ships to.

The sdist carries the tracked tree, and the wheel carries ``plugin/``. A colon
in a filename (``plugin/commands/siftd:peek.md``) cannot be written on Windows,
and uv 0.13.1's build rejects it outright, so v0.12.3's publish failed at
``uv build`` with the files unchanged since March. The plugin's command names
come from their frontmatter, so nothing needs the character in the path.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Characters Windows forbids in a path component (control characters aside).
_RESERVED = re.compile(r'[<>:"|?*\\]')


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout
    return [p for p in out.decode().split("\0") if p]


def test_no_tracked_path_uses_a_reserved_character():
    tracked = _tracked()
    assert "pyproject.toml" in tracked, "positive control: git ls-files saw the repo"
    offenders = sorted(p for p in tracked if _RESERVED.search(p))
    assert not offenders, (
        f"paths with a character Windows forbids (and uv's build rejects): {offenders}. "
        f"Rename them; a plugin command's name comes from its frontmatter, not its file."
    )
