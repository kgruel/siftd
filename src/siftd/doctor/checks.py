"""Health check definitions and built-in checks."""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Finding:
    """A single issue detected by a check."""

    check: str  # check name (e.g., "ingest-pending")
    severity: str  # "info" | "warning" | "error"
    message: str  # human-readable description
    fix_available: bool
    fix_command: str | None = None  # CLI command if fix available
    context: dict | None = None  # optional structured data


@dataclass
class CheckInfo:
    """Metadata about an available check."""

    name: str
    description: str
    has_fix: bool


@dataclass
class FixResult:
    """Result of applying a fix."""

    success: bool
    message: str


@dataclass
class CheckContext:
    """Context passed to all checks."""

    db_path: Path
    embed_db_path: Path
    adapters_dir: Path
    formatters_dir: Path
    queries_dir: Path

    # Lazy-loaded connections (populated on first access)
    _db_conn: sqlite3.Connection | None = field(default=None, repr=False)
    _embed_conn: sqlite3.Connection | None = field(default=None, repr=False)

    def get_db_conn(self):
        """Get main database connection (lazy-loaded)."""
        if self._db_conn is None:
            from siftd.storage.sqlite import open_database

            self._db_conn = open_database(self.db_path, read_only=True)
        return self._db_conn

    def get_embed_conn(self):
        """Get embeddings database connection (lazy-loaded)."""
        if self._embed_conn is None:
            from siftd.storage.embeddings import open_embeddings_db

            self._embed_conn = open_embeddings_db(self.embed_db_path, read_only=True)
        return self._embed_conn

    def close(self):
        """Close any open connections."""
        if (conn := self._db_conn) is not None:
            conn.close()
            self._db_conn = None
        if (embed_conn := self._embed_conn) is not None:
            embed_conn.close()
            self._embed_conn = None


class Check(Protocol):
    """Protocol for health checks."""

    name: str
    description: str
    has_fix: bool

    def run(self, ctx: CheckContext) -> list[Finding]: ...

    def fix(self, finding: Finding) -> FixResult | None: ...


# =============================================================================
# Built-in Checks
# =============================================================================


class IngestPendingCheck:
    """Detects files discovered by adapters but not yet ingested."""

    name = "ingest-pending"
    description = "Files discovered by adapters but not yet ingested"
    has_fix = True

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.adapters.registry import load_all_adapters

        findings = []
        adapters = load_all_adapters()
        conn = ctx.get_db_conn()

        # Get all ingested file paths
        cur = conn.execute("SELECT path FROM ingested_files")
        ingested_paths = {row[0] for row in cur.fetchall()}

        for adapter in adapters:
            try:
                discovered = list(adapter.discover())
            except Exception as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Adapter '{adapter.NAME}' discover() failed: {e}",
                        fix_available=False,
                    )
                )
                continue

            # Find files not in ingested_files
            pending = []
            for source in discovered:
                path_str = str(source.location)
                if path_str not in ingested_paths:
                    pending.append(path_str)

            if pending:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="info",
                        message=f"Adapter '{adapter.NAME}': {len(pending)} file(s) pending ingestion",
                        fix_available=True,
                        fix_command="siftd ingest",
                        context={"adapter": adapter.NAME, "count": len(pending)},
                    )
                )

        return findings

    def fix(self, finding: Finding) -> FixResult | None:
        # v1: report only, don't execute
        return None


class IngestErrorsCheck:
    """Reports files that failed ingestion."""

    name = "ingest-errors"
    description = "Files that failed ingestion (recorded with error)"
    has_fix = False

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings = []
        conn = ctx.get_db_conn()

        # Check if error column exists (migration may not have run yet)
        cur = conn.execute("PRAGMA table_info(ingested_files)")
        columns = {row[1] for row in cur.fetchall()}
        if "error" not in columns:
            return findings

        cur = conn.execute(
            "SELECT path, error, harness_id FROM ingested_files WHERE error IS NOT NULL"
        )
        rows = cur.fetchall()

        if rows:
            # Group by harness for cleaner reporting
            by_harness: dict[str, list[str]] = {}
            for row in rows:
                h_id = row["harness_id"]
                h_row = conn.execute(
                    "SELECT name FROM harnesses WHERE id = ?", (h_id,)
                ).fetchone()
                h_name = h_row["name"] if h_row else h_id
                by_harness.setdefault(h_name, []).append(row["error"])

            for harness_name, errors in by_harness.items():
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Adapter '{harness_name}': {len(errors)} file(s) failed ingestion",
                        fix_available=False,
                        context={
                            "adapter": harness_name,
                            "count": len(errors),
                            "errors": errors[:5],
                        },
                    )
                )

        return findings

    def fix(self, finding: Finding) -> FixResult | None:
        return None


