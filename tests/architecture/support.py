"""Neutral mechanics shared by the architecture ratchets.

Every ratchet in this directory is an instance of one shape: walk the Python
sources under `src/siftd`, parse each one, look for a property, and report the
offenders against a stable root — with a shrink-only allowlist stating why any
survivor is legitimate. The *property* is what makes each ratchet worth reading
on its own, and it stays in its own module along with the allowlist and the
docstring that argues for it.

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
