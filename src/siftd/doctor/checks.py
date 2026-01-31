"""Health check definitions and built-in checks."""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

# Cost classification for --fast mode filtering
CheckCost = Literal["fast", "slow"]


@dataclass
class Finding:
    """A single issue detected by a check.

    Attributes:
        check: Check name that produced this finding (e.g., "ingest-pending").
        severity: One of "info", "warning", or "error".
        message: Human-readable description of the issue.
        fix_available: Whether a fix suggestion exists.
        fix_command: CLI command to fix the issue (advisory only, not executed
            automatically). User must run this command manually.
        context: Optional structured data for programmatic consumers.
    """

    check: str
    severity: str
    message: str
    fix_available: bool
    fix_command: str | None = None
    context: dict | None = None


@dataclass
class CheckInfo:
    """Metadata about an available check."""

    name: str
    description: str
    has_fix: bool
    requires_db: bool
    requires_embed_db: bool
    cost: CheckCost


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
    """Protocol for health checks.

    Checks detect issues and may provide fix suggestions via Finding.fix_command.
    Fixes are advisory only - they report what command to run but don't execute it.

    Attributes:
        name: Unique check identifier (e.g., "ingest-pending").
        description: Human-readable description of what the check does.
        has_fix: Whether this check can suggest fixes (via Finding.fix_command).
        requires_db: Whether check needs main database to exist.
        requires_embed_db: Whether check needs embeddings database to exist.
        cost: "fast" or "slow" for --fast mode filtering.
    """

    name: str
    description: str
    has_fix: bool
    requires_db: bool
    requires_embed_db: bool
    cost: CheckCost

    def run(self, ctx: CheckContext) -> list[Finding]:
        """Run the check and return any findings."""
        ...


# =============================================================================
# Built-in Checks
# =============================================================================


class IngestPendingCheck:
    """Detects files discovered by adapters but not yet ingested."""

    name = "ingest-pending"
    description = "Files discovered by adapters but not yet ingested"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "slow"  # Runs discover() on all adapters

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.adapters.registry import load_all_adapters

        findings = []
        plugins = load_all_adapters()
        conn = ctx.get_db_conn()

        # Get all ingested file paths
        cur = conn.execute("SELECT path FROM ingested_files")
        ingested_paths = {row[0] for row in cur.fetchall()}

        for plugin in plugins:
            adapter = plugin.module
            try:
                discovered = list(adapter.discover())
            except Exception as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Adapter '{plugin.name}' discover() failed: {e}",
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
                        message=f"Adapter '{plugin.name}': {len(pending)} file(s) pending ingestion",
                        fix_available=True,
                        fix_command="siftd ingest",
                        context={"adapter": plugin.name, "count": len(pending)},
                    )
                )

        return findings


class IngestErrorsCheck:
    """Reports files that failed ingestion."""

    name = "ingest-errors"
    description = "Files that failed ingestion (recorded with error)"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

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


class EmbeddingsStaleCheck:
    """Detects conversations not indexed in embeddings database."""

    name = "embeddings-stale"
    description = "Conversations not indexed in embeddings database"
    has_fix = True
    requires_db = True
    requires_embed_db = True
    cost: CheckCost = "fast"

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