class EmbeddingsStaleCheck:
    """Detects conversations not indexed in embeddings database."""

    name = "embeddings-stale"
    description = "Conversations not indexed in embeddings database"
    has_fix = True

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        # Skip entirely if embeddings not installed — not an error, it's optional
        if not embeddings_available():
            return []

        # Check if embeddings DB exists
        if not ctx.embed_db_path.exists():
            return [
                Finding(
                    check=self.name,
                    severity="info",
                    message="Embeddings database not found (not yet created)",
                    fix_available=True,
                    fix_command="siftd ask --index",
                )
            ]

        conn = ctx.get_db_conn()
        embed_conn = ctx.get_embed_conn()

        # Get conversation IDs that have embeddable content (at least one prompt)
        cur = conn.execute(
            "SELECT DISTINCT conversation_id FROM prompts"
        )
        main_ids = {row[0] for row in cur.fetchall()}

        # Get indexed conversation IDs from embeddings DB
        from siftd.storage.embeddings import get_indexed_conversation_ids

        indexed_ids = get_indexed_conversation_ids(embed_conn)

        # Find stale (not indexed)
        stale_ids = main_ids - indexed_ids

        if stale_ids:
            return [
                Finding(
                    check=self.name,
                    severity="info",
                    message=f"{len(stale_ids)} conversation(s) not indexed in embeddings",
                    fix_available=True,
                    fix_command="siftd ask --index",
                    context={"count": len(stale_ids)},
                )
            ]

        return []

    def fix(self, finding: Finding) -> FixResult | None:
        # v1: report only, don't execute
        return None


class PricingGapsCheck:
    """Detects models used in responses without pricing data."""

    name = "pricing-gaps"
    description = "Models used in responses without pricing data"
    has_fix = False

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings = []
        conn = ctx.get_db_conn()

        # Check if pricing table exists
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pricing'"
        )
        if not cur.fetchone():
            # No pricing table yet, skip this check
            return []

        # Find models without pricing
        # Note: responses.provider_id is nullable, so we need to handle that
        cur = conn.execute("""
            SELECT DISTINCT m.name as model_name, COALESCE(p.name, 'unknown') as provider_name
            FROM responses r
            JOIN models m ON r.model_id = m.id
            LEFT JOIN providers p ON r.provider_id = p.id
            WHERE r.model_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM pricing pr
                WHERE pr.model_id = r.model_id
                  AND (r.provider_id IS NULL OR pr.provider_id = r.provider_id)
            )
            ORDER BY provider_name, m.name
        """)

        missing = cur.fetchall()

        if missing:
            model_list = [f"{row[1]}/{row[0]}" for row in missing]
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"{len(missing)} model(s) without pricing: {', '.join(model_list[:5])}"
                    + ("..." if len(missing) > 5 else ""),
                    fix_available=False,
                    context={"models": model_list},
                )
            )

        return findings

    def fix(self, finding: Finding) -> FixResult | None:
        # No automated fix for pricing gaps
        return None


