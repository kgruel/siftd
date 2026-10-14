"""Neutral mechanics shared by the architecture ratchets.

Every ratchet in this directory is an instance of one shape: walk the Python
sources under `src/siftd`, parse each one, look for a property, and report the
offenders against a stable root — with a shrink-only allowlist stating why any
survivor is legitimate. The *property* is what makes each ratchet worth reading
on its own, and it stays in its own module along with its allowlist — or, where
the survivors are permanent rather than shrinking, its stated carve-outs — and
the docstring that argues for both.

What lives here is only the part that carries no invariant: where the source
tree is, how to enumerate it, and how to read the static text out of a string
node. `Path(__file__)` appears in exactly one place in this directory now, and
`test_shared_mechanics.py` is what keeps it that way — its docstring carries
the count that made the case (#45).

Import it as a top-level package (`tests/` is on `pythonpath`, and this
directory has an `__init__.py`)::

    from architecture.support import SRC, source_files
"""

import ast
import re
from collections.abc import Callable
from functools import cache
from pathlib import Path

# The package under analysis, and the repo root the ratchets render paths
# against. Both spellings are in use and both are correct: a finding about a
# rule internal to the package reads better relative to SRC
# (`storage/sqlite.py`), one a developer has to go open reads better relative
# to the repo (`src/siftd/storage/sqlite.py`). Which root a ratchet displays is
# its own choice; deriving them is not.
REPO_ROOT = Path(__file__).parent.parent.parent
SRC = REPO_ROOT / "src" / "siftd"


def source_files(root: Path = SRC) -> list[Path]:
    """Every Python source under `root`, sorted, `__pycache__` excluded.

    The `__pycache__` filter is inert today and kept for intent. Sorting is
    not cosmetic: several ratchets report offenders in file order, and an
    unsorted `rglob` makes those messages differ run to run.
    """
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def literal_text(node: ast.AST) -> str | None:
    """The static text of a string literal, or None if the node is not one.

    An f-string contributes its constant segments, which is where a copied URI
    keeps its query parameters — `f"file:{p}?mode=ro&immutable=1"` yields
    `file:?mode=ro&immutable=1`, and a SQL statement keeps its keywords.

    An f-string made entirely of interpolations yields `""`, not `None`: the
    node *is* a string literal, it simply contributes no static text. Every
    caller tests the result for truth, so the two are interchangeable at the
    call sites — the distinction is stated here so a future one can rely on it.
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


def docstring_ids(tree: ast.Module) -> set[int]:
    """Node ids of every docstring in `tree` — prose about a property, not a use.

    Only the string-hunting ratchets need this, and they all need the same
    definition: `test_readonly_opens.py` skips docstrings that quote the URI
    markers it counts, and `test_prefix_resolution.py` skips the two that quote
    the SQL predicate it forbids. A call-hunting ratchet reads the tree and
    gets prose-immunity for free (`test_shared_mechanics.py` argues that at
    length) — which is why this is a helper here rather than folded into
    :func:`literal_text`.

    A *docstring* is the first statement of a module, class, or function. Any
    other bare string statement is left visible: it is unreachable prose only
    by convention, and no ratchet has needed the wider skip.
    """
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr):
            ids.add(id(first.value))
    return ids


@cache
def parsed_sources() -> tuple[tuple[str, ast.Module, frozenset[int]], ...]:
    """Every source under `src/siftd` as (module, tree, docstring node ids).

    Cached because this module sweeps the package four times and the parse is
    the expensive third of it. The docstring ids ride along rather than being
    recomputed per sweep: they are `id()` values, so they are only meaningful
    while the tree they came from is alive, and keeping the two in one tuple is
    what guarantees that.
    """
    return tuple(
        (path.relative_to(SRC).as_posix(), tree, frozenset(docstring_ids(tree)))
        for path in source_files()
        if (tree := ast.parse(path.read_text()))
    )


def sites_by_function(
    match: Callable[[ast.AST, frozenset[int]], str | None], *, exclude: str | None = None
) -> dict[tuple[str, str], dict[str, int]]:
    """Where `match` fires across `src/siftd`, keyed by (module, enclosing function).

    Swept over the whole package rather than a named file — a ratchet that
    reads one module can only ever confirm that module, which is what let
    `api/merge.py` be a replacement door nothing here could see.

    Two things about the traversal are load-bearing, and both were learned by
    getting them wrong. Scope is rebound at each nested `FunctionDef` rather
    than walking the whole subtree per function, so a match inside a nested
    function is attributed to exactly one scope. And a matched node **stops the
    descent**: an f-string's constant segments would otherwise each match again
    under the `JoinedStr` that already did, scoring one statement twice.
    `test_readonly_opens.py::_sites` carries the same two rules for the same
    reasons — a third copy of this walker is what #79's review found, and
    consolidating all of them is filed rather than done here.

    `match` returns the label to count under, or None. Taking a callable rather
    than writing the walker twice is not only shorter: the first cut had a call
    version and a literal version, and only the call version had the
    stop-descent rule, so an f-stringed `DELETE FROM conversations` counted as
    two homes.
    """
    sites: dict[tuple[str, str], dict[str, int]] = {}
    for rel, tree, prose in parsed_sources():
        if rel == exclude:
            continue
        counts: dict[str, dict[str, int]] = {}

        def visit(node: ast.AST, scope: str, counts: dict = counts, prose: frozenset = prose) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    visit(child, child.name, counts, prose)
                    continue
                label = match(child, prose)
                if label is not None:
                    per = counts.setdefault(scope, {})
                    per[label] = per.get(label, 0) + 1
                    continue
                visit(child, scope, counts, prose)

        visit(tree, "<module>")
        for scope, per in counts.items():
            sites[(rel, scope)] = per
    return sites


def call_to(names: tuple[str, ...]) -> Callable[[ast.AST, frozenset[int]], str | None]:
    """Match a call to any of `names`, in either spelling: `f()` and `mod.f()`.

    Only the bare spelling was matched until #79, which is a hole precisely
    where it costs the most — the delete guard is a *name* sweep, and
    `sq.delete_conversation(...)` is a live spelling in this repo. A door
    reaching the primitive through its module would carry no bare name and no
    SQL literal, so both halves of that guard would have missed it.

    Matching on `.attr` alone is deliberately loose: it counts an unrelated
    method that happens to share the name, which fails *loudly* into the
    declared population rather than silently out of it. #79's own trigger was
    the opposite trade — a name collision resolved by renaming
    `storage/embeddings.py`'s function, not by teaching this to resolve
    imports.
    """

    def match(node: ast.AST, _prose: frozenset[int]) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        called = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        return called if called in names else None

    return match


def literal_matching(pattern: re.Pattern[str]) -> Callable[[ast.AST, frozenset[int]], str | None]:
    """Match a static string literal against `pattern`, ignoring prose.

    The half that catches a door bypassing the primitive entirely — a raw
    `conn.execute("DELETE FROM conversations ...")` carries no call the sweep
    would recognise. `literal_text` reads through f-strings, so an interpolated
    predicate still contributes its keywords; `docstring_ids` keeps prose about
    the delete from counting as a use, which matters because the docstrings in
    `storage/` quote this exact statement.
    """
    def match(node: ast.AST, prose: frozenset[int]) -> str | None:
        if id(node) in prose:
            return None
        text = literal_text(node)
        if not text:
            return None
        found = pattern.search(text)
        # Labelled by the table, not by the matched text: the label is what
        # `DESTROY_SITES` counts, and two spellings of the same delete in one
        # function are two deletes, not two kinds.
        return "conversations" if found else None

    return match
