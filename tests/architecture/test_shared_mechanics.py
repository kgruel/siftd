"""One module in `tests/architecture/` locates the source tree; the rest import it.

The ratchets here are deliberately self-contained — each carries its own
invariant, allowlist, and the argument for why it exists — and that virtue was
being read as licence to re-implement the *mechanics* too. It does not scale:
the three-`.parent` walk from a test module up to `src/siftd` reached five
copies by the time #45 was filed and eight by the time it was picked up, so the
convention was drifting in the direction of more copies while an open issue
described the problem. Prose in `tests/README.md` would have been the same
instrument that had already failed.

This is the enumerable-property form: `architecture/support.py` is the only
module in this directory that may walk up from its own location, which makes
`SRC` and `REPO_ROOT` single-valued by construction rather than by review
attention.

Limits worth naming:

- It is textual, matching the `Path`-of-`__file__` call. A module that reached
  the tree some other way — `os.path.dirname`, an environment variable,
  `importlib` on the installed package — passes this and is just as much a
  second definition. Those spellings have never appeared here; the one that
  has is guarded.
- It says nothing about `literal_text` or `source_files`. A ratchet that
  re-implements either is not caught, because both are small enough that a
  textual signature would be noise. The root is the one with eight copies.
- Bare `__file__` in a message string is untouched (`test_exceptions.py` points
  a reader at its own allowlist that way) — only the wrapped call matters.

ALLOWLIST is shrink-only, and empty. A module that genuinely needs its own root
is the conversation, not the entry.
"""

import re

from architecture.support import REPO_ROOT

# Modules exempt from the rule, relative to tests/architecture/. Shrink-only.
ALLOWLIST: set[str] = set()

# The owner: the module the others import their roots from.
OWNER = "support.py"

_SELF_LOCATING = re.compile(r"Path\(__file__\)")

# Reached through the shared root, not through this module's own location —
# the rule applied to the check itself.
_HERE = REPO_ROOT / "tests" / "architecture"


def test_only_support_locates_the_source_tree():
    offenders = {}
    for path in sorted(_HERE.glob("*.py")):
        rel = path.name
        if rel == OWNER or rel in ALLOWLIST:
            continue
        lines = [
            lineno
            for lineno, line in enumerate(path.read_text().splitlines(), 1)
            if _SELF_LOCATING.search(line)
        ]
        if lines:
            offenders[rel] = lines

    assert not offenders, (
        f"architecture ratchets deriving their own source root: {offenders}. "
        f"`SRC` and `REPO_ROOT` live in `architecture/{OWNER}` so there is one "
        f"answer to where the tree is — import them (`from architecture.support "
        f"import SRC`) rather than re-deriving. Eight copies had accreted by #45."
    )


def test_allowlist_only_shrinks():
    """The ratchet's own guard: entries come out, never in."""
    assert ALLOWLIST == set(), (
        "ALLOWLIST is shrink-only and currently empty. A module that cannot "
        "import its root from support.py needs its reason written here, where "
        "a reviewer sees it in the diff."
    )