class DropInsValidCheck:
    """Validates drop-in adapters, formatters, and queries can load."""

    name = "drop-ins-valid"
    description = "Drop-in adapters, formatters, and queries load without errors"
    has_fix = False

    def run(self, ctx: CheckContext) -> list[Finding]:
        findings = []

        # Check adapters
        findings.extend(self._check_adapters(ctx.adapters_dir))

        # Check formatters
        findings.extend(self._check_formatters(ctx.formatters_dir))

        # Check queries
        findings.extend(self._check_queries(ctx.queries_dir))

        return findings

    def _check_adapters(self, adapters_dir: Path) -> list[Finding]:
        """Validate drop-in adapter files."""
        import importlib.util

        from siftd.adapters.registry import _REQUIRED_ATTRS, _REQUIRED_CALLABLES, _VALID_DEDUP_STRATEGIES

        findings = []

        if not adapters_dir.is_dir():
            return findings

        for py_file in sorted(adapters_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            module_name = f"siftd_doctor_check_adapter_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    findings.append(
                        Finding(
                            check=self.name,
                            severity="error",
                            message=f"Adapter '{py_file.name}': could not create module spec",
                            fix_available=False,
                        )
                    )
                    continue

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Validate required attributes
                errors = []
                for attr, expected_type in _REQUIRED_ATTRS.items():
                    if not hasattr(module, attr):
                        errors.append(f"missing '{attr}'")
                    elif not isinstance(getattr(module, attr), expected_type):
                        errors.append(f"'{attr}' wrong type")

                if hasattr(module, "DEDUP_STRATEGY"):
                    if module.DEDUP_STRATEGY not in _VALID_DEDUP_STRATEGIES:
                        errors.append(f"invalid DEDUP_STRATEGY '{module.DEDUP_STRATEGY}'")

                for func_name in _REQUIRED_CALLABLES:
                    if not hasattr(module, func_name) or not callable(
                        getattr(module, func_name)
                    ):
                        errors.append(f"missing function '{func_name}'")

                if errors:
                    findings.append(
                        Finding(
                            check=self.name,
                            severity="error",
                            message=f"Adapter '{py_file.name}': {', '.join(errors)}",
                            fix_available=False,
                        )
                    )

            except Exception as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="error",
                        message=f"Adapter '{py_file.name}': import failed: {e}",
                        fix_available=False,
                    )
                )

        return findings

    def _check_formatters(self, formatters_dir: Path) -> list[Finding]:
        """Validate drop-in formatter files."""
        import importlib.util

        from siftd.output.registry import _REQUIRED_ATTRS, _REQUIRED_CALLABLES

        findings = []

        if not formatters_dir.is_dir():
            return findings

        for py_file in sorted(formatters_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            module_name = f"siftd_doctor_check_formatter_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    findings.append(
                        Finding(
                            check=self.name,
                            severity="error",
                            message=f"Formatter '{py_file.name}': could not create module spec",
                            fix_available=False,
                        )
                    )
                    continue

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Validate required attributes
                errors = []
                for attr, expected_type in _REQUIRED_ATTRS.items():
                    if not hasattr(module, attr):
                        errors.append(f"missing '{attr}'")
                    elif not isinstance(getattr(module, attr), expected_type):
                        errors.append(f"'{attr}' wrong type")

                for func_name in _REQUIRED_CALLABLES:
                    if not hasattr(module, func_name) or not callable(
                        getattr(module, func_name)
                    ):
                        errors.append(f"missing function '{func_name}'")

                if errors:
                    findings.append(
                        Finding(
                            check=self.name,
                            severity="error",
                            message=f"Formatter '{py_file.name}': {', '.join(errors)}",
                            fix_available=False,
                        )
                    )

            except Exception as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="error",
                        message=f"Formatter '{py_file.name}': import failed: {e}",
                        fix_available=False,
                    )
                )

        return findings

    def _check_queries(self, queries_dir: Path) -> list[Finding]:
        """Validate query files have valid syntax."""
        findings = []

        if not queries_dir.is_dir():
            return findings


        for sql_file in sorted(queries_dir.glob("*.sql")):
            try:
                content = sql_file.read_text()

                # Basic check: file is not empty
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

                # Check for balanced quotes (basic syntax check)
                single_quotes = content.count("'") - content.count("\\'")
                if single_quotes % 2 != 0:
                    findings.append(
                        Finding(
                            check=self.name,
                            severity="error",
                            message=f"Query '{sql_file.name}': unbalanced single quotes",
                            fix_available=False,
                        )
                    )

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

    def fix(self, finding: Finding) -> FixResult | None:
        # No automated fix for invalid drop-ins
        return None


class OrphanedChunksCheck:
    """Detects embedding chunks whose conversations no longer exist in the main DB."""

    name = "orphaned-chunks"
    description = "Embedding chunks referencing deleted conversations"
    has_fix = True

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        # Skip if embeddings not installed — not an error
        if not embeddings_available():
            return []

        if not ctx.embed_db_path.exists():
            return []

        conn = ctx.get_db_conn()
        embed_conn = ctx.get_embed_conn()

        from siftd.storage.embeddings import get_indexed_conversation_ids

        embed_ids = get_indexed_conversation_ids(embed_conn)
        if not embed_ids:
            return []

        main_ids = {
            row[0]
            for row in conn.execute("SELECT id FROM conversations").fetchall()
        }

        orphaned_ids = embed_ids - main_ids
        if not orphaned_ids:
            return []

        # Count orphaned chunks (not just conversations)
        placeholders = ",".join("?" * len(orphaned_ids))
        count = embed_conn.execute(
            f"SELECT COUNT(*) FROM chunks WHERE conversation_id IN ({placeholders})",
            list(orphaned_ids),
        ).fetchone()[0]

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"{count} orphaned chunk(s) from {len(orphaned_ids)} deleted conversation(s)",
                fix_available=True,
                fix_command="siftd ask --rebuild",
                context={"chunk_count": count, "conversation_count": len(orphaned_ids)},
            )
        ]

    def fix(self, finding: Finding) -> FixResult | None:
        return None


class EmbeddingsAvailableCheck:
    """Reports embedding support installation status (informational only)."""

    name = "embeddings-available"
    description = "Embedding support installation status"
    has_fix = False  # Not an error, just informational

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        if embeddings_available():
            return []  # No finding when available — it's optional, not an error

        # Only report if user has an embeddings DB (indicates intent to use)
        if ctx.embed_db_path.exists():
            return [
                Finding(
                    check=self.name,
                    severity="info",
                    message="Embeddings database exists but embedding support not installed",
                    fix_available=False,
                    context={"install_hint": "pip install siftd[embed]"},
                )
            ]

        return []  # No DB, no finding — user may not need embeddings

    def fix(self, finding: Finding) -> FixResult | None:
        return None


# Registry of built-in checks
BUILTIN_CHECKS: list[Check] = [
    IngestPendingCheck(),
    IngestErrorsCheck(),
    EmbeddingsAvailableCheck(),
    EmbeddingsStaleCheck(),
    OrphanedChunksCheck(),
    PricingGapsCheck(),
    DropInsValidCheck(),
]
