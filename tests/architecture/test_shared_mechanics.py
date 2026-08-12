"""One module in `tests/architecture/` locates the source tree; the rest import it.

The ratchets here are deliberately self-contained — each carries its own
invariant, allowlist, and the argument for why it exists — and that virtue was
being read as licence to re-implement the *mechanics* too. It does not scale.
`Path(__file__).parent.parent.parent / "src" / "siftd"` stood at seven sites
across six modules when #45 was filed, and the issue's own careful enumeration
found five of the modules — it missed `test_contracts.py`, which had carried a
copy since February. Hours later a seventh module landed an eighth site
(`test_timestamp_converters.py`, from the #32 arc). Both halves say the same
thing: a convention this small is below the resolution of review attention,
which is what makes prose in `tests/README.md` the wrong instrument for it.

This is the enumerable-property form: `architecture/support.py` is the only
module in this directory that may locate itself from `__file__`, which makes
`SRC` and `REPO_ROOT` single-valued by construction rather than by review
attention.

**The check is on the AST, not on the text, and that is the load-bearing
choice.** A regex over source lines matches this module's own docstring — the
paragraph above is a working example — so a textual version would force every
future ratchet to spell `Path(__file__)` around its own matcher. That is an
unwritten sub-convention, which is the exact failure mode #45 was filed about.
Reading the call out of the tree makes prose invisible for free, with no
docstring-skipping machinery (`test_readonly_opens.py` needs `_docstring_ids`
only because the property it hunts is a *string*, not a call).

Limits worth naming:

- It matches the call `Path(__file__)`, including `pathlib.Path(__file__)`. A
  module that reached the tree some other way — `os.path.dirname`, an
  environment variable, `importlib` on the installed package — passes this and
  is just as much a second definition. Those spellings have never appeared
  here; the one that has is guarded.
- It says nothing about `literal_text` or `source_files`. A ratchet that
  re-implements either is not caught. The root is the one that reached eight
  sites, and a check is worth what its population justifies.
- Bare `__file__` is untouched — `test_exceptions.py` points a reader at its
  own allowlist that way. Only the wrapped call is a location.

ALLOWLIST is shrink-only, and empty. A module that genuinely needs its own root
is the conversation, not the entry.
"""

import ast

from architecture.support import REPO_ROOT, source_files

# Modules exempt from the rule, relative to tests/architecture/. Shrink-only.
ALLOWLIST: set[str] = set()

# The owner: the module the others import their roots from.
OWNER = "support.py"

# Reached through the shared root, not through this module's own location —
# the rule applied to the check itself.
_HERE = REPO_ROOT / "tests" / "architecture"


def _self_locating_calls(tree: ast.Module) -> list[int]:
    """Lines calling `Path(__file__)`, in either the bare or `pathlib.` spelling."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "Path":
            continue
        if any(isinstance(arg, ast.Name) and arg.id == "__file__" for arg in node.args):
            lines.append(node.lineno)
    return sorted(lines)


def test_only_support_locates_the_source_tree():
    offenders = {}
    for path in source_files(_HERE):
        if path.name == OWNER or path.name in ALLOWLIST:
            continue
        if lines := _self_locating_calls(ast.parse(path.read_text())):
            offenders[path.name] = lines

    assert not offenders, (
        f"architecture ratchets deriving their own source root: {offenders}. "
        f"`SRC` and `REPO_ROOT` live in `architecture/{OWNER}` so there is one "
        f"answer to where the tree is — import them (`from architecture.support "
        f"import SRC`) rather than re-deriving (#45)."
    )


def test_allowlist_only_shrinks():
    """The ratchet's own guard: entries come out, never in."""
    assert ALLOWLIST == set(), (
        "ALLOWLIST is shrink-only and currently empty. A module that cannot "
        "import its root from support.py needs its reason written here, where "
        "a reviewer sees it in the diff."
    )
