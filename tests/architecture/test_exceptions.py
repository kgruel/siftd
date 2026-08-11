"""Every custom exception joins the taxonomy or is explicitly allowlisted.

The taxonomy (siftd/errors.py) is the presentation contract: exceptions that
derive from SiftdError render as clean error lines at the CLI backstop and map
to status codes in serve. An exception outside it that escapes a per-command
catch tuple surfaces as a Python traceback — which is how the IndexCompatError
bug happened (raised as plain Exception, matched by no net).

This test is a ratchet, same shape as test_known_violations_ratchet in
test_imports.py: new exceptions must either subclass a taxonomy base or be
added to an allowlist below — a diff a reviewer sees and questions.

It is a tripwire, not a proof: static analysis can't see dynamically created
classes (``type(...)``) and resolves bases by bare name, so an aliased builtin
(``from builtins import ValueError as V``) or a third-party exception base
evades the closure. The naming-convention check below is the second wire —
anything named ``*Error``/``*Exception``/``*NotAvailable`` must classify as
exception-like or be allowlisted, which catches those shapes in practice.

PERMANENT entries are carve-outs by design (a traceback IS the right rendering,
or the exception is control flow, never presentation). TRANSITIONAL entries are
pre-taxonomy stragglers, removed as the family migration slices land; this
set must only ever shrink.
"""

import ast
import builtins
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent.parent / "src" / "siftd"

TAXONOMY_BASES = {"SiftdError", "UserInputError", "DriftError"}

# Builtin exception names that mark a class as exception-like when used as a
# base (directly or transitively through other classes defined in src/).
# Derived, not hand-listed: a hand-written set silently misses bases like
# PermissionError or AssertionError and the ratchet stops ratcheting.
BUILTIN_EXCEPTION_BASES = {
    name
    for name, obj in vars(builtins).items()
    if isinstance(obj, type) and issubclass(obj, BaseException)
}

# Classes NAMED like exceptions that aren't exceptions at all — value types
# whose rename would break a public contract. Exempt from the naming-convention
# tripwire only; if one ever grows an exception base, the closure test takes over.
NON_EXCEPTION_NAMED = {
    ("adapters/sdk.py", "ParseError"): "dataclass record of a collected parse failure "
    "(line_number/error/raw_line), never raised; drop-in adapter SDK public surface",
}

# (relative_path_from_src_siftd, class_name): why it stays out — permanently.
PERMANENT_CARVEOUTS = {
    # Invariant violations signal bugs; a traceback is the correct rendering.
    ("storage/blobs.py", "BlobCollisionError"): "invariant violation (SHA256 collision)",
    ("storage/sqlite.py", "MigrationAssertionError"): "invariant violation (migration assertion)",
    # Control-flow signals: handled (degrade / fall back / surface structurally),
    # never presented as terminal errors.
    ("embeddings/base.py", "EmbeddingTransientError"): "degradable blip; search falls back to FTS",
    ("embeddings/base.py", "EmbeddingError"): "domain grouping base, not a taxonomy member — "
    "EmbeddingTransientError subclasses it and must not transitively reach SiftdError "
    "(would break degrade-to-fts); EmbeddingConfigError joins DriftError directly instead",
    ("api/op_spec.py", "MissingOpSpec"): "invariant violation (op registered without wire spec); "
    "also caught as delegation control flow",
    ("serve/client.py", "ServeUnavailable"): "delegation control flow (fall back to local)",
    ("serve/client.py", "ServeRequest4xx"): "delegation control flow (structured 4xx surface)",
    ("cli/data.py", "_FixNotApplied"): "control flow inside doctor fix's step runner: "
    "a step that declined to run is neither a failure nor a fix, and is reported as "
    "'not applied' rather than raised at the user",
}

# Pre-taxonomy stragglers, keyed the same way, valued by the slice that
# migrates them. Shrink-only: entries are deleted as slices land, never added.
TRANSITIONAL = {}


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

    Membership is computed over a transitive closure. The first hop always
    uses the definition's OWN base list — a same-named class in another
    module must not vouch for this one. Deeper hops fall back to matching
    bases by bare name (the AST can't resolve imports); the duplicate-name
    guard below keeps that fallback sound for exception classes.
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
    for (rel, name), own_bases in classes.items():
        if name in TAXONOMY_BASES:
            continue  # the taxonomy itself
        seen = frozenset({name})
        if any(
            reaches(base, BUILTIN_EXCEPTION_BASES | TAXONOMY_BASES, seen)
            for base in own_bases
        ):
            exception_like.add((rel, name))
        if any(reaches(base, TAXONOMY_BASES, seen) for base in own_bases):
            taxonomy_members.add((rel, name))
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


def test_exception_named_classes_are_classified():
    """The closure can't resolve aliased-builtin or third-party bases (bare-name
    AST matching), so a class like ``NewError(V)`` where ``V`` aliases
    ValueError would silently skip the taxonomy check. Convention is the
    backstop: a class whose name says it's an exception must either classify
    as exception-like (and thus face the taxonomy test) or be allowlisted."""
    classes = _collect_classes()
    exception_like, _ = _exception_classes(classes)
    allowlisted = set(PERMANENT_CARVEOUTS) | set(TRANSITIONAL)
    suspects = {
        key
        for key in classes
        if key[1].endswith(("Error", "Exception", "NotAvailable"))
        and key not in exception_like
        and key not in allowlisted
        and key not in NON_EXCEPTION_NAMED
        and key[1] not in TAXONOMY_BASES
    }
    assert not suspects, (
        f"Classes named like exceptions but not classified as exception-like "
        f"(aliased/third-party base?): {sorted(suspects)} — give them a "
        f"resolvable exception base or allowlist them with a reason."
    )


def test_exception_class_names_unique():
    """Deep hops of the closure resolve bases by bare name; two exception
    classes sharing a name across modules would let one definition's ancestry
    vouch for the other's. Keep exception class names unique so the by-name
    fallback stays sound."""
    classes = _collect_classes()
    exception_like, _ = _exception_classes(classes)
    names: dict[str, list[str]] = {}
    for rel, name in exception_like:
        names.setdefault(name, []).append(rel)
    dupes = {name: paths for name, paths in names.items() if len(paths) > 1}
    assert not dupes, f"duplicate exception class names across modules: {dupes}"


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
