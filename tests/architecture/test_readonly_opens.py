"""A read-only open derives immutability from the medium. It does not assert it.

Why asserting it is wrong, what it cost, and how the derived open works are all
documented once, at the mechanism: see `storage.sqlite.connect_read_only`. This
file is only the enforcement.

What it enforces is a *copy-paste* failure mode. The property reached five
sites because each new read-only open copied the nearest URI — no site was
reasoned about independently, which is why review attention could never have
held the line. So the enforceable form is an enumeration of producers: every
literal `immutable=1` under `src/siftd/` must be a known site.

ALLOWLIST is shrink-only, and it was the completion signal for #42: one entry
came out per read-only open rewired through the derived helper. **It is now
empty, and must stay that way** — every read-only open in siftd routes through
`storage.sqlite.connect_read_only`. The one legitimate assertion is that
helper's own fallback, reached only *after* a plain `mode=ro` probe has proved
the medium unwritable; it is tracked separately as DERIVED_FALLBACK so that
emptiness stays meaningful.

Both lists carry an occurrence *count*, not just a site. Keying on the site
alone would let a second copied URI land inside an already-listed function
without changing anything the tests compare — measured: injecting a duplicate
into `open_database` left both assertions green. Since the guarded failure mode
is a URI being copied, and the most convenient place to copy one to is next to
an existing one, the count is what makes the ratchet hold.

Limits worth naming, in the shape `test_timestamps.py` names its
pass-through-adapter limit:

- It matches the literal string, so a URI built by concatenation at runtime, or
  received as a parameter, is invisible here. Acceptable: the failure mode this
  guards is literally copy-paste of the URI, and copy-paste carries the literal.
- It reads string *constants* via AST, not the file's text, so the eight prose
  mentions in docstrings and comments are correctly ignored — but a site whose
  only trace is a comment is likewise invisible. There is no such site.
- Sites are keyed by enclosing *function* name, so two same-named functions in
  one module share a row and only their total is checked. Adding an occurrence
  still fails; moving one between the two would not.
- It covers `src/siftd/` only. A test or a drop-in adapter under
  `~/.config/siftd/adapters/` opening its own immutable connection is out of
  reach of any static check the repo can run.
"""

import ast
from collections import Counter
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent.parent / "src" / "siftd"

MARKER = "immutable=1"

# Read-only opens that assert immutability instead of deriving it, keyed by
# (path relative to src/siftd, enclosing function) → occurrence count.
# Shrink-only — an entry is removed when #42 rewires that site, and there is no
# legitimate reason to add one, so an addition is the conversation.
ALLOWLIST: dict[tuple[str, str], int] = {}

# The one permanent use: the fallback taken only when the plain `mode=ro` probe
# raises SQLITE_READONLY/SQLITE_CANTOPEN, i.e. on a medium no writer can reach,
# where `immutable=1` is true rather than assumed. Deliberately not an ALLOWLIST
# row — "ALLOWLIST is empty" has to remain #42's completion test.
DERIVED_FALLBACK: dict[tuple[str, str], int] = {
    ("storage/sqlite.py", "connect_read_only"): 1,
}


def _literal_text(node: ast.AST) -> str | None:
    """The static text of a string literal, or None if the node is not one.

    An f-string contributes its constant segments, which is where a copied URI
    keeps its query parameters — `f"file:{p}?mode=ro&immutable=1"` yields
    `file:?mode=ro&immutable=1`.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Node ids of every docstring — prose about the property, not a use of it.

    Five of the eight prose mentions are docstrings, and one of them sits in a
    *different* function than the open it describes (`_ensure_cache_loaded`), so
    skipping them is what keeps the enumeration honest rather than merely tidy.
    """
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr):
            ids.add(id(first.value))
    return ids


def _sites() -> Counter[tuple[str, str]]:
    """Immutable-URI occurrences per (relative path, enclosing function)."""
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        skip = _docstring_ids(tree)
        rel = path.relative_to(SRC_DIR).as_posix()

        def visit(node: ast.AST, scope: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                text = _literal_text(child) if id(child) not in skip else None
                if text and MARKER in text:
                    # Count the outermost matching literal and stop: an
                    # f-string's constant segments each match again otherwise,
                    # so one copied URI would score two.
                    found[(rel, scope)] += 1
                    continue
                visit(child, scope)

        visit(tree, "<module>")
    return found


def test_no_unlisted_immutable_opens():
    """A new read-only open cannot assert immutability without this list changing."""
    listed = ALLOWLIST | DERIVED_FALLBACK
    added = {
        site: (count, listed.get(site, 0))
        for site, count in _sites().items()
        if count > listed.get(site, 0)
    }
    assert not added, (
        "Read-only open(s) asserting immutability instead of deriving it:\n"
        + "\n".join(
            f"  {path} :: {func} — {found} occurrence(s), {allowed} listed"
            for (path, func), (found, allowed) in sorted(added.items())
        )
        + "\n\nUse the derived open (plain `mode=ro`, falling back to `immutable=1` "
        "only when the probe proves the medium unwritable) rather than copying an "
        "`immutable=1` URI — see the module docstring and issue #42."
    )


def test_allowlist_has_no_stale_entries():
    """A rewired site leaves the list, so 'empty' means every site is derived."""
    sites = _sites()
    stale = {
        site: (sites[site], count)
        for site, count in (ALLOWLIST | DERIVED_FALLBACK).items()
        if sites[site] < count
    }
    assert not stale, (
        "Listed site(s) no longer build as many immutable URIs — shrink the "
        "entries:\n"
        + "\n".join(
            f"  {path} :: {func} — {found} occurrence(s), {listed} listed"
            for (path, func), (found, listed) in sorted(stale.items())
        )
    )
