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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

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
from siftd.doctor.checks.config_valid import ConfigValidCheck  # noqa: E402
from siftd.doctor.checks.cost_coverage import CostCoverageCheck  # noqa: E402
from siftd.doctor.checks.db_blob_orphans import DbBlobOrphansCheck  # noqa: E402
from siftd.doctor.checks.db_blob_refcount_drift import DbBlobRefcountDriftCheck  # noqa: E402
from siftd.doctor.checks.db_fk_integrity import DbFkIntegrityCheck  # noqa: E402
from siftd.doctor.checks.db_trigger_presence import DbTriggerPresenceCheck  # noqa: E402
from siftd.doctor.checks.drop_ins_valid import DropInsValidCheck  # noqa: E402
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
from siftd.doctor.checks.pricing_gaps import PricingGapsCheck  # noqa: E402
from siftd.doctor.checks.schema_current import SchemaCurrentCheck  # noqa: E402
from siftd.doctor.checks.workspace_identity import WorkspaceIdentityCheck  # noqa: E402

# Registry of built-in checks
BUILTIN_CHECKS: list[Check] = [
    IngestPendingCheck(),
    IngestErrorsCheck(),
    EmbeddingsAvailableCheck(),
    EmbeddingsCompatCheck(),
    EmbeddingsStaleCheck(),
    OrphanedChunksCheck(),
    PricingGapsCheck(),
    CostCoverageCheck(),
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
