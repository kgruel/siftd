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
                    fix_command="siftd search --index",
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
                    fix_command="siftd search --index",
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
                fix_command="siftd search --rebuild",
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


class PendingTagsCheck:
    """Detects orphaned pending tags for sessions that may never be ingested."""

    name = "pending-tags"
    description = "Pending tags for sessions that may never be ingested"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.storage.sessions import (
            get_orphaned_pending_tags_count,
            get_stale_sessions_count,
        )

        findings = []
        conn = ctx.get_db_conn()

        # Check if tables exist (migration may not have run)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_tags'"
        )
        if not cur.fetchone():
            return []

        # Count orphaned pending tags (session not registered)
        orphaned = get_orphaned_pending_tags_count(conn)
        if orphaned > 0:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"{orphaned} pending tag(s) for unregistered sessions",
                    fix_available=True,
                    fix_command="siftd doctor fix --pending-tags",
                    context={"orphaned_count": orphaned},
                )
            )

        # Count stale sessions (older than 48 hours)
        stale = get_stale_sessions_count(conn, max_age_hours=48)
        if stale > 0:
            findings.append(
                Finding(
                    check=self.name,
                    severity="info",
                    message=f"{stale} active session(s) older than 48 hours",
                    fix_available=True,
                    fix_command="siftd doctor fix --pending-tags",
                    context={"stale_count": stale},
                )
            )

        return findings


class FtsStaleCheck:
    """Detects FTS5 index out of sync with main content tables."""

    name = "fts-stale"
    description = "FTS5 search index out of sync with content tables"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        conn = ctx.get_db_conn()

        # Check if FTS table exists
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
        )
        if not cur.fetchone():
            return []  # FTS not created yet, schema-current will catch this

        # Check for orphaned FTS entries (in FTS but not in content tables)
        orphaned_count = conn.execute("""
            SELECT COUNT(*) FROM content_fts
            WHERE content_id NOT IN (SELECT id FROM prompt_content)
              AND content_id NOT IN (SELECT id FROM response_content)
        """).fetchone()[0]

        # Check for missing FTS entries (in content tables but not in FTS)
        missing_prompt_count = conn.execute("""
            SELECT COUNT(*) FROM prompt_content pc
            WHERE pc.block_type = 'text'
              AND pc.id NOT IN (SELECT content_id FROM content_fts WHERE side = 'prompt')
        """).fetchone()[0]

        missing_response_count = conn.execute("""
            SELECT COUNT(*) FROM response_content rc
            WHERE rc.block_type = 'text'
              AND rc.id NOT IN (SELECT content_id FROM content_fts WHERE side = 'response')
        """).fetchone()[0]

        total_issues = orphaned_count + missing_prompt_count + missing_response_count

        if total_issues == 0:
            return []

        parts = []
        if orphaned_count > 0:
            parts.append(f"{orphaned_count} orphaned")
        if missing_prompt_count + missing_response_count > 0:
            parts.append(f"{missing_prompt_count + missing_response_count} missing")

        return [
            Finding(
                check=self.name,
                severity="warning",
                message=f"FTS index out of sync: {', '.join(parts)} entries",
                fix_available=True,
                fix_command="siftd ingest --rebuild-fts",
                context={
                    "orphaned_count": orphaned_count,
                    "missing_prompt_count": missing_prompt_count,
                    "missing_response_count": missing_response_count,
                },
            )
        ]


class FtsIntegrityCheck:
    """Checks FTS5 table integrity for corruption."""

    name = "fts-integrity"
    description = "FTS5 search index integrity"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        # FTS5 integrity-check requires write access (special command syntax),
        # so we need to open a separate writable connection for this check.
        from siftd.storage.sqlite import open_database

        # Check if FTS table exists using read-only connection first
        conn = ctx.get_db_conn()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='content_fts'"
        )
        if not cur.fetchone():
            return []  # FTS not created yet

        # Now open a writable connection for the integrity check
        try:
            write_conn = open_database(ctx.db_path, read_only=False)
        except Exception as e:
            return [
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Cannot check FTS integrity (read-only): {e}",
                    fix_available=False,
                    context={"error": str(e)},
                )
            ]

        try:
            # FTS5 integrity-check returns 'ok' or error rows
            write_conn.execute("INSERT INTO content_fts(content_fts) VALUES('integrity-check')")
            return []  # No error means integrity is OK
        except sqlite3.IntegrityError as e:
            return [
                Finding(
                    check=self.name,
                    severity="error",
                    message=f"FTS5 index corruption detected: {e}",
                    fix_available=True,
                    fix_command="siftd ingest --rebuild-fts",
                    context={"error": str(e)},
                )
            ]
        finally:
            write_conn.close()


