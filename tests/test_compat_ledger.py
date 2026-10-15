"""A deprecation's stated removal version is enforced by the release that reaches it.

Breaking changes ship behind a compat ledger with a stated removal version
(CLAUDE.md, "Release process"). The statement lives where the surface does — a
docstring or config description saying "removed in X.Y.Z" — and until #74 nothing
read it back: the release ritual bumps the version and checks the tree, so a
release could cross X.Y.Z with the surface still in place and nothing would fail.

This is a location claim: it points at text in ``src/siftd`` that promises a
removal by a version ``pyproject.toml`` has already reached. It does not decide
whether the surface is still live; removing it, or restating the promise with a
later version, is what makes it pass. The bump in step 2 of the ``release`` skill
is what trips it, before the tag.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "siftd"

# Whitespace is normalized before matching, so a promise wrapped across docstring
# lines ("will be\n    removed in 0.13.0") is still one match.
_PROMISE = re.compile(r"removed in v?(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)


def _current_version() -> tuple[int, ...]:
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    return tuple(int(part) for part in version.split("."))


def _promises() -> list[tuple[str, tuple[int, ...]]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        text = " ".join(path.read_text().split())
        for m in _PROMISE.finditer(text):
            found.append((path.relative_to(REPO).as_posix(), tuple(int(g) for g in m.groups())))
    return found


def test_no_removal_promise_is_past_due():
    current = _current_version()
    due = sorted(
        f"{path}: removed in {'.'.join(map(str, version))}"
        for path, version in _promises()
        if version <= current
    )
    assert not due, (
        f"pyproject.toml is at {'.'.join(map(str, current))}, and these surfaces promised "
        f"removal by then. Remove them (sweeping docs, config keys and tests), or restate "
        f"the promise with a later version and say why in the changelog:\n" + "\n".join(due)
    )


def test_the_promise_matcher_reads_a_wrapped_docstring():
    wrapped = " ".join("Deprecated: will be\n            removed in 0.13.0.".split())
    assert [m.groups() for m in _PROMISE.finditer(wrapped)] == [("0", "13", "0")]
    assert _PROMISE.search("Deprecated, no effect, removed in 0.13.0: merged content")
    assert _PROMISE.search("accepted through v0.9.x and removed in v0.10.0.")
    assert not _PROMISE.search("removed in a later release")