class PricingGapsCheck:
    """Detects models used in responses without pricing data."""

    name = "pricing-gaps"
    description = "Models used in responses without pricing data"
    has_fix = False
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

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

        # Check adapters
        findings.extend(self._check_adapters(ctx.adapters_dir))

        # Check formatters
        findings.extend(self._check_formatters(ctx.formatters_dir))

        # Check queries
        findings.extend(self._check_queries(ctx.queries_dir))

        return findings

    # Required module-level names for adapters (must be defined at module level)
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

    # Required module-level names for formatters
    _FORMATTER_REQUIRED_NAMES = [
        "NAME",
        "create_formatter",
    ]

    def _check_adapters(self, adapters_dir: Path) -> list[Finding]:
        """Validate drop-in adapter files using AST parsing (no import/execution)."""
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

                # Use SQLite EXPLAIN to validate syntax
                error = self._validate_sql_syntax(content)
                if error:
                    findings.append(
                        Finding(
                            check=self.name,
                            severity="error",
                            message=f"Query '{sql_file.name}': {error}",
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

    def _validate_sql_syntax(self, sql: str) -> str | None:
        """Return error message if SQL has syntax errors, None if valid.

        Uses SQLite EXPLAIN on an in-memory database to catch syntax errors.
        Missing table/column errors are ignored (runtime validation).
        """
        import re

        # Substitute $var placeholders with NULL to allow EXPLAIN to parse
        # Query files use $var for user-provided values
        sql_for_explain = re.sub(r"\$\w+", "NULL", sql)

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(f"EXPLAIN {sql_for_explain}")
            return None
        except sqlite3.Error as e:
            msg = str(e)
            # Ignore missing table/column errors — those are runtime validation
            # Real syntax errors: "syntax error", "incomplete input", etc.
            if msg.startswith("no such table:") or msg.startswith("no such column:"):
                return None
            return msg
        finally:
            conn.close()


class OrphanedChunksCheck:
    """Detects embedding chunks whose conversations no longer exist in the main DB."""

    name = "orphaned-chunks"
    description = "Embedding chunks referencing deleted conversations"
    has_fix = True
    requires_db = True
    requires_embed_db = True
    cost: CheckCost = "fast"

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


class EmbeddingsAvailableCheck:
    """Reports embedding support installation status (informational only)."""

    name = "embeddings-available"
    description = "Embedding support installation status"
    has_fix = False  # Not an error, just informational
    requires_db = False
    requires_embed_db = False
    cost: CheckCost = "fast"

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


class FreelistCheck:
    """Reports SQLite freelist pages that could be reclaimed with VACUUM."""

    name = "freelist"
    description = "SQLite freelist pages (reclaimable with VACUUM)"
    has_fix = False  # VACUUM is manual, not auto-applied
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        conn = ctx.get_db_conn()

        freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]

        if freelist_count == 0:
            return []

        # Calculate wasted space
        wasted_bytes = freelist_count * page_size
        if wasted_bytes < 1024 * 1024:  # < 1MB
            wasted_str = f"{wasted_bytes / 1024:.0f}KB"
        else:
            wasted_str = f"{wasted_bytes / (1024 * 1024):.1f}MB"

        pct = (freelist_count / page_count * 100) if page_count > 0 else 0

        return [
            Finding(
                check=self.name,
                severity="info",
                message=f"{freelist_count} free page(s) ({wasted_str}, {pct:.0f}% of DB) could be reclaimed",
                fix_available=False,
                context={
                    "freelist_count": freelist_count,
                    "page_count": page_count,
                    "page_size": page_size,
                    "wasted_bytes": wasted_bytes,
                    "tip": f"sqlite3 {ctx.db_path} 'VACUUM'",
                },
            )
        ]


class SchemaCurrentCheck:
    """Checks if database schema is up to date with expected migrations."""

    name = "schema-current"
    description = "Database schema migrations are up to date"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        conn = ctx.get_db_conn()
        pending_migrations: list[str] = []

        # Check 1: error column on ingested_files (added in _migrate_add_error_column)
        cur = conn.execute("PRAGMA table_info(ingested_files)")
        columns = {row[1] for row in cur.fetchall()}
        if "error" not in columns:
            pending_migrations.append("add error column to ingested_files")

        # Check 2: CASCADE deletes on prompts (added in _migrate_add_cascade_deletes)
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='prompts'"
        )
        row = cur.fetchone()
        if row and "ON DELETE CASCADE" not in (row[0] or ""):
            pending_migrations.append("add CASCADE deletes to foreign keys")

        # Check 3: pricing table exists (ensure_pricing_table)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pricing'"
        )
        if not cur.fetchone():
            pending_migrations.append("create pricing table")

        # Check 4: content_blobs table exists (ensure_content_blobs_table)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_blobs'"
        )
        if not cur.fetchone():
            pending_migrations.append("create content_blobs table")

        # Check 5: tool_call_tags table exists (ensure_tool_call_tags_table)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_call_tags'"
        )
        if not cur.fetchone():
            pending_migrations.append("create tool_call_tags table")

        # Check 6: FTS5 content_fts table exists (ensure_fts_table)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
        )
        if not cur.fetchone():
            pending_migrations.append("create FTS5 search index")

        if not pending_migrations:
            return []

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"{len(pending_migrations)} migration(s) pending: {', '.join(pending_migrations[:3])}"
                + ("..." if len(pending_migrations) > 3 else ""),
                fix_available=True,
                fix_command="siftd ingest",
                context={"pending": pending_migrations},
            )
        ]


# Registry of built-in checks
BUILTIN_CHECKS: list[Check] = [
    IngestPendingCheck(),
    IngestErrorsCheck(),
    EmbeddingsAvailableCheck(),
    EmbeddingsStaleCheck(),
    OrphanedChunksCheck(),
    PricingGapsCheck(),
    DropInsValidCheck(),
    FreelistCheck(),
    SchemaCurrentCheck(),
]