class EmbeddingsCompatCheck:
    """Validates embedding index compatibility with current backend configuration."""

    name = "embeddings-compat"
    description = "Embedding index matches current backend configuration"
    has_fix = True
    requires_db = False
    requires_embed_db = True
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.embeddings import embeddings_available

        # Skip if embeddings not installed
        if not embeddings_available():
            return []

        # Skip if embeddings DB doesn't exist
        if not ctx.embed_db_path.exists():
            return []

        from siftd.embeddings import SCHEMA_VERSION, get_backend
        from siftd.storage.embeddings import get_meta

        embed_conn = ctx.get_embed_conn()
        findings = []

        # Check for missing metadata keys (pre-versioning index)
        stored_schema = get_meta(embed_conn, "schema_version")
        stored_model = get_meta(embed_conn, "model")

        if stored_schema is None or stored_model is None:
            missing = []
            if stored_schema is None:
                missing.append("schema_version")
            if stored_model is None:
                missing.append("model")
            findings.append(
                Finding(
                    check=self.name,
                    severity="info",
                    message=f"Index missing compatibility metadata: {', '.join(missing)}",
                    fix_available=True,
                    fix_command="siftd search --rebuild",
                    context={"missing_keys": missing},
                )
            )
            # Continue with available checks

        # Try to get current backend for comparison
        try:
            backend = get_backend(verbose=False)
        except RuntimeError:
            # No backend available, can't compare
            return findings

        stored_backend = get_meta(embed_conn, "backend")
        stored_dimension = get_meta(embed_conn, "dimension")

        # Schema version check
        if stored_schema is not None:
            stored_ver = int(stored_schema)
            if stored_ver != SCHEMA_VERSION:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="info",
                        message=f"Index schema outdated (v{stored_ver} → v{SCHEMA_VERSION})",
                        fix_available=True,
                        fix_command="siftd search --rebuild",
                        context={
                            "stored_version": stored_ver,
                            "current_version": SCHEMA_VERSION,
                        },
                    )
                )

        # Backend/model mismatch check
        if stored_backend is not None and stored_backend != backend.name:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Backend mismatch: index={stored_backend}, current={backend.name}",
                    fix_available=True,
                    fix_command="siftd search --rebuild",
                    context={
                        "stored_backend": stored_backend,
                        "current_backend": backend.name,
                    },
                )
            )
        elif stored_model is not None and stored_model != backend.model:
            findings.append(
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Model mismatch: index={stored_model}, current={backend.model}",
                    fix_available=True,
                    fix_command="siftd search --rebuild",
                    context={
                        "stored_model": stored_model,
                        "current_model": backend.model,
                    },
                )
            )

        # Dimension mismatch (may happen without model being stored)
        if stored_dimension is not None:
            stored_dim = int(stored_dimension)
            if stored_dim != backend.dimension:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Dimension mismatch: index={stored_dim}, current={backend.dimension}",
                        fix_available=True,
                        fix_command="siftd search --rebuild",
                        context={
                            "stored_dimension": stored_dim,
                            "current_dimension": backend.dimension,
                        },
                    )
                )

        return findings


class ConfigValidCheck:
    """Validates configuration file syntax and known keys."""

    name = "config-valid"
    description = "Configuration file syntax and values"
    has_fix = False  # Manual fix required
    requires_db = False
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.paths import config_file

        path = config_file()

        if not path.exists():
            return []  # No config file is valid (uses defaults)

        findings = []

        # Check TOML syntax
        try:
            import tomlkit
            import tomlkit.exceptions

            content = path.read_text()
            doc = tomlkit.parse(content)
        except tomlkit.exceptions.TOMLKitError as e:
            return [
                Finding(
                    check=self.name,
                    severity="error",
                    message=f"Invalid TOML syntax in config file: {e}",
                    fix_available=False,
                    context={"path": str(path), "error": str(e)},
                )
            ]
        except OSError as e:
            return [
                Finding(
                    check=self.name,
                    severity="error",
                    message=f"Cannot read config file: {e}",
                    fix_available=False,
                    context={"path": str(path), "error": str(e)},
                )
            ]

        # Validate known keys
        search_config = doc.get("search", {})
        if isinstance(search_config, dict):
            formatter = search_config.get("formatter")
            if formatter is not None:
                # Check if formatter is valid
                findings.extend(self._validate_formatter(str(formatter)))

        return findings

    def _validate_formatter(self, formatter_name: str) -> list[Finding]:
        """Validate that the formatter name is registered."""
        from siftd.output.registry import get_registry

        valid_names = get_registry().list_names()
        if formatter_name not in valid_names:
            return [
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Unknown formatter '{formatter_name}' in config (valid: {', '.join(sorted(valid_names))})",
                    fix_available=False,
                    context={"formatter": formatter_name, "valid_formatters": valid_names},
                )
            ]
        return []


# Registry of built-in checks
BUILTIN_CHECKS: list[Check] = [
    IngestPendingCheck(),
    IngestErrorsCheck(),
    EmbeddingsAvailableCheck(),
    EmbeddingsCompatCheck(),
    EmbeddingsStaleCheck(),
    OrphanedChunksCheck(),
    PricingGapsCheck(),
    DropInsValidCheck(),
    FreelistCheck(),
    SchemaCurrentCheck(),
    PendingTagsCheck(),
    FtsStaleCheck(),
    FtsIntegrityCheck(),
    ConfigValidCheck(),
]
