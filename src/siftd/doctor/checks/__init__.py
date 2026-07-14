"""Health check types and built-in check registry."""

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

# Cost classification for check filtering
CheckCost = Literal["fast", "slow", "deep"]


@dataclass
class Finding:
    """A single issue detected by a check.

    Attributes:
        check: Check name that produced this finding (e.g., "ingest-pending").
        severity: One of "info", "warning", "error", or "hint".
        message: Human-readable description of the issue.
        fix_available: Whether a fix suggestion exists.
        fix_command: CLI command to fix the issue (advisory only, not executed
            automatically). User must run this command manually.
        context: Optional structured data for programmatic consumers.
        target: Optional row-scope identifier — when set, the finding refers to
            a specific entity (e.g., a conversation id) rather than the whole
            result set or DB. Used by the caveats producer registry to thread
            row-level annotations through dispatch into renderers.
        channel: Controls output-format visibility. "text" findings are excluded
            from --json output; "json" findings are excluded from text/TTY
            output; "both" (default) appears everywhere.
    """

    check: str
    severity: Literal["info", "warning", "error", "hint"]
    message: str
    fix_available: bool
    fix_command: str | None = None
    context: dict | None = None
    target: str | None = None
    channel: Literal["text", "json", "both"] = "both"


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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # Lazy adapter discovery shared across checks (populated on first access).
    # _discovered caches each adapter's materialized discover() output — or the
    # exception it raised — keyed by adapter name. Separate lock so a slow
    # directory walk doesn't block checks that only need a DB connection.
    _plugins: list | None = field(default=None, repr=False)
    _discovered: dict = field(default_factory=dict, repr=False)
    _discovery_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def get_db_conn(self):
        """Get main database connection (lazy-loaded, thread-safe for reads)."""
        with self._lock:
            if self._db_conn is None:
                uri = f"file:{self.db_path.as_posix()}?mode=ro&immutable=1"
                self._db_conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                self._db_conn.row_factory = sqlite3.Row
                self._db_conn.execute("PRAGMA foreign_keys = ON")
            return self._db_conn

    def get_embed_conn(self):
        """Get embeddings database connection (lazy-loaded, thread-safe for reads)."""
        with self._lock:
            if self._embed_conn is None:
                uri = f"file:{self.embed_db_path.as_posix()}?mode=ro&immutable=1"
                self._embed_conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                self._embed_conn.row_factory = sqlite3.Row
            return self._embed_conn

    def get_adapters(self) -> list:
        """Enabled adapter plugins, loaded once per run and shared across checks."""
        with self._discovery_lock:
            if self._plugins is None:
                from siftd.adapters.registry import load_all_adapters

                self._plugins = load_all_adapters(dropin_path=self.adapters_dir)
            return self._plugins

    def discover_sources(self, plugin) -> list:
        """One adapter's discover() results, materialized and memoized for the run.

        Discovery walks the adapter's log directories — the expensive part of
        the slow-lane checks — so checks that reconcile discovered files
        against the DB (ingest-pending, adapter-stale) share one pass per
        adapter instead of each running their own. Memoized per adapter rather
        than as one eager sweep so a check that needs only a subset
        (adapter-stale skips adapters without DB presence) doesn't force
        discovery of the rest. A discover() failure is cached and re-raised to
        every caller, letting each check keep its own failure policy.
        """
        with self._discovery_lock:
            if plugin.name not in self._discovered:
                try:
                    self._discovered[plugin.name] = list(plugin.module.discover())
                except Exception as e:
                    self._discovered[plugin.name] = e
            outcome = self._discovered[plugin.name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

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


# Import all built-in check classes — must come after type definitions above
from siftd.doctor.checks.adapter_stale import AdapterStaleCheck  # noqa: E402
from siftd.doctor.checks.config_valid import ConfigValidCheck  # noqa: E402
from siftd.doctor.checks.cost_coverage import CostCoverageCheck  # noqa: E402
from siftd.doctor.checks.db_blob_orphans import DbBlobOrphansCheck  # noqa: E402
from siftd.doctor.checks.db_blob_refcount_drift import DbBlobRefcountDriftCheck  # noqa: E402
from siftd.doctor.checks.db_fk_integrity import DbFkIntegrityCheck  # noqa: E402
from siftd.doctor.checks.db_trigger_presence import DbTriggerPresenceCheck  # noqa: E402
from siftd.doctor.checks.drop_ins_valid import DropInsValidCheck  # noqa: E402
from siftd.doctor.checks.embed_config import EmbedConfigCheck  # noqa: E402
from siftd.doctor.checks.embeddings_available import EmbeddingsAvailableCheck  # noqa: E402
from siftd.doctor.checks.embeddings_compat import EmbeddingsCompatCheck  # noqa: E402
from siftd.doctor.checks.embeddings_stale import EmbeddingsStaleCheck  # noqa: E402
from siftd.doctor.checks.freelist import FreelistCheck  # noqa: E402
from siftd.doctor.checks.fts_integrity import FtsIntegrityCheck  # noqa: E402
from siftd.doctor.checks.fts_stale import FtsStaleCheck  # noqa: E402
from siftd.doctor.checks.ingest_errors import IngestErrorsCheck  # noqa: E402
from siftd.doctor.checks.ingest_pending import IngestPendingCheck  # noqa: E402
from siftd.doctor.checks.orphaned_chunks import OrphanedChunksCheck  # noqa: E402
from siftd.doctor.checks.pending_tags import PendingTagsCheck  # noqa: E402
from siftd.doctor.checks.pricing_provenance import PricingProvenanceCheck  # noqa: E402
from siftd.doctor.checks.schema_current import SchemaCurrentCheck  # noqa: E402
from siftd.doctor.checks.workspace_identity import WorkspaceIdentityCheck  # noqa: E402

# Registry of built-in checks
BUILTIN_CHECKS: list[Check] = [
    IngestPendingCheck(),
    IngestErrorsCheck(),
    AdapterStaleCheck(),
    EmbedConfigCheck(),
    EmbeddingsAvailableCheck(),
    EmbeddingsCompatCheck(),
    EmbeddingsStaleCheck(),
    OrphanedChunksCheck(),
    CostCoverageCheck(),
    PricingProvenanceCheck(),
    DropInsValidCheck(),
    FreelistCheck(),
    SchemaCurrentCheck(),
    PendingTagsCheck(),
    FtsStaleCheck(),
    FtsIntegrityCheck(),
    ConfigValidCheck(),
    WorkspaceIdentityCheck(),
    DbFkIntegrityCheck(),
    DbBlobRefcountDriftCheck(),
    DbBlobOrphansCheck(),
    DbTriggerPresenceCheck(),
]
