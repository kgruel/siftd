import sqlite3
from pathlib import Path

from siftd.doctor.checks import CheckContext, CheckCost, Finding


class DropInsValidCheck:
    """Validates drop-in adapters, formatters, and queries can load."""

    name = "drop-ins-valid"
    description = "Drop-in adapters, formatters, and queries load without errors"
    has_fix = False
    requires_db = False
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings = []
        findings.extend(self._check_adapters(ctx.adapters_dir))
        findings.extend(self._check_formatters(ctx.formatters_dir))
        findings.extend(self._check_queries(ctx.queries_dir))
        return findings

    _ADAPTER_REQUIRED_NAMES = [
        "ADAPTER_INTERFACE_VERSION",
        "NAME",
        "DEFAULT_LOCATIONS",
        "DEDUP_STRATEGY",
        "HARNESS_SOURCE",
        "discover",
        "can_handle",
        "parse",
    ]

    _FORMATTER_REQUIRED_NAMES = [
        "NAME",
        "create_formatter",
    ]

    def _check_adapters(self, adapters_dir: Path) -> list[Finding]:
        """Validate drop-in adapters: AST name check, then import for signature validation."""
        import importlib.util

        from siftd.adapters.validation import validate_adapter
        from siftd.plugin_discovery import validate_dropin_ast

        findings = []
        if not adapters_dir.is_dir():
            return findings

        for py_file in sorted(adapters_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            errors = validate_dropin_ast(py_file, self._ADAPTER_REQUIRED_NAMES)
            if errors:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="error",
                        message=f"Adapter '{py_file.name}': {', '.join(errors)}",
                        fix_available=False,
                    )
                )
                continue  # skip signature check when required names are missing

            # Import to validate callable signatures (intentionally executes module code)
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec is None:
                    raise ImportError("spec_from_file_location returned None")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except Exception as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="error",
                        message=f"Adapter '{py_file.name}': import failed: {e}",
                        fix_available=False,
                    )
                )
                continue

            error = validate_adapter(module, origin=f"adapter '{py_file.name}'")
            if error:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="error",
                        message=error,
                        fix_available=False,
                    )
                )
        return findings

    def _check_formatters(self, formatters_dir: Path) -> list[Finding]:
        """Validate drop-in formatter files using AST parsing (no import/execution)."""
        from siftd.plugin_discovery import validate_dropin_ast

        findings = []
        if not formatters_dir.is_dir():
            return findings

        for py_file in sorted(formatters_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            errors = validate_dropin_ast(py_file, self._FORMATTER_REQUIRED_NAMES)
            if errors:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="error",
                        message=f"Formatter '{py_file.name}': {', '.join(errors)}",
                        fix_available=False,
                    )
                )
        return findings

    def _check_queries(self, queries_dir: Path) -> list[Finding]:
        """Validate query files have valid syntax using SQLite EXPLAIN."""
        import re

        findings = []
        if not queries_dir.is_dir():
            return findings

        for sql_file in sorted(queries_dir.glob("*.sql")):
            try:
                content = sql_file.read_text()
                if not content.strip():
                    findings.append(
                        Finding(
                            check=self.name,
                            severity="warning",
                            message=f"Query '{sql_file.name}': file is empty",
                            fix_available=False,
                        )
                    )
                    continue

                sql_for_explain = re.sub(r"\$\w+", "NULL", content)
                sql_for_explain = re.sub(r":\w+", "NULL", sql_for_explain)
                conn = sqlite3.connect(":memory:")
                try:
                    conn.execute(f"EXPLAIN {sql_for_explain}")
                except sqlite3.Error as e:
                    msg = str(e)
                    if not msg.startswith("no such table:") and not msg.startswith("no such column:"):
                        findings.append(
                            Finding(
                                check=self.name,
                                severity="error",
                                message=f"Query '{sql_file.name}': {msg}",
                                fix_available=False,
                            )
                        )
                finally:
                    conn.close()

            except Exception as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="error",
                        message=f"Query '{sql_file.name}': read failed: {e}",
                        fix_available=False,
                    )
                )

        return findings
