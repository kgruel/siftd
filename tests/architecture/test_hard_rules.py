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
        """HTML UI routes (html_routes.py) must not reference /v1/ paths.

        Operation path= fields are excluded — those are API endpoint
        identifiers for serve delegation, not URL references in HTML output.
        """
        import re

        html_routes_file = src_dir / "serve" / "html_routes.py"
        if not html_routes_file.exists():
            pytest.skip("No serve/html_routes.py")

        v1_re = re.compile(r'["\']/v1/')
        # Operation(path="/api/v1/...") is serve delegation, not HTML output
        op_path_re = re.compile(r'path\s*=\s*["\']|path\s*=\s*f["\']')
        violations = []
        for i, line in enumerate(html_routes_file.read_text().splitlines(), 1):
            if v1_re.search(line) and not op_path_re.search(line):
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

    @staticmethod
    def _find_forbidden_imports(
        src_dir, *, subdirs: tuple[str, ...], forbidden_prefix: str, suppress_comment: str,
    ) -> list[str]:
        """Scan subdirs for imports matching forbidden_prefix.

        Returns a list of "rel/path.py:line: imports module" strings.
        Lines containing suppress_comment are excluded.
        """
        import ast as _ast

        violations = []
        for subdir in subdirs:
            pkg = src_dir / subdir
            if not pkg.exists():
                continue
            for py_file in pkg.rglob("*.py"):
                source = py_file.read_text()
                lines = source.splitlines()
                try:
                    tree = _ast.parse(source)
                except SyntaxError:
                    continue
                for node in _ast.walk(tree):
                    module = None
                    if isinstance(node, _ast.ImportFrom) and node.module:
                        module = node.module
                    elif isinstance(node, _ast.Import):
                        for alias in node.names:
                            if alias.name.startswith(forbidden_prefix):
                                module = alias.name

                    if module and (module == forbidden_prefix or module.startswith(forbidden_prefix + ".")):
                        line = node.lineno
                        if 0 < line <= len(lines) and suppress_comment in lines[line - 1]:
                            continue
                        rel = py_file.relative_to(src_dir.parent.parent)
                        violations.append(f"{rel}:{line}: imports {module}")
        return violations

    def test_cli_and_serve_no_direct_search_import(self, src_dir):
        """CLI and serve modules must not import siftd.search directly.

        Rationale: siftd.search is an internal module with lower-level
        functions (MMR, temporal weight, candidate resolution). CLI and
        serve must go through siftd.api.search, which is the public
        boundary with safety behaviors (retry, candidate cap, re-sort).

        Suppress with ``# arch: allow-search`` on the import line.
        """
        violations = self._find_forbidden_imports(
            src_dir,
            subdirs=("cli", "serve"),
            forbidden_prefix="siftd.search",
            suppress_comment="arch: allow-search",
        )
        if violations:
            pytest.fail(
                "CLI/serve modules must import from siftd.api.search, "
                "not siftd.search directly:\n"
                + "\n".join(violations)
            )

    def test_cli_no_direct_storage_import(self, src_dir):
        """CLI modules must not import siftd.storage directly.

        Rationale: The API layer (siftd.api) owns connection lifecycle,
        transactions, and query composition. CLI modules that reach into
        storage bypass validation, retry logic, and the Operation IR
        pipeline.

        Suppress with ``# arch: allow-storage`` on the import line.
        """
        violations = self._find_forbidden_imports(
            src_dir,
            subdirs=("cli",),
            forbidden_prefix="siftd.storage",
            suppress_comment="arch: allow-storage",
        )
        if violations:
            pytest.fail(
                "CLI modules must import from siftd.api, "
                "not siftd.storage directly:\n"
                + "\n".join(violations)
            )

    def test_serve_no_direct_storage_import(self, src_dir):
        """Serve routes must not import siftd.storage directly.

        Rationale: Same as CLI — the API layer owns connection lifecycle
        and query composition. Serve routes that open databases or call
        storage functions directly create a parallel access path that
        bypasses the API's transaction management.

        Suppress with ``# arch: allow-storage`` on the import line.
        """
        violations = self._find_forbidden_imports(
            src_dir,
            subdirs=("serve",),
            forbidden_prefix="siftd.storage",
            suppress_comment="arch: allow-storage",
        )
        if violations:
            pytest.fail(
                "Serve modules must import from siftd.api, "
                "not siftd.storage directly:\n"
                + "\n".join(violations)
            )

    def test_cli_no_direct_embeddings_import(self, src_dir):
        """CLI modules must not import siftd.embeddings directly.

        Rationale: Embeddings is an optional extra with heavyweight
        dependencies (numpy). CLI modules should use siftd.api for
        embedding operations. Direct imports create coupling to the
        embeddings internals and make it harder to gate availability
        checks in one place.

        Suppress with ``# arch: allow-embeddings`` on the import line.
        """
        violations = self._find_forbidden_imports(
            src_dir,
            subdirs=("cli",),
            forbidden_prefix="siftd.embeddings",
            suppress_comment="arch: allow-embeddings",
        )
        if violations:
            pytest.fail(
                "CLI modules must import from siftd.api, "
                "not siftd.embeddings directly:\n"
                + "\n".join(violations)
            )

    def test_package_root_no_direct_storage_import(self, src_dir):
        """src/siftd/__init__.py must not import siftd.storage directly.

        Rationale: The package root is the public surface (``import siftd``).
        External consumers doing ``from siftd import X`` should always go
        through the API layer, not reach into storage. This mirrors the
        CLI/serve boundary and keeps lifecycle ownership in one place.

        Suppress with ``# arch: allow-storage`` on the import line.
        """
        root_init = src_dir / "__init__.py"
        if not root_init.exists():
            pytest.skip("src/siftd/__init__.py not found")

        source = root_init.read_text()
        lines = source.splitlines()
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("siftd.storage"):
                        module = alias.name
            if module and (module == "siftd.storage" or module.startswith("siftd.storage.")):
                line = node.lineno
                if 0 < line <= len(lines) and "arch: allow-storage" in lines[line - 1]:
                    continue
                violations.append(f"__init__.py:{line}: imports {module}")
        if violations:
            pytest.fail(
                "src/siftd/__init__.py must re-export from siftd.api, "
                "not siftd.storage directly:\n"
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
    """Find lines with asdict() calls — both dataclasses.asdict(x) and bare asdict(x)."""
    source = file_path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        matched = False
        if isinstance(node.func, ast.Attribute) and node.func.attr == "asdict":
            matched = True
        elif isinstance(node.func, ast.Name) and node.func.id == "asdict":
            matched = True
        if not matched:
            continue
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

    def test_asdict_matcher_catches_both_call_forms(self, tmp_path):
        """Regression: matcher must catch bare asdict(x) as well as dataclasses.asdict(x)."""
        sample = tmp_path / "sample.py"
        sample.write_text(
            "import dataclasses\n"
            "from dataclasses import asdict\n"
            "def a(x): return dataclasses.asdict(x)\n"  # attr form
            "def b(x): return asdict(x)\n"  # bare form
            "def c(x): return asdict(x)  # arch: allow-asdict\n"  # bare, suppressed
        )
        violations = _find_dataclasses_asdict_calls(sample)
        assert violations == [3, 4], violations

    def test_serve_dispatch_maps_missing_embeddings_to_501(self, src_dir):
        """Serve must expose missing optional embeddings as 501, not generic 500."""
        routes_file = src_dir / "serve" / "routes.py"
        if not routes_file.exists():
            pytest.skip("serve/routes.py not found")

        source = routes_file.read_text()
        tree = ast.parse(source)
        dispatch = next(
            (
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
            ),
            None,
        )
        if dispatch is None:
            pytest.fail("serve/routes.py must define _dispatch")

        dispatch_source = ast.get_source_segment(source, dispatch) or ""
        if "EmbeddingsNotAvailable" not in dispatch_source or "status_code=501" not in dispatch_source:
            pytest.fail(
                "serve/routes.py:_dispatch must map EmbeddingsNotAvailable "
                "to HTTP 501 before the generic 500 handler"
            )


class TestOptionalExtraBoundaries:
    """Optional extras must fail through stable availability gates."""

    CONDITIONAL_EMBEDDINGS_EXPORTS = {
        "EmbeddingBackend",
        "get_backend",
        "invalidate_backend_cache",
        "IndexStats",
        "build_embeddings_index",
        "SCHEMA_VERSION",
    }

    def test_internal_code_does_not_import_conditional_embeddings_exports(self, src_dir):
        """Internal code must not import heavy conditional exports from siftd.embeddings.

        Rationale: siftd.embeddings always exports availability helpers, but
        backend/index symbols only exist when [embed] dependencies are importable.
        Importing those symbols from the package root makes optional absence show
        up as raw ImportError instead of EmbeddingsNotAvailable. Use
        require_embeddings()/embeddings_available() first, then import concrete
        submodules such as siftd.embeddings.base or siftd.embeddings.indexer.
        """
        violations = []
        allowed_files = {src_dir / "embeddings" / "__init__.py"}

        for py_file in src_dir.rglob("*.py"):
            if py_file in allowed_files:
                continue
            source = py_file.read_text()
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "siftd.embeddings":
                    bad_names = sorted(
                        alias.name for alias in node.names
                        if alias.name in self.CONDITIONAL_EMBEDDINGS_EXPORTS
                    )
                    if bad_names:
                        rel_path = py_file.relative_to(src_dir.parent.parent)
                        violations.append(f"{rel_path}:{node.lineno}: {', '.join(bad_names)}")

        if violations:
            pytest.fail(
                "Do not import conditional [embed] exports from siftd.embeddings package root. "
                "Use availability helpers plus concrete submodules instead:\n"
                + "\n".join(violations)
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


# =============================================================================
# 7. Schema Stability
# =============================================================================


class TestSchemaStability:
    """Database schema produced by open_database must match expected structure.

    Rationale: Schema changes affect migrations, queries, and adapters.
    Breaking changes to table/column names must be deliberate. These tests
    catch accidental renames, drops, or missing migration steps.
    """

    @pytest.fixture()
    def db(self, tmp_path):
        """Create a fresh database with full migration chain applied."""
        from siftd.storage.sqlite import open_database

        db_path = tmp_path / "schema_test.db"
        conn = open_database(db_path)
        yield conn
        conn.close()

    def _table_names(self, db) -> set[str]:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {r[0] for r in rows}

    def _virtual_table_names(self, db) -> set[str]:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%VIRTUAL%'"
        ).fetchall()
        # FTS5 creates shadow tables (*_content, *_data, etc); only return the root
        return {r[0] for r in rows if not any(r[0].endswith(s) for s in ("_content", "_data", "_idx", "_docsize", "_config"))}

    def _columns_of(self, db, table: str) -> set[str]:
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}

    # -- Tables ---------------------------------------------------------------

    # Core tables from schema.sql
    EXPECTED_TABLES = {
        # Vocabulary
        "harnesses",
        "models",
        "providers",
        "tools",
        "tool_aliases",
        "pricing",
        "workspaces",
        # Core
        "conversations",
        # Content-addressable storage
        "content_blobs",
        # Tags
        "tags",
        # Operational
        "ingested_files",
        # Migration-ensured
        "conversation_stats",
        "usage_by_conv_model",  # keystone usage rollup (schema v9); conversation_stats' source
        "active_sessions",
        "pending_tags",
        "conversation_owners",
        "sync_inbox",
        # FTS5 virtual table (shows as type='table' in sqlite_master)
        "content_fts",
        # Polymorphic event tables (schema v4+, legacy tables dropped in v6)
        "events",
        "event_response",
        "event_tool_call",
        "event_content",
        "attributes",
        "tag_assignments",
        "tag_pins",
        "workspace_pins",
        "search_events",
        "search_opens",
    }

    def test_expected_tables_exist(self, db):
        """All expected tables must be present after open_database."""
        actual = self._table_names(db)
        missing = self.EXPECTED_TABLES - actual
        if missing:
            pytest.fail(f"Missing tables: {sorted(missing)}")

    def test_no_unexpected_tables_without_review(self, db):
        """New tables should be added to EXPECTED_TABLES to track them.

        If this test fails, a migration added a table. Add it to
        EXPECTED_TABLES above so future renames are caught.
        """
        actual = self._table_names(db)
        # Exclude FTS shadow tables (content_fts_*)
        filtered = {t for t in actual if not t.startswith("content_fts_")}
        unexpected = filtered - self.EXPECTED_TABLES
        if unexpected:
            pytest.fail(
                f"Untracked tables found: {sorted(unexpected)}. "
                "Add them to TestSchemaStability.EXPECTED_TABLES."
            )

    def test_fts5_virtual_table_exists(self, db):
        """The FTS5 full-text search index must exist."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
        ).fetchall()
        assert len(rows) == 1, "content_fts virtual table not found"

    # -- Key columns on critical tables ---------------------------------------

    CRITICAL_COLUMNS = {
        "conversations": {"id", "external_id", "harness_id", "workspace_id", "started_at", "branch"},
        "events": {"id", "kind", "conversation_id", "parent_id", "external_id", "timestamp"},
        "event_response": {"event_id", "model_id", "provider_id", "input_tokens", "output_tokens"},
        "event_tool_call": {"event_id", "tool_id", "input", "result_hash", "status"},
        "event_content": {"id", "event_id", "block_index", "block_type", "content"},
        "content_blobs": {"hash", "content", "ref_count", "created_at"},
        "tags": {"id", "name", "created_at"},
        "ingested_files": {"id", "path", "file_hash", "harness_id", "conversation_id", "error", "file_mtime", "file_size"},
        "models": {"id", "raw_name", "name", "family", "variant"},
    }

    def test_critical_columns_exist(self, db):
        """Key columns on critical tables must be present."""
        violations = []
        for table, expected_cols in self.CRITICAL_COLUMNS.items():
            actual_cols = self._columns_of(db, table)
            missing = expected_cols - actual_cols
            if missing:
                violations.append(f"{table}: missing {sorted(missing)}")
        if violations:
            pytest.fail("Missing columns on critical tables:\n" + "\n".join(violations))


# =============================================================================
# 8. Dependency Direction Rules
# =============================================================================


def _extract_siftd_imports(file_path: Path) -> list[tuple[int, str]]:
    """Extract (line_number, module_name) for all siftd imports in a file."""
    source = file_path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("siftd."):
            results.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("siftd."):
                    results.append((node.lineno, alias.name))
    return results


class TestDependencyDirection:
    """Enforce that dependency arrows point in the right direction.

    These tests catch architectural inversions that create cycles
    or coupling between layers that should be independent.
    """

    def test_api_does_not_import_serialization(self, src_dir):
        """API layer must not import from serialization.

        Rationale: Serialization imports API types (correct direction).
        API importing serialization creates a cycle. If API needs to
        serialize, the caller should do it.

        Suppress with ``# arch: allow-serialization`` on the import line.
        """
        api_dir = src_dir / "api"
        violations = []

        for py_file in api_dir.rglob("*.py"):
            source_lines = py_file.read_text().splitlines()
            for line_num, module in _extract_siftd_imports(py_file):
                if module.startswith("siftd.serialization"):
                    if 0 < line_num <= len(source_lines) and "arch: allow-serialization" in source_lines[line_num - 1]:
                        continue
                    rel = py_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel}:{line_num}: imports {module}")

        if violations:
            pytest.fail(
                "API modules must not import from siftd.serialization "
                "(serialization imports API, not the reverse):\n"
                + "\n".join(violations)
            )

    def test_storage_does_not_import_api(self, src_dir):
        """Storage layer must not import from API.

        Rationale: API wraps storage, not the other way around.
        Storage importing API would create a reverse dependency.
        """
        storage_dir = src_dir / "storage"
        violations = []

        for py_file in storage_dir.rglob("*.py"):
            for line_num, module in _extract_siftd_imports(py_file):
                if module.startswith("siftd.api"):
                    rel = py_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel}:{line_num}: imports {module}")

        if violations:
            pytest.fail(
                "Storage modules must not import from siftd.api "
                "(API wraps storage, not the reverse):\n"
                + "\n".join(violations)
            )

    def test_domain_is_pure(self, src_dir):
        """Domain layer must not import from API, storage, CLI, or serve.

        Rationale: Domain types are the foundation — they should have
        no dependencies on higher layers. Only infrastructure (ids,
        paths, safecall) and stdlib are allowed.
        """
        domain_dir = src_dir / "domain"
        forbidden = {"siftd.api", "siftd.storage", "siftd.cli", "siftd.serve",
                     "siftd.serialization", "siftd.output", "siftd.embeddings",
                     "siftd.adapters", "siftd.doctor", "siftd.ingestion"}
        violations = []

        for py_file in domain_dir.rglob("*.py"):
            for line_num, module in _extract_siftd_imports(py_file):
                top = "siftd." + module.split(".")[1]
                if any(module.startswith(f) for f in forbidden):
                    rel = py_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel}:{line_num}: imports {module}")

        if violations:
            pytest.fail(
                "Domain modules must be pure (no API/storage/CLI/serve deps):\n"
                + "\n".join(violations)
            )

    def test_no_api_serialization_cycle(self, src_dir):
        """No import cycles between api/ and serialization/.

        This is a stricter version of test_api_does_not_import_serialization
        without the suppress comment escape hatch. It will pass once the
        cycle is broken.
        """
        api_dir = src_dir / "api"
        violations = []

        for py_file in api_dir.rglob("*.py"):
            for line_num, module in _extract_siftd_imports(py_file):
                if module.startswith("siftd.serialization"):
                    rel = py_file.relative_to(src_dir.parent.parent)
                    violations.append(f"{rel}:{line_num}: imports {module}")

        if violations:
            pytest.fail(
                "API ↔ serialization cycle detected:\n"
                + "\n".join(violations)
            )
