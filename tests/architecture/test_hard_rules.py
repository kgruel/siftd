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
    return Path(__file__).parent.parent.parent / "src" / "siftd"


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

            # Replace $var and :var placeholders with NULL for syntax check
            normalized = re.sub(r"\$\w+", "NULL", sql_content)
            normalized = re.sub(r":\w+", "NULL", normalized)

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
        """Every output format module has the required interface."""
        from siftd.output.format_registry import load_all_formats

        formats = load_all_formats()
        violations = []
        for plugin in formats:
            m = plugin.module
            if not hasattr(m, "render_detail") or not callable(m.render_detail):
                violations.append(f"'{plugin.name}' formatter missing render_detail()")
        if violations:
            pytest.fail("Formatter violations:\n" + "\n".join(violations))

    def test_unknown_format_errors_cleanly(self):
        """Unknown format name returns None from get_format."""
        from siftd.output.format_registry import get_format

        result = get_format("nonexistent_format_xyz")
        assert result is None


# =============================================================================
# 5. Raw SQL in CLI Modules
# =============================================================================


# =============================================================================
# 5. Serve Route Boundary (v1 JSON API <-> UI HTML fragments)
# =============================================================================


class TestServeRouteBoundary:
    """JSON API routes (/v1/) and UI HTML routes (/ui/) must not cross-reference.

    Rationale: The output layer (html_fmt) must be route-agnostic — it receives
    detail_base as a context parameter rather than hardcoding paths. JSON API
    routes must not reference /ui/ paths and vice versa.
    """

    def test_output_formatters_no_hardcoded_routes(self, src_dir):
        """Output formatters must not contain /ui/ or /v1/ route paths."""
        import re

        output_dir = src_dir / "output"
        route_re = re.compile(r'["\'](/(?:ui|v1)/[^"\']*)["\']')

        violations = []
        for py_file in output_dir.rglob("*.py"):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                # Skip comments and docstrings (heuristic: lines with # or triple-quote context)
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                for m in route_re.finditer(line):
                    # Allow in docstrings/comments (crude: if line has >>> or e.g.)
                    if "e.g." in line or ">>>" in line or "example" in line.lower():
                        continue
                    rel = py_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel}:{i}: hardcoded route {m.group(1)!r}")

        if violations:
            pytest.fail(
                "Output formatters must not hardcode serve routes.\n"
                "Use detail_base context parameter instead:\n"
                + "\n".join(violations)
            )

    def test_json_routes_no_ui_references(self, src_dir):
        """JSON API routes (routes.py) must not reference /ui/ paths."""
        import re

        routes_file = src_dir / "serve" / "routes.py"
        if not routes_file.exists():
            pytest.skip("No serve/routes.py")

        ui_re = re.compile(r'["\']/ui/')
        violations = []
        for i, line in enumerate(routes_file.read_text().splitlines(), 1):
            if ui_re.search(line):
                violations.append(f"serve/routes.py:{i}: references /ui/ path")

        if violations:
            pytest.fail(
                "JSON API routes must not reference UI paths:\n"
                + "\n".join(violations)
            )

    def test_html_routes_no_v1_references(self, src_dir):
        """HTML UI routes (html_routes.py) must not reference /v1/ paths."""
        import re

        html_routes_file = src_dir / "serve" / "html_routes.py"
        if not html_routes_file.exists():
            pytest.skip("No serve/html_routes.py")

        v1_re = re.compile(r'["\']/v1/')
        violations = []
        for i, line in enumerate(html_routes_file.read_text().splitlines(), 1):
            if v1_re.search(line):
                violations.append(f"serve/html_routes.py:{i}: references /v1/ path")

        if violations:
            pytest.fail(
                "HTML UI routes must not reference JSON API paths:\n"
                + "\n".join(violations)
            )

    def test_html_routes_use_api_layer(self, src_dir):
        """HTML routes must import from api/output, not storage/peek/search directly.

        Rationale: html_routes.py is a thin controller — it should call
        API functions, not reach into storage or internal modules. This
        prevents the UI from accumulating direct DB access that bypasses
        the API's validation, connection management, and abstraction.
        """
        import ast

        html_routes = src_dir / "serve" / "html_routes.py"
        if not html_routes.exists():
            pytest.skip("No serve/html_routes.py")

        # html_routes may only import from these groups
        allowed = {"api", "output", "domain", "utilities", "serve"}
        # Forbidden top-level modules (anything not in allowed groups)
        forbidden = {"storage", "peek", "search", "embeddings", "adapters",
                     "ingestion", "doctor", "content"}

        tree = ast.parse(html_routes.read_text())
        violations = []

        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("siftd."):
                        module = alias.name

            if module and module.startswith("siftd."):
                top = module.split(".")[1]
                if top in forbidden:
                    violations.append(
                        f"html_routes.py imports {module} ({top} layer)"
                    )

        if violations:
            pytest.fail(
                "HTML routes must go through the API layer, not import "
                "storage/peek/search directly:\n"
                + "\n".join(violations)
            )


