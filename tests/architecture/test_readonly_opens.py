"""A read-only open derives immutability from the medium. It does not assert it.

`mode=ro&immutable=1` tells SQLite the file cannot change, so it omits all
locking and change detection. siftd cannot honour that: `ingest`, `serve`, and
any second CLI invocation write the same file from another process. SQLite
calls the result undefined, and #38 measured it reaching users two ways, both
silent — an immutable reader ignores the `-wal` outright and answers from the
last checkpoint, and a concurrent checkpoint rewrites main-file pages under a
reader with no change detection, producing truncated scans and
`integrity_check` corruption reports against a healthy database.

The property reached five sites because each new read-only open copied the
nearest URI. That is a copy-paste failure mode, so this is the enumerable-
property form of the invariant rather than review attention: every literal
`immutable=1` under `src/siftd/` is enumerated and must be a known site.

ALLOWLIST is shrink-only, and it is the completion signal for #42: one entry
comes out per read-only open rewired through the derived helper, and the issue
is done when the list is empty. The one legitimate assertion —
`_connect_read_only`'s fallback, reached only *after* a plain `mode=ro` probe
has proved the medium unwritable — is tracked separately as DERIVED_FALLBACK
so that emptiness stays meaningful.

Limits worth naming, in the shape `test_timestamps.py` names its
pass-through-adapter limit:

- It matches the literal string, so a URI built by concatenation at runtime, or
  received as a parameter, is invisible here. Acceptable: the failure mode this
  guards is literally copy-paste of the URI, and copy-paste carries the literal.
- It reads string *constants* via AST, not the file's text, so the eight prose
  mentions in docstrings and comments are correctly ignored — but a site whose
  only trace is a comment is likewise invisible. There is no such site.
- It covers `src/siftd/` only. A test or a drop-in adapter under
  `~/.config/siftd/adapters/` opening its own immutable connection is out of
  reach of any static check the repo can run.
"""

import ast
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent.parent / "src" / "siftd"

MARKER = "immutable=1"

# Read-only opens that assert immutability instead of deriving it, keyed by
# (path relative to src/siftd, enclosing function). Shrink-only — an entry is
# removed when #42 rewires that site, and there is no legitimate reason to add
# one, so an addition is the conversation.
ALLOWLIST: set[tuple[str, str]] = {
    ("storage/sqlite.py", "_peek_user_version"),
    ("storage/sqlite.py", "open_database"),
    ("storage/embeddings.py", "open_embeddings_db"),
}

# The one permanent use: the fallback taken only when the plain `mode=ro` probe
# raises SQLITE_READONLY/SQLITE_CANTOPEN, i.e. on a medium no writer can reach,
# where `immutable=1` is true rather than assumed. Deliberately not an ALLOWLIST
# row — "ALLOWLIST is empty" has to remain #42's completion test.
DERIVED_FALLBACK: tuple[str, str] = ("doctor/checks/__init__.py", "_connect_read_only")


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
    """Node ids of every docstring — prose about the property, not a use of it."""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _sites() -> set[tuple[str, str]]:
    """Every (relative path, enclosing function) that builds an immutable URI."""
    found: set[tuple[str, str]] = set()
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
                    found.add((rel, scope))
                visit(child, scope)

        visit(tree, "<module>")
    return found


def test_no_unlisted_immutable_opens():
    """A new read-only open cannot assert immutability without this list changing."""
    unlisted = _sites() - ALLOWLIST - {DERIVED_FALLBACK}
    assert not unlisted, (
        "Read-only open(s) asserting immutability instead of deriving it:\n"
        + "\n".join(f"  {path} :: {func}" for path, func in sorted(unlisted))
        + "\n\nUse the derived open (plain `mode=ro`, falling back to `immutable=1` "
        "only when the probe proves the medium unwritable) rather than copying an "
        "`immutable=1` URI — see the module docstring and issue #42."
    )


def test_allowlist_has_no_stale_entries():
    """A rewired site leaves the list, so 'empty' means every site is derived."""
    sites = _sites()
    stale = (ALLOWLIST | {DERIVED_FALLBACK}) - sites
    assert not stale, (
        "Listed site(s) no longer build an immutable URI — delete the entries:\n"
        + "\n".join(f"  {path} :: {func}" for path, func in sorted(stale))
    )
