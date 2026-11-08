"""Doctor checks reach what siftd.embeddings re-exports through the package.

The doctor runner runs checks on a thread pool, and the embedding checks import
lazily inside ``run()``. ``siftd.embeddings/__init__`` imports ``.availability``,
so a check importing ``siftd.embeddings.availability`` directly takes the two
module locks in the opposite order from its neighbours. Under load that races
into ``deadlock detected by _ModuleLock('siftd.embeddings.availability')``, and
doctor reports an error and exits 1 (~1% of runs on 3.14 with eight concurrent
doctors; seen in CI on 3.12 and 3.14). Entering through the package gives every
thread the same lock order.

Only submodules the package's ``__init__`` imports can invert the order; heavy
ones it deliberately leaves out (``base``, ``indexer``, ``presets``, ...) stay
importable directly, so the rule reads that set from ``__init__.py``.
"""

import ast

from architecture.support import SRC

CHECKS = SRC / "doctor" / "checks"
PACKAGE_INIT = SRC / "embeddings" / "__init__.py"


def _eager_submodules() -> set[str]:
    """``siftd.embeddings.<x>`` for every ``from .x import`` at the top of the package."""
    tree = ast.parse(PACKAGE_INIT.read_text())
    return {
        f"siftd.embeddings.{node.module}"
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
    }


def test_the_package_still_imports_availability_eagerly():
    """The rule's premise: if this changes, revisit the rule rather than let it pass vacuously."""
    assert "siftd.embeddings.availability" in _eager_submodules()


def test_doctor_checks_import_embeddings_through_the_package():
    eager = _eager_submodules()
    offenders = []
    for path in sorted(CHECKS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = next((a.name for a in node.names if a.name in eager), None)
            if module in eager:
                offenders.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno} {module}")
    assert not offenders, (
        "import these from siftd.embeddings, not the submodule, in doctor checks "
        f"(they run concurrently; see this module's docstring): {offenders}"
    )
