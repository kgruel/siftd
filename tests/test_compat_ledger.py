"""The compat ledger's removal versions are enforced by the release that reaches them.

Breaking changes ship behind a compat ledger with a stated removal version
(CLAUDE.md, "Release process"). The ledger is ``siftd.api.compat``: one
``*_REMOVAL`` constant per deprecated surface. Until #74 nothing read it back —
the release ritual bumps the version and checks the tree, so a release could
cross a removal version with the surface still in place and nothing would fail.

Two properties, both location claims:

- **due** — no ledger entry names a version ``pyproject.toml`` has reached. The
  bump in step 2 of the ``release`` skill is what trips it, before the tag.
- **recorded** — every "removed in X.Y.Z" in ``src/siftd`` (a docstring, a help
  string, a config description) names a version some ledger entry holds. The
  version is spelled out in several places a user reads; this keeps a stray copy
  from promising a date the ledger does not, and keeps a promise from being made
  outside the ledger at all.

Neither decides whether a surface is still live. Removing it with its ledger
entry, or restating the entry with a later version, is what makes it pass.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import siftd.api.compat as compat

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "siftd"

# Whitespace is normalized before matching, so a promise wrapped across docstring
# lines ("will be\n    removed in 0.13.0") is still one match.
_PROMISE = re.compile(r"removed in v?(\d+\.\d+\.\d+)", re.IGNORECASE)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _current_version() -> tuple[int, ...]:
    return _version(tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"])


def _ledger() -> dict[str, str]:
    return {name: value for name, value in vars(compat).items() if name.endswith("_REMOVAL")}


def _promises() -> list[tuple[str, str]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        text = " ".join(path.read_text().split())
        found += [(path.relative_to(REPO).as_posix(), m[1]) for m in _PROMISE.finditer(text)]
    return found


def test_no_ledger_entry_is_due():
    current = _current_version()
    due = sorted(f"{name} = {version!r}" for name, version in _ledger().items()
                 if _version(version) <= current)
    assert not due, (
        f"pyproject.toml is at {'.'.join(map(str, current))}, which these siftd.api.compat "
        f"entries name as their removal version. Remove each surface with its entry "
        f"(sweeping docs, config keys and tests), or restate it with a later version and "
        f"say why in the changelog:\n" + "\n".join(due)
    )


def test_every_removal_promise_is_in_the_ledger():
    held = set(_ledger().values())
    stray = sorted(f"{path}: removed in {version}" for path, version in _promises()
                   if version not in held)
    assert not stray, (
        f"these promise a removal version no siftd.api.compat entry holds ({sorted(held)}). "
        f"Correct the copy, or record the surface in the ledger:\n" + "\n".join(stray)
    )


def test_the_scanners_see_what_they_check():
    """Positive controls: a scan that finds nothing passes both tests above."""
    assert {"REBUILD_FTS_REMOVAL", "DEBUG_IDS_REMOVAL"} <= set(_ledger())
    assert ("src/siftd/api/merge.py", compat.REBUILD_FTS_REMOVAL) in _promises()

    wrapped = " ".join("Deprecated: will be\n            removed in 0.13.0.".split())
    assert [m[1] for m in _PROMISE.finditer(wrapped)] == ["0.13.0"]
    assert _PROMISE.search("Deprecated, no effect (removed in 0.13.0)")
    assert _PROMISE.search("accepted through v0.9.x and removed in v0.10.0.")
    assert not _PROMISE.search("removed in a later release")
