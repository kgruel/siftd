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


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection, deriving immutability instead of asserting it.

    `mode=ro&immutable=1` tells SQLite the file cannot change, so it omits all
    locking and change detection. Doctor cannot honour that: `fts-integrity`
    opens its own write connection mid-run, and a concurrent `siftd ingest` or
    a running `serve` writes the same file from another process. SQLite calls
    the result undefined, and it is reachable two ways, both silent:

      - An immutable reader ignores the `-wal` file entirely. Against a
        database with un-checkpointed WAL content — which is what a live
        `serve` leaves — every check reads a snapshot missing every commit
        since the last checkpoint, and reports on it without complaint.
      - When a writer checkpoints mid-run, main-file pages are rewritten under
        a reader that has no change detection. Scans then truncate early,
        counts disagree with the rows they counted, and `integrity_check`
        reports corruption in a database that is fine.

    So the plain `mode=ro` open is tried first: it takes WAL read marks and
    sees a consistent snapshot no matter who else is writing. It needs to
    create the `-shm` sidecar, which fails on genuinely read-only media — and
    that failure is the signal we actually want, because a medium no writer
    can reach is a medium where `immutable=1` is *true* rather than assumed.
    Hence the fallback: immutability becomes a property discovered from the
    file, not a promise the caller makes on its behalf.

    The probe has to run a statement. `sqlite3.connect` succeeds against
    read-only media; the sidecar is only created when the first read
    transaction opens, so that is where the error surfaces.
    """
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA schema_version")
        return conn
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        return sqlite3.connect(f"{uri}&immutable=1", uri=True, check_same_thread=False)


@dataclass
class CheckContext:
    """Context passed to all checks."""

    db_path: Path
    embed_db_path: Path
    adapters_dir: Path
    formatters_dir: Path
    queries_dir: Path

    # Read-only connections, keyed by (thread, database) and opened on demand.
    # Never one connection shared across the runner's thread pool: see the
    # per-thread rule in _get_conn.
    _conns: dict[tuple[threading.Thread, str], sqlite3.Connection] = field(
        default_factory=dict, repr=False, compare=False
    )
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

    def _get_conn(self, db_path: Path, *, foreign_keys: bool) -> sqlite3.Connection:
        """One read-only connection per (thread, database), opened on first use.

        Per thread, never per run: checks run concurrently, and a single
        sqlite3.Connection read from several threads is not safe even at
        threadsafety==3 — the prepared-statement cache is shared state. With
        fts-integrity opening its own *write* connection to the same file
        mid-run, the overlap produced wrong query results rather than errors.

        Keyed on the Thread object, not threading.get_ident(): idents are
        recycled once a thread dies, so an ident key silently hands a new
        thread its dead predecessor's connection. That happens to be safe
        (sequential use, which check_same_thread=False permits — that flag is
        here so close() can run from the caller's thread, not as a concurrency
        claim), but "one connection per thread" should be true as written
        rather than true by accident. The dict is per-run and pool-sized, so
        holding Thread references costs nothing.

        Immutability is derived from the medium by _connect_read_only, not
        asserted — see its docstring. Not routed through open_database despite
        the overlap: it clears the process-global vocabulary caches on every
        open, which a per-thread open would do repeatedly mid-run, on other
        subsystems' behalf. A diagnostic reads; it should not reach into
        shared state.
        """
        key = (threading.current_thread(), str(db_path))
        with self._lock:
            conn = self._conns.get(key)
            if conn is None:
                conn = _connect_read_only(db_path)
                conn.row_factory = sqlite3.Row
                if foreign_keys:
                    conn.execute("PRAGMA foreign_keys = ON")
                self._conns[key] = conn
        return conn

    def get_db_conn(self):
        """Get main database connection (lazy-loaded, one per calling thread)."""
        return self._get_conn(self.db_path, foreign_keys=True)

    def get_embed_conn(self):
        """Get embeddings database connection (lazy-loaded, one per calling thread)."""
        return self._get_conn(self.embed_db_path, foreign_keys=False)

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
        """Close every connection this context opened, on any thread."""
        with self._lock:
            conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            conn.close()


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
