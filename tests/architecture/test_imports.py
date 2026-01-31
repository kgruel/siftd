"""Test import dependency rules to enforce layered architecture.

Rules:
- domain/ must not import storage/, api/, or cli
- storage/ must not import cli
- api/ can import storage/ + domain/
- cli should import api/ (not storage/ directly)
"""

import ast
from pathlib import Path

import pytest

# Layer import rules: module -> forbidden imports
RULES = {
    "domain": {"forbidden": ["storage", "api", "cli"]},
    "storage": {"forbidden": ["cli"]},
    "api": {"forbidden": ["cli"]},
    "cli": {"forbidden": ["storage"]},
}


def get_siftd_imports(file_path: Path) -> list[tuple[int, str]]:
    """Extract siftd.* imports from a Python file.

    Returns list of (line_number, module_path) tuples.
    """
    source = file_path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("siftd."):
                    imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("siftd."):
                imports.append((node.lineno, node.module))

    return imports


def get_layer(module_path: str) -> str | None:
    """Determine which layer a module belongs to.

    Returns layer name (domain, storage, api, cli) or None if not in a layer.
    """
    parts = module_path.split(".")
    if len(parts) < 2:
        return None

    # siftd.domain.* -> domain
    # siftd.storage.* -> storage
    # siftd.api.* -> api
    # siftd.cli or siftd.cli_* -> cli
    second = parts[1]

    if second in ("domain", "storage", "api"):
        return second

    if second == "cli" or second.startswith("cli_"):
        return "cli"

    return None


def check_file_imports(file_path: Path, source_layer: str) -> list[str]:
    """Check a file for import violations.

    Returns list of violation messages.
    """
    rule = RULES.get(source_layer)
    if not rule:
        return []

    forbidden = rule.get("forbidden", [])
    if not forbidden:
        return []

    violations = []
    imports = get_siftd_imports(file_path)

    for line_num, module_path in imports:
        imported_layer = get_layer(module_path)
        if imported_layer in forbidden:
            violations.append(
                f"{file_path}:{line_num}: {source_layer}/ imports {imported_layer}/ "
                f"({module_path})"
            )

    return violations


def collect_python_files(src_dir: Path) -> list[tuple[Path, str]]:
    """Collect Python files with their layer.

    Returns list of (file_path, layer) tuples.
    """
    files = []

    for layer in RULES:
        if layer == "cli":
            # cli.py and cli_*.py are at src/siftd/
            for pattern in ["cli.py", "cli_*.py"]:
                for file_path in src_dir.glob(pattern):
                    files.append((file_path, layer))
        else:
            # domain/, storage/, api/ are subdirectories
            layer_dir = src_dir / layer
            if layer_dir.exists():
                for file_path in layer_dir.rglob("*.py"):
                    files.append((file_path, layer))

    return files


def test_import_rules():
    """Verify that all modules follow import dependency rules."""
    src_dir = Path(__file__).parent.parent / "src" / "siftd"

    all_violations = []
    files = collect_python_files(src_dir)

    for file_path, layer in files:
        violations = check_file_imports(file_path, layer)
        all_violations.extend(violations)

    if all_violations:
        msg = "Import violations found:\n" + "\n".join(all_violations)
        pytest.fail(msg)


def test_domain_is_pure():
    """Verify domain/ has no external dependencies on other siftd layers."""
    src_dir = Path(__file__).parent.parent / "src" / "siftd"
    domain_dir = src_dir / "domain"

    if not domain_dir.exists():
        pytest.skip("No domain/ directory")

    violations = []
    for file_path in domain_dir.rglob("*.py"):
        for line_num, module_path in get_siftd_imports(file_path):
            layer = get_layer(module_path)
            if layer and layer != "domain":
                violations.append(
                    f"{file_path}:{line_num}: domain imports {layer}/ ({module_path})"
                )

    if violations:
        msg = "Domain layer purity violations:\n" + "\n".join(violations)
        pytest.fail(msg)


def find_sqlite3_connect_calls(file_path: Path) -> list[tuple[int, str]]:
    """Find sqlite3.connect() calls in a Python file.

    Returns list of (line_number, call_text) tuples.
    Excludes :memory: connections which don't touch the filesystem.
    """
    source = file_path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Match sqlite3.connect(...)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
            ):
                # Allow :memory: connections (no filesystem access)
                if node.args and isinstance(node.args[0], ast.Constant):
                    if node.args[0].value == ":memory:":
                        continue
                calls.append((node.lineno, "sqlite3.connect()"))
    return calls


def test_no_sqlite3_connect_outside_storage():
    """Verify sqlite3.connect() is only used in storage/.

    All DB connections should go through open_database() or open_embeddings_db()
    to ensure consistent read-only handling and avoid WAL/SHM file creation.
    """
    src_dir = Path(__file__).parent.parent / "src" / "siftd"

    violations = []
    storage_dir = src_dir / "storage"

    for py_file in src_dir.rglob("*.py"):
        # Allow sqlite3.connect() inside storage/
        if storage_dir in py_file.parents or py_file.parent == storage_dir:
            continue

        for line_num, call_text in find_sqlite3_connect_calls(py_file):
            rel_path = py_file.relative_to(src_dir.parent.parent)
            violations.append(f"{rel_path}:{line_num}: {call_text}")

    if violations:
        msg = (
            "sqlite3.connect() found outside storage/. "
            "Use open_database() or open_embeddings_db() instead:\n"
            + "\n".join(violations)
        )
        pytest.fail(msg)