# =============================================================================
# 6. Raw SQL in CLI Modules
# =============================================================================


_SQL_KEYWORDS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "PRAGMA",
    "WITH",
    "CREATE",
    "DROP",
    "ALTER",
)


def _extract_sql_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return "".join(parts) if parts else None
    return None


def find_sql_execute_calls(file_path: Path) -> list[tuple[int, str]]:
    """Find conn.execute(...) calls with SQL literals in a Python file."""
    import re

    source = file_path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    sql_re = re.compile(rf"\\b({'|'.join(_SQL_KEYWORDS)})\\b", re.IGNORECASE)
    lines = source.splitlines()
    results = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr not in {"execute", "executemany", "executescript"}:
                continue
            if not node.args:
                continue
            sql_literal = _extract_sql_literal(node.args[0])
            if not sql_literal or not sql_re.search(sql_literal):
                continue
            line = node.lineno
            if 0 < line <= len(lines) and "arch: allow-sql" in lines[line - 1]:
                continue
            snippet = sql_literal.strip().splitlines()[0][:80]
            results.append((line, snippet))
    return results


# =============================================================================
# 6. Serve Routes Must Use Formatters, Not dataclasses.asdict
# =============================================================================


def _find_dataclasses_asdict_calls(file_path: Path) -> list[int]:
    """Find lines with dataclasses.asdict() calls."""
    source = file_path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()
    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "asdict":
                line = node.lineno
                if 0 < line <= len(lines) and "arch: allow-asdict" in lines[line - 1]:
                    continue
                results.append(line)
    return results


class TestServeRoutesSerialization:
    """Serve routes must use json_fmt formatters, not dataclasses.asdict.

    Rationale: The json_fmt module is the canonical JSON serialization.
    Using dataclasses.asdict in routes creates a second, divergent shape
    that doesn't match --json CLI output or external consumers.

    Suppress with ``# arch: allow-asdict`` on the offending line.
    """

    def test_no_asdict_in_serve_routes(self, src_dir):
        """serve/routes.py should not use dataclasses.asdict."""
        routes_file = src_dir / "serve" / "routes.py"
        if not routes_file.exists():
            pytest.skip("serve/routes.py not found")

        violations = _find_dataclasses_asdict_calls(routes_file)
        if violations:
            lines = [f"  line {ln}" for ln in violations]
            pytest.fail(
                "serve/routes.py uses dataclasses.asdict() — "
                "use json_fmt.render_* instead:\n" + "\n".join(lines)
            )


def test_no_raw_sql_in_cli_modules(src_dir):
    """CLI modules should not execute raw SQL directly."""
    violations = []

    for py_file in src_dir.rglob("cli*.py"):
        for line_num, snippet in find_sql_execute_calls(py_file):
            rel_path = py_file.relative_to(src_dir.parent.parent)
            violations.append(f"{rel_path}:{line_num}: execute({snippet!r})")

    if violations:
        pytest.fail(
            "Raw SQL execution found in CLI modules. "
            "Use API/storage helpers instead:\n"
            + "\n".join(violations)
        )
