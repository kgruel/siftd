"""Static code analysis tests for architectural invariants.

These tests analyze source code without executing it. They complement
test_imports.py (layer boundaries) with additional structural checks.
"""

import ast
import sqlite3
from pathlib import Path

import pytest


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def src_dir():
    return Path(__file__).parent.parent / "src" / "siftd"


# =============================================================================
# 1. stderr vs stdout Hygiene
# =============================================================================


def find_print_calls_with_pattern(file_path: Path, pattern: str) -> list[tuple[int, bool]]:
    """Find print() calls containing pattern, return (line, uses_stderr)."""
    source = file_path.read_text()
    tree = ast.parse(source)

    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # Match print(...)
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                # Check if any string arg contains pattern
                has_pattern = False
                for arg in node.args:
                    if isinstance(arg, (ast.Constant, ast.JoinedStr)):
                        arg_str = ast.unparse(arg)
                        if pattern in arg_str:
                            has_pattern = True
                            break

                if has_pattern:
                    # Check for file=sys.stderr
                    uses_stderr = False
                    for kw in node.keywords:
                        if kw.arg == "file":
                            if isinstance(kw.value, ast.Attribute) and kw.value.attr == "stderr":
                                uses_stderr = True
                    results.append((node.lineno, uses_stderr))

    return results


class TestStderrHygiene:
    """Warnings and tips must go to stderr, not stdout.

    Rationale: CLI output is often piped/parsed. Mixing warnings into
    stdout breaks downstream tooling.

    Rule: print() calls containing 'Tip:', 'Warning:' must use
    file=sys.stderr (or use a logging function that goes to stderr).
    """

    def test_tips_use_stderr(self, src_dir):
        """print() calls with 'Tip:' must use file=sys.stderr."""
        violations = []

        for py_file in src_dir.rglob("*.py"):
            for line_num, uses_stderr in find_print_calls_with_pattern(py_file, "Tip:"):
                if not uses_stderr:
                    rel_path = py_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel_path}:{line_num}: print('Tip:...') without stderr")

        if violations:
            pytest.fail("Tip messages must go to stderr:\n" + "\n".join(violations))

    def test_warnings_use_stderr(self, src_dir):
        """print() calls with 'Warning:' must use file=sys.stderr."""
        violations = []

        for py_file in src_dir.rglob("*.py"):
            for line_num, uses_stderr in find_print_calls_with_pattern(py_file, "Warning:"):
                if not uses_stderr:
                    rel_path = py_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel_path}:{line_num}: print('Warning:...') without stderr")

        if violations:
            pytest.fail("Warning messages must go to stderr:\n" + "\n".join(violations))


# =============================================================================
# 2. Query File Validation (Bundled)
# =============================================================================


class TestBundledQueries:
    """Built-in queries must have valid SQL syntax.

    Rationale: Built-in queries ship with the package. Syntax errors
    are release blockers.

    Note: Drop-in queries are validated by `siftd doctor` at runtime.
    """

    def test_builtin_queries_valid_sql(self, src_dir, tmp_path):
        """All .sql files in package have valid syntax."""
        import re

        from siftd.storage.sqlite import create_database

        queries_dir = src_dir / "builtin_queries"
        if not queries_dir.exists():
            pytest.skip("No builtin_queries directory")

        # Create temporary DB with schema for validation
        db_path = tmp_path / "schema_test.db"
        conn = create_database(db_path)

        violations = []

        for sql_file in queries_dir.glob("*.sql"):
            sql_content = sql_file.read_text()

            # Replace $var placeholders with NULL for syntax check
            normalized = re.sub(r"\$\w+", "NULL", sql_content)

            # Split into individual statements and validate each
            statements = [s.strip() for s in normalized.split(";") if s.strip()]
            for i, stmt in enumerate(statements, 1):
                # Skip comment-only blocks
                if all(line.strip().startswith("--") or not line.strip() for line in stmt.split("\n")):
                    continue

                # Use EXPLAIN to validate syntax (requires schema)
                try:
                    conn.execute(f"EXPLAIN {stmt}")
                except sqlite3.Error as e:
                    rel_path = sql_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel_path} (statement {i}): {e}")

        conn.close()

        if violations:
            pytest.fail("Invalid SQL in built-in queries:\n" + "\n".join(violations))


# =============================================================================
# 3. Adapter Interface Compliance (Built-in)
# =============================================================================


class TestBuiltinAdapters:
    """Built-in adapters must implement the required interface.

    Rationale: Built-in adapters ship with the package. Interface
    violations are release blockers.

    Note: Drop-in adapters are validated by `siftd doctor` at runtime.
    """

    def test_all_builtin_adapters_valid(self):
        """All built-in adapters pass validate_adapter()."""
        from siftd.adapters.registry import load_builtin_adapters
        from siftd.adapters.validation import validate_adapter

        violations = []

        for plugin in load_builtin_adapters():
            error = validate_adapter(plugin.module, origin=plugin.name)
            if error:
                violations.append(error)

        if violations:
            pytest.fail("Built-in adapter violations:\n" + "\n".join(violations))


# =============================================================================
# 4. Formatter Registration Validity
# =============================================================================


class TestFormatterRegistry:
    """All registered formatters must exist and be callable.

    Rationale: Format registration is static. Invalid registrations
    should fail fast.
    """

    def test_all_formatters_exist(self):
        """Every format name maps to a callable formatter."""
        from siftd.output.registry import load_builtin_factories

        factories = load_builtin_factories()
        violations = []

        for name, factory in factories.items():
            if not callable(factory):
                violations.append(f"'{name}' is not callable")
                continue
            try:
                instance = factory()
                if not hasattr(instance, "format") or not callable(instance.format):
                    violations.append(f"'{name}' formatter missing format() method")
            except Exception as e:
                violations.append(f"'{name}' factory raised: {e}")

        if violations:
            pytest.fail("Formatter registration violations:\n" + "\n".join(violations))

    def test_unknown_format_errors_cleanly(self):
        """--format unknown gives helpful error (returns None)."""
        from siftd.output.registry import get_formatter

        result = get_formatter("nonexistent_format_xyz")
        # Registry returns None for unknown formats, which select_formatter
        # handles by falling through to built-in selection
        assert result is None
