"""A read-only open derives immutability from the medium. It does not assert it.

Why asserting it is wrong, what it cost, and how the derived open works are all
documented once, at the mechanism: see `storage.sqlite.connect_read_only`. This
file is only the enforcement.

What it enforces is a *routing* rule:

    Every read-only open under `src/siftd/` routes through
    `storage.sqlite.connect_read_only`.

That is stronger and simpler than the rule this file enforced during #42 ("no
one writes `immutable=1`"), and #48 retargeted it once the rewire made it
reachable. The old marker could be satisfied by writing the same defect a
different way: a hand-rolled `mode=ro` open asserts nothing about immutability,
so it passed the old check while still being a second definition of the read
contract — one that drifts the moment `connect_read_only` gains a pragma, a
timeout, or a sidecar policy. Keying on the URI *mode* catches both spellings.
It found a live one: `backup_database` built its own `mode=ro` source URI, and
a backup is exactly the read that must not silently copy a stale snapshot.

Both markers are scanned, not just `mode=ro`. `immutable=1` implies read-only on
its own, so a URI carrying only that flag would evade a `mode=ro`-only
enumeration while being the more dangerous of the two. A node matching both
counts once.

Scoping on the mode is also what keeps the list honest. `src/siftd/` has 12
`sqlite3.connect` call sites, 8 of them legitimate write-mode, in-memory, or
backup-destination opens; a ratchet over *all* of them would need those 8 listed
on day one, relocating the problem rather than enforcing anything, and
conflating two invariants that have nothing to do with each other.

Neither list below is an allowlist in the #42 sense — that one was shrink-only
and reached empty, which was its completion signal. These two are permanent
carve-outs, each with its reason recorded beside it. The discipline is the same:
an addition is the conversation.

Both lists carry an occurrence *count*, not just a site. Keying on the site
alone would let a second copied URI land inside an already-listed function
without changing anything the tests compare — measured: injecting a duplicate
into `open_database` left both assertions green. Since the guarded failure mode
is a URI being written out at a new site, and the most convenient place to write
one is next to an existing one, the count is what makes the ratchet hold.

Limits worth naming, in the shape `test_timestamps.py` names its
pass-through-adapter limit:

- It matches the literal string, so a URI assembled at runtime, or received as a
  parameter, is invisible here. Acceptable: the failure mode this guards is a
  read-only URI being written out at a new site, and writing one out carries the
  literal. A caller that merely receives a finished URI is not defining the
  contract.
- It cannot see a read-only open expressed *without* a URI — `sqlite3.connect`
  on a plain path, relying on filesystem permissions. That direction is covered
  by `test_imports.py::test_no_sqlite3_connect_outside_storage`; the two
  ratchets are complementary and neither subsumes the other.
- It reads string *constants* via AST, not the file's text, so prose mentions in
  docstrings and comments are correctly ignored — but a site whose only trace is
  a comment is likewise invisible. There is no such site.
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

# The mechanism itself: the plain `mode=ro` open, and the `immutable=1` fallback
# taken only when that open's probe raises SQLITE_READONLY/SQLITE_CANTOPEN — a
# medium no writer can reach, where immutability is true rather than assumed.
# Two literals, one per URI it builds. This is the site every other read-only
# open routes through, so it is the one that must build them.
DERIVED_OPEN: dict[tuple[str, str], int] = {
    ("storage/sqlite.py", "connect_read_only"): 2,
}

# The one read-only open that legitimately does not route through the helper.
# `adapters/README.md` states that adapters never reach into the storage layer,
# and `sdk.py` is the authoring surface for third-party drop-in adapters under
# `~/.config/siftd/adapters/` — routing it through `storage.sqlite` would
# puncture that boundary for every adapter author. It also opens *other tools'*
# databases rather than siftd's, so the concurrent-writer race that motivated
# #42 does not apply the same way: nothing in this process writes them.
ADAPTER_BOUNDARY: dict[tuple[str, str], int] = {
    ("adapters/sdk.py", "open_external_db"): 1,
}


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Node ids of every docstring — prose about the property, not a use of it.

    Most prose mentions of these markers are docstrings, and one of them sits in
    a *different* function than the open it describes (`_ensure_cache_loaded`),
    so skipping them is what keeps the enumeration honest rather than merely
    tidy.
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
    """Read-only-URI occurrences per (relative path, enclosing function)."""
    found: Counter[tuple[str, str]] = Counter()
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
                if text and any(marker in text for marker in MARKERS):
                    # Count the outermost matching literal once and stop. An
                    # f-string's constant segments would each match again
                    # otherwise, so one copied URI would score two — and a URI
                    # carrying *both* markers is still one open, not two.
                    found[(rel, scope)] += 1
                    continue
                visit(child, scope)

        visit(tree, "<module>")
    return found


def test_no_unlisted_read_only_uris():
    """A new read-only open cannot hand-roll its URI without this list changing."""
    listed = DERIVED_OPEN | ADAPTER_BOUNDARY
    added = {
        site: (count, listed.get(site, 0))
        for site, count in _sites().items()
        if count > listed.get(site, 0)
    }
    assert not added, (
        "Read-only open(s) building their own URI instead of routing through "
        "storage.sqlite.connect_read_only:\n"
        + "\n".join(
            f"  {path} :: {func} — {found} occurrence(s), {allowed} listed"
            for (path, func), (found, allowed) in sorted(added.items())
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
        for site, count in (DERIVED_OPEN | ADAPTER_BOUNDARY).items()
        if sites[site] < count
    }
    assert not stale, (
        "Listed site(s) no longer build as many read-only URIs — shrink the "
        "entries:\n"
        + "\n".join(
            f"  {path} :: {func} — {found} occurrence(s), {listed} listed"
            for (path, func), (found, listed) in sorted(stale.items())
        )
    )
