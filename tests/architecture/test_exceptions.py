"""Every custom exception joins the taxonomy or is explicitly allowlisted.

The taxonomy (siftd/errors.py) is the presentation contract: exceptions that
derive from SiftdError render as clean error lines at the CLI backstop and map
to status codes in serve. An exception outside it that escapes a per-command
catch tuple surfaces as a Python traceback — which is how the IndexCompatError
bug happened (raised as plain Exception, matched by no net).

This test is a ratchet, same shape as test_known_violations_ratchet in
test_imports.py: new exceptions must either subclass a taxonomy base or be
added to an allowlist below — a diff a reviewer sees and questions.

PERMANENT entries are carve-outs by design (a traceback IS the right rendering,
or the exception is control flow, never presentation). TRANSITIONAL entries are
pre-taxonomy stragglers, removed as the family migration slices land; this
set must only ever shrink.
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent.parent / "src" / "siftd"

TAXONOMY_BASES = {"SiftdError", "UserInputError", "DriftError"}

# Builtin exception names that mark a class as exception-like when used as a
# base (directly or transitively through other classes defined in src/).
BUILTIN_EXCEPTION_BASES = {
    "Exception",
    "BaseException",
    "ValueError",
    "RuntimeError",
    "TypeError",
    "KeyError",
    "OSError",
    "IOError",
    "LookupError",
    "ArithmeticError",
    "StopIteration",
}

# (relative_path_from_src_siftd, class_name): why it stays out — permanently.
PERMANENT_CARVEOUTS = {
    # Invariant violations signal bugs; a traceback is the correct rendering.
    ("storage/blobs.py", "BlobCollisionError"): "invariant violation (SHA256 collision)",
    ("storage/sqlite.py", "MigrationAssertionError"): "invariant violation (migration assertion)",
    # Control-flow signals: handled (degrade / fall back / surface structurally),
    # never presented as terminal errors.
    ("embeddings/base.py", "EmbeddingTransientError"): "degradable blip; search falls back to FTS",
    ("api/op_spec.py", "MissingOpSpec"): "invariant violation (op registered without wire spec); "
    "also caught as delegation control flow",
    ("serve/client.py", "ServeUnavailable"): "delegation control flow (fall back to local)",
    ("serve/client.py", "ServeRequest4xx"): "delegation control flow (structured 4xx surface)",
}

# Pre-taxonomy stragglers, keyed the same way, valued by the slice that
# migrates them. Shrink-only: entries are deleted as slices land, never added.
TRANSITIONAL = {
    # Slice 2 — embeddings/state family
    ("embeddings/base.py", "EmbeddingError"): "slice 2",
    ("embeddings/base.py", "EmbeddingConfigError"): "slice 2",
    ("embeddings/availability.py", "EmbeddingsNotAvailable"): "slice 2",
    ("embeddings/indexer.py", "IncrementalCompatError"): "slice 2",
    # Slice 3 — user-input family
    ("api/search.py", "EmbeddingsRequiredError"): "slice 3",
    ("api/conversations.py", "AnchorError"): "slice 3",
    ("api/conversations.py", "AnchorOutOfRange"): "slice 3",
    ("api/conversations.py", "AnchorNotFound"): "slice 3",
    ("api/conversations.py", "AnchorPhraseInvalid"): "slice 3",
    ("api/conversations.py", "AmbiguousPrefix"): "slice 3",
    ("api/conversations.py", "QueryError"): "slice 3",
    ("api/ingest.py", "AdapterSelectionError"): "slice 3",
    ("peek/reader.py", "AmbiguousSessionError"): "slice 3",
    # Slice 4 — operation family
    ("credentials.py", "AuthLoginError"): "slice 4",
    ("credentials.py", "TokenRefError"): "slice 4",
    ("api/auth.py", "AuthError"): "slice 4",
    ("api/sync.py", "SyncError"): "slice 4",
    ("api/resources.py", "CopyError"): "slice 4",
    ("api/database.py", "PreflightError"): "slice 4",
    ("adapters/sdk.py", "AdapterParseError"): "slice 4",
}


def _base_names(node: ast.ClassDef) -> list[str]:
    names = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _collect_classes() -> dict[tuple[str, str], list[str]]:
    """Map (relpath, class_name) -> base names, for every class in src/siftd."""
    classes: dict[tuple[str, str], list[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = str(path.relative_to(SRC_ROOT))
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes[(rel, node.name)] = _base_names(node)
    return classes


def _exception_classes(
    classes: dict[tuple[str, str], list[str]],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Split defined classes into (exception-like, taxonomy-member) sets.

    Membership is computed by name over a transitive closure: a class is
    exception-like if any base chain reaches a builtin exception name or a
    taxonomy base; it is a taxonomy member if a chain reaches a taxonomy base.
    Bases are matched by bare name — class names are unique across src/, and
    a false merge would only make the test stricter, not let drift through.
    """
    by_name: dict[str, list[str]] = {}
    for (_, name), bases in classes.items():
        by_name.setdefault(name, []).extend(bases)

    def reaches(name: str, targets: set[str], seen: frozenset = frozenset()) -> bool:
        if name in targets:
            return True
        if name in seen:
            return False
        return any(
            reaches(base, targets, seen | {name}) for base in by_name.get(name, [])
        )

    exception_like = set()
    taxonomy_members = set()
    for key, _ in classes.items():
        _, name = key
        if name in TAXONOMY_BASES:
            continue  # the taxonomy itself
        if reaches(name, BUILTIN_EXCEPTION_BASES | TAXONOMY_BASES):
            exception_like.add(key)
        if reaches(name, TAXONOMY_BASES):
            taxonomy_members.add(key)
    return exception_like, taxonomy_members


def test_exceptions_join_taxonomy_or_allowlist():
    classes = _collect_classes()
    exception_like, taxonomy_members = _exception_classes(classes)
    allowlisted = set(PERMANENT_CARVEOUTS) | set(TRANSITIONAL)

    strays = exception_like - taxonomy_members - allowlisted
    assert not strays, (
        f"Exception classes outside the taxonomy: {sorted(strays)}\n"
        f"Subclass a base from siftd/errors.py (UserInputError or DriftError) "
        f"so the CLI backstop and serve mapping cover it, or add a "
        f"PERMANENT_CARVEOUTS entry in {__file__} with the reason it must "
        f"stay out (invariant violation / control-flow signal)."
    )


def test_allowlist_entries_still_exist():
    """A stale allowlist entry means a migration landed without shrinking the
    ratchet (or a file moved) — clean it up so the ratchet stays honest."""
    classes = _collect_classes()
    _, taxonomy_members = _exception_classes(classes)
    for key in list(PERMANENT_CARVEOUTS) + list(TRANSITIONAL):
        assert key in classes, f"allowlist entry {key} no longer exists"
    migrated = taxonomy_members & set(TRANSITIONAL)
    assert not migrated, (
        f"These joined the taxonomy but are still allowlisted — remove the "
        f"TRANSITIONAL entries: {sorted(migrated)}"
    )
