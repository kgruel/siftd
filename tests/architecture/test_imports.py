"""Test import dependency rules to enforce layered architecture.

Uses a declarative dependency manifest to keep architecture rules explicit
and in sync with the codebase.
"""

import ast
from pathlib import Path

import pytest

# Architecture groups (layered, lowest → highest):
# - domain: pure data models (no internal dependencies)
# - utilities: shared helpers (paths, ids, config, git, plugin discovery, math)
# - content: content filters + built-in query templates
# - storage: SQLite access and migrations
# - embeddings: vector index & similarity operations
# - adapters: log parsers for external tools
# - ingestion: ingest/backfill pipeline coordination
# - peek: live session scanning/inspection
# - search: query API over storage + embeddings
# - output: result formatting/printing
# - doctor: diagnostics and validation
# - api: public programmatic API surface
# - cli: command-line interface entrypoints
ALLOWED_DEPS: dict[str, list[str]] = {
    "domain": [],
    "utilities": [],
    "content": [],
    "storage": ["domain", "content", "utilities"],
    "embeddings": ["domain", "storage", "utilities"],
    "adapters": ["domain", "content", "utilities"],
    "ingestion": ["domain", "storage", "adapters", "content", "utilities"],
    "peek": ["domain", "adapters", "utilities"],
    "search": ["domain", "storage", "embeddings", "peek", "utilities"],
    "output": ["domain", "storage", "search", "utilities", "content"],
    "doctor": ["domain", "storage", "adapters", "embeddings", "output", "utilities"],
    "api": [
        "domain",
        "storage",
        "search",
        "peek",
        "doctor",
        "ingestion",
        "embeddings",
        "content",
        "output",
        "adapters",
        "utilities",
    ],
    "cli": ["api", "output", "utilities"],
}

# Map top-level siftd modules to architecture groups.
# CLI modules are handled via prefix in group_for_module().
MODULE_GROUPS: dict[str, str] = {
    "domain": "domain",
    "storage": "storage",
    "api": "api",
    "search": "search",
    "embeddings": "embeddings",
    "peek": "peek",
    "doctor": "doctor",
    "ingestion": "ingestion",
    "adapters": "adapters",
    "content": "content",
    "output": "output",
    "builtin_queries": "content",
    "backfill": "ingestion",
    "config": "utilities",
    "paths": "utilities",
    "ids": "utilities",
    "math": "utilities",
    "model_names": "utilities",
    "git": "utilities",
    "plugin_discovery": "utilities",
    "__init__": "api",
}

# Known violations pending refactor.
# Format: (relative_path_from_src_siftd, imported_group)
KNOWN_VIOLATIONS = {
    ("adapters/claude_code.py", "peek"),  # TYPE_CHECKING on peek types
    ("adapters/codex_cli.py", "peek"),  # TYPE_CHECKING on peek types
    ("adapters/gemini_cli.py", "peek"),  # TYPE_CHECKING on peek types
    ("adapters/sdk.py", "peek"),  # TYPE_CHECKING on peek types
    ("cli_data.py", "adapters"),  # CLI entrypoint loads adapters directly
    ("cli_data.py", "ingestion"),  # CLI entrypoint calls ingestion/backfill
    ("cli_meta.py", "embeddings"),  # CLI entrypoint reads embeddings status
    ("cli_peek.py", "peek"),  # CLI entrypoint needs peek error type
    ("cli_search.py", "embeddings"),  # CLI entrypoint uses embeddings utilities
    ("cli_search.py", "search"),  # CLI entrypoint calls search module directly
    ("storage/embeddings.py", "search"),  # storage depends on search scoring types
}


def get_siftd_imports(file_path: Path) -> list[str]:
    """Extract siftd.* imports from a Python file.

    Returns list of module paths.
    """
    source = file_path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("siftd."):
                    imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("siftd."):
                imports.append(node.module)

    return imports


def module_name_from_path(file_path: Path, src_dir: Path) -> str:
    """Return full module path (siftd.*) for a source file."""
    rel = file_path.relative_to(src_dir)
    parts = rel.with_suffix("").parts
    return "siftd." + ".".join(parts)


def group_for_module(module_path: str) -> str | None:
    """Return the architecture group for a siftd module path."""
    if not module_path.startswith("siftd"):
        return None

    if module_path == "siftd":
        return "api"

    parts = module_path.split(".")
    if len(parts) < 2:
        return None

    top = parts[1]
    if top == "cli" or top.startswith("cli_"):
        return "cli"

    return MODULE_GROUPS.get(top)


def collect_python_files(src_dir: Path) -> list[Path]:
    """Collect all Python files under src/siftd."""
    return sorted(
        py_file
        for py_file in src_dir.rglob("*.py")
        if "__pycache__" not in py_file.parts
    )


def test_import_rules():
    """Verify that all modules follow import dependency rules."""
    src_dir = Path(__file__).parent.parent.parent / "src" / "siftd"

    ungrouped_files: list[str] = []
    ungrouped_imports: list[str] = []
    violations: list[str] = []

    for file_path in collect_python_files(src_dir):
        module_path = module_name_from_path(file_path, src_dir)
        source_group = group_for_module(module_path)
        if source_group is None:
            rel_path = file_path.relative_to(src_dir)
            ungrouped_files.append(f"{rel_path} ({module_path})")
            continue

        allowed_targets = set(ALLOWED_DEPS.get(source_group, [])) | {source_group}

        for imported_module in get_siftd_imports(file_path):
            target_group = group_for_module(imported_module)
            if target_group is None:
                rel_path = file_path.relative_to(src_dir)
                ungrouped_imports.append(f"{rel_path} imports {imported_module}")
                continue

            if target_group in allowed_targets:
                continue

            rel_path = file_path.relative_to(src_dir)
            if (str(rel_path), target_group) in KNOWN_VIOLATIONS:
                continue

            violations.append(
                f"{source_group} ({rel_path}) cannot import {target_group} ({imported_module})"
            )

    if ungrouped_files or ungrouped_imports or violations:
        parts = []
        if ungrouped_files:
            parts.append("Ungrouped modules:\n" + "\n".join(sorted(ungrouped_files)))
        if ungrouped_imports:
            parts.append(
                "Ungrouped imports:\n" + "\n".join(sorted(ungrouped_imports))
            )
        if violations:
            parts.append("Import violations:\n" + "\n".join(sorted(violations)))
        pytest.fail("\n\n".join(parts))


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
    src_dir = Path(__file__).parent.parent.parent / "src" / "siftd"

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
