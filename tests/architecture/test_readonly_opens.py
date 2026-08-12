"""A read-only open derives immutability from the medium. It does not assert it.

Why asserting it is wrong, what it cost, and how the derived open works are all
documented once, at the mechanism: see `storage.sqlite.connect_read_only`. This
file is only the enforcement.

What it enforces is a *routing* rule:

    Every read-only open under `src/siftd/` routes through
    `storage.sqlite.connect_read_only`.

During #42 this file matched the literal `immutable=1` alone, which was the
right marker for blocking a fourth copy of that URI but weaker than the rule the
codebase now has: a hand-rolled `mode=ro` open asserts nothing about
immutability, so it passed, while still being a second definition of the read
contract — one that drifts the moment the helper gains a pragma, a timeout, or a
sidecar policy.

Both markers are scanned, and **counted separately per site**. That separation
is the whole ratchet, not a detail: counting one-per-literal instead would let a
listed site upgrade `?mode=ro` to `?mode=ro&immutable=1` without changing any
number the tests compare — measured, and it made this file weaker than the
version it replaced at both carve-outs. Per-marker counts mean a site may keep
exactly the URIs it is listed for and no others.

Scoping on the URI mode is also what keeps the lists honest. Of the 8
`sqlite3.connect` call sites under `src/siftd/`, 5 are write-mode, in-memory, or
the backup *destination*; a ratchet over all of them would need those 5 listed on
day one, relocating the problem rather than enforcing anything, and conflating
two invariants that have nothing to do with each other. The remaining 3 are the
read-only URI opens enumerated below, and 3 is exactly what OWNER and
PERMANENT_CARVEOUTS total — the lists are the complete population, not a sample.

Neither list is an allowlist in the #42 sense. That one was shrink-only and
reaching empty was its completion signal, which it did. These are permanent, in
the shape `test_exceptions.py` uses for the same distinction. The discipline is
unchanged: an addition is the conversation.

Both lists carry an occurrence *count*, not just a site. Keying on the site alone
would let a second copied URI land inside an already-listed function without
changing anything the tests compare — measured: injecting a duplicate into
`open_database` left both assertions green.

Limits worth naming, in the shape `test_timestamps.py` names its
pass-through-adapter limit:

- It matches the literal string, so a URI assembled at runtime, or received as a
  parameter, is invisible here. Acceptable: the failure mode this guards is a
  read-only URI being written out at a new site, and writing one out carries the
  literal. A caller that merely receives a finished URI is not defining the
  contract.
- It cannot see a read-only open expressed *without* a URI — `sqlite3.connect` on
  a plain path, relying on filesystem permissions.
  `test_imports.py::test_no_sqlite3_connect_outside_storage` covers that
  direction, but only outside `storage/` and `adapters/sdk.py`; inside those two
  it permits everything, and this file is the only check there. That overlap is
  where the unique reach of this ratchet actually lies.
- It reads string *constants* via AST, not the file's text. Of the 8 marker
  literals under `src/siftd/`, 5 are docstrings and the other 3 are the opens
  themselves — so the skip is what makes the enumeration exact, and a reader can
  re-derive both numbers. A site whose only trace is a comment stays invisible;
  there is no such site.
- Sites are keyed by enclosing *function* name, so two same-named functions in
  one module share a row and only their total is checked. Adding an occurrence
  still fails; moving one between the two would not.
- It covers `src/siftd/` only. A test, or a drop-in adapter under
  `~/.config/siftd/adapters/`, opening its own read-only connection is out of
  reach of any static check the repo can run.
"""

import ast
from collections import Counter

from architecture.support import SRC, literal_text, source_files

MARKERS = ("mode=ro", "immutable=1")

# The module that owns the operation; every read-only open routes through it.
# Two URIs: the plain `mode=ro` open, and the `immutable=1` fallback taken only
# when that open's probe raises SQLITE_READONLY/SQLITE_CANTOPEN — a medium no
# writer can reach, where immutability is true rather than assumed. Listed per
# marker, so neither URI can quietly acquire the other's flag.
OWNER: dict[tuple[str, str, str], int] = {
    ("storage/sqlite.py", "connect_read_only", "mode=ro"): 1,
    ("storage/sqlite.py", "connect_read_only", "immutable=1"): 1,
}

# The one read-only open that legitimately does not route through the helper.
# `adapters/README.md` states that adapters never reach into the storage layer,
# and `sdk.py` is the authoring surface for third-party drop-in adapters under
# `~/.config/siftd/adapters/` — routing it through `storage.sqlite` would
# puncture that boundary for every adapter author. It also opens *other tools'*
# databases rather than siftd's, so the concurrent-writer race that motivated #42
# does not apply the same way: nothing in this process writes them. The same
# carve-out and the same reason appear in `test_imports.py`; they must agree.
PERMANENT_CARVEOUTS: dict[tuple[str, str, str], int] = {
    ("adapters/sdk.py", "open_external_db", "mode=ro"): 1,
}


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Node ids of every docstring — prose about the property, not a use of it.

    Five of the eight marker literals under `src/siftd/` are docstrings, and one
    of them sits in a *different* function than the open it describes
    (`_ensure_cache_loaded`), so skipping them is what keeps the enumeration
    honest rather than merely tidy.
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


def _sites() -> Counter[tuple[str, str, str]]:
    """Occurrences per (relative path, enclosing function, marker).

    A literal carrying both markers counts once for each: it asserts both
    properties, and a site listed for one must not silently gain the other.
    """
    found: Counter[tuple[str, str, str]] = Counter()
    for path in source_files():
        tree = ast.parse(path.read_text())
        skip = _docstring_ids(tree)
        rel = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, scope: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                text = literal_text(child) if id(child) not in skip else None
                matched = [m for m in MARKERS if text and m in text]
                if matched:
                    # Count the outermost matching literal and stop descending:
                    # an f-string's constant segments would each match again,
                    # scoring one copied URI twice.
                    for marker in matched:
                        found[(rel, scope, marker)] += 1
                    continue
                visit(child, scope)

        visit(tree, "<module>")
    return found


def test_no_unlisted_read_only_uris():
    """A new read-only open cannot hand-roll its URI without this list changing."""
    listed = OWNER | PERMANENT_CARVEOUTS
    added = {
        site: (count, listed.get(site, 0))
        for site, count in _sites().items()
        if count > listed.get(site, 0)
    }
    assert not added, (
        "Read-only open(s) building their own URI instead of routing through "
        "storage.sqlite.connect_read_only:\n"
        + "\n".join(
            f"  {path} :: {func} — `{marker}` × {found}, {allowed} listed"
            for (path, func, marker), (found, allowed) in sorted(added.items())
        )
        + "\n\nCall `connect_read_only(path)`. It derives immutability from the "
        "medium — a plain `mode=ro` open, falling back to `immutable=1` only "
        "when the probe proves the medium unwritable, and refusing even that "
        "when a sidecar holds state the fallback would drop. A hand-rolled URI "
        "is a second definition of that contract. See the module docstring and "
        "issues #42 and #48."
    )


def test_listed_sites_have_no_stale_entries():
    """A site that stops building its own URI leaves the list, so it stays exact."""
    sites = _sites()
    stale = {
        site: (sites[site], count)
        for site, count in (OWNER | PERMANENT_CARVEOUTS).items()
        if sites[site] < count
    }
    assert not stale, (
        "Listed site(s) no longer build as many read-only URIs — shrink the "
        "entries:\n"
        + "\n".join(
            f"  {path} :: {func} — `{marker}` × {found}, {listed} listed"
            for (path, func, marker), (found, listed) in sorted(stale.items())
        )
    )
