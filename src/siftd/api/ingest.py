"""Ingest API wrappers.

Provides API-level write primitives for ingestion and FTS rebuild operations.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal

from siftd.adapters.registry import load_all_adapters, wrap_adapter_paths
from siftd.api.database import create_database
from siftd.api.search import rebuild_fts_index
from siftd.errors import UserInputError
from siftd.ingestion import IngestEvent, IngestStats, ingest_all

logger = logging.getLogger(__name__)


class AdapterSelectionError(UserInputError):
    """Raised when requested adapter names match no discovered adapters."""

    def __init__(
        self, requested: list[str], available: list[str], disabled: list[str] | None = None
    ) -> None:
        self.requested = requested
        self.available = available
        self.disabled = disabled or []
        message = f"No adapters matched: {', '.join(requested)}"
        if self.disabled:
            message += (
                f" (disabled via config: {', '.join(self.disabled)} —"
                " set [adapters.<name>] enabled = true to re-enable)"
            )
        super().__init__(message)


# Cap on the stale+new set the post-ingest hook will embed inline. A larger backlog (or a
# never-built index) is deferred to an explicit `siftd embed` — the first-run backlog can be
# tens of thousands of chunks, and a 3-RPM remote free tier would otherwise hang ingest.
_AUTO_INDEX_BACKLOG_LIMIT = 200


@dataclass
class AutoIndexReport:
    """Outcome of the post-ingest auto-index hook, surfaced by the ingest renderer.

    ``ran`` = the incremental indexer executed; ``awaiting``/``skipped_reason`` = inline work
    was deferred to ``siftd embed``; ``notice`` = one-time remote first-egress notice; ``error``
    = an embedding failure that was isolated (never aborts ingest).
    """

    ran: bool = False
    chunks_added: int = 0
    conversations_indexed: int = 0
    awaiting: int = 0
    skipped_reason: str | None = None  # "unbuilt" | "backlog" | "notice"
    notice: str | None = None
    error: str | None = None


@dataclass
class IngestRunResult:
    """Result metadata for an ingest API run."""

    db_path: Path
    db_created: bool
    mode: Literal["ingest", "rebuild_fts"]
    adapters: list[str]
    scan_paths: list[str]
    stats: IngestStats | None
    elapsed_ms: int
    dropin_failures: list[tuple[Path, str]] = field(default_factory=list)
    auto_index: AutoIndexReport | None = None
    adapter_tiers: dict[str, str] = field(default_factory=dict)  # name -> SUPPORT_TIER
    disabled_adapters: list[str] = field(default_factory=list)  # skipped via config knob
    # True when another process already holds this database's ingest lock, so
    # this run did nothing. Not an error: an ingest of the same sources is
    # already in flight. ``stats`` is None in that case.
    skipped_locked: bool = False


__all__ = [
    "AdapterSelectionError",
    "AutoIndexReport",
    "IngestRunResult",
    "run_ingest",
    "run_rebuild_fts",
]


@contextmanager
def _ingest_lock(db_path: Path) -> Iterator[bool]:
    """Hold an exclusive, non-blocking advisory lock on ``db_path``'s ingest.

    Yields True when this process owns the lock (or when locking is
    unavailable, see below) and False when another process already holds it.

    Two concurrent ingests of the same sources both parse the same changed
    transcript and both insert the same conversation; the loser hits
    UNIQUE(harness_id, external_id) and used to discard the winner's pointer,
    freezing that file forever (kgruel/siftd#29). Serializing removes the race
    at the source. The lock is a sibling ``.ingest.lock`` file rather than the
    database, so it survives the DB being replaced and so a lock-out never has
    to open — or create — the database at all. It is keyed on the *resolved*
    path so two spellings of one database contend and two different databases
    do not.

    ``flock`` is POSIX-only, and even on POSIX a filesystem may refuse it
    (some NFS mounts). Both degrade to running unlocked — the pre-0.12.1
    behavior — because refusing to ingest would be a worse failure than the
    race this prevents. The refusal is logged at WARNING: degrading silently
    would leave a user who upgraded for the lock believing they are serialized
    when they are not.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover — non-POSIX (Windows)
        yield True
        return

    lock_path = Path(str(Path(db_path).resolve()) + ".ingest.lock")
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Held by another ingest. BlockingIOError is an OSError subclass, so
        # this branch must precede the degrade branch below.
        if handle is not None:
            handle.close()
        yield False
        return
    except OSError as exc:
        # Degrading is deliberate, but silence is not: an ingest that is not
        # serialized is one that can still lose the UNIQUE race, and a user who
        # upgraded for the lock has no other way to learn it is not in effect.
        logger.warning(
            f"Ingest is running unserialized: could not lock {lock_path} "
            f"({exc.strerror or exc}). Concurrent ingests of this database can "
            "still collide (kgruel/siftd#29)."
        )
        if handle is not None:
            handle.close()
        yield True
        return

    try:
        yield True
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _resolve_adapters(
    *,
    adapter_names: list[str] | None,
    scan_paths: list[str] | None,
    failures_out: list[tuple[Path, str]] | None = None,
    disabled_out: list[str] | None = None,
) -> tuple[list, list[str], dict[str, str]]:
    """Resolve discovered adapter modules with optional filtering/overrides."""
    from siftd.adapters.validation import support_tier

    disabled: list[str] = []
    plugins = load_all_adapters(failures_out=failures_out, disabled_out=disabled)
    if disabled_out is not None:
        disabled_out.extend(disabled)

    if adapter_names:
        requested = set(adapter_names)
        plugins = [p for p in plugins if p.name in requested]
        if not plugins:
            raise AdapterSelectionError(
                requested=adapter_names,
                available=[],
                disabled=[n for n in adapter_names if n in disabled],
            )

    if scan_paths:
        adapters = [wrap_adapter_paths(p.module, scan_paths) for p in plugins]
    else:
        adapters = [p.module for p in plugins]

    tiers = {p.name: support_tier(p.module) for p in plugins}
    return adapters, [p.name for p in plugins], tiers


def run_ingest(
    *,
    db_path: Path,
    adapter_names: list[str] | None = None,
    scan_paths: list[str] | None = None,
    filter_binary: bool | None = None,
    on_event: Callable[[IngestEvent], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
) -> IngestRunResult:
    """Run ingestion from discovered adapters.

    API owns DB lifecycle for this write operation. ``on_notice`` is invoked (once, before
    any embedding egress) with the remote first-egress disclosure so the caller can surface
    it live. A caller with NO callback cannot satisfy notice-precedes-egress, so
    pending-disclosure auto-indexing is skipped (``skipped_reason="notice"``, disclosure on
    ``result.auto_index.notice``) until any surface has shown the notice — interactive
    ingest, or the first explicit ``siftd embed``.

    Runs under an exclusive advisory lock on the database: a second concurrent invocation
    does nothing and returns ``skipped_locked=True`` (stats None). This is the single funnel
    into :func:`~siftd.ingestion.ingest_all` — the CLI ``ingest`` command and ``doctor fix
    --ingest`` are its only callers — so the lock belongs here, not in either surface.
    """
    path = Path(db_path)
    started = perf_counter()

    with _ingest_lock(path) as acquired:
        if not acquired:
            return IngestRunResult(
                db_path=path,
                db_created=False,
                mode="ingest",
                adapters=[],
                scan_paths=list(scan_paths or []),
                stats=None,
                elapsed_ms=int((perf_counter() - started) * 1000),
                skipped_locked=True,
            )
        return _run_ingest_locked(
            path=path,
            started=started,
            adapter_names=adapter_names,
            scan_paths=scan_paths,
            filter_binary=filter_binary,
            on_event=on_event,
            on_notice=on_notice,
        )


def _run_ingest_locked(
    *,
    path: Path,
    started: float,
    adapter_names: list[str] | None,
    scan_paths: list[str] | None,
    filter_binary: bool | None,
    on_event: Callable[[IngestEvent], None] | None,
    on_notice: Callable[[str], None] | None,
) -> IngestRunResult:
    """The body of :func:`run_ingest`, with the ingest lock already held."""
    db_created = not path.exists()

    dropin_failures: list[tuple[Path, str]] = []
    disabled_adapters: list[str] = []
    conn = create_database(path)
    try:
        adapters, selected_names, adapter_tiers = _resolve_adapters(
            adapter_names=adapter_names,
            scan_paths=scan_paths,
            failures_out=dropin_failures,
            disabled_out=disabled_adapters,
        )
        stats = ingest_all(
            conn,
            adapters,
            on_event=on_event,
            filter_binary=filter_binary,
        )
    finally:
        conn.close()

    # Best-effort cache refresh after ingest.
    try:
        from siftd.api.stats import effective_db_mtime_ns, get_stats, write_stats_cache

        db_mtime = effective_db_mtime_ns(path)  # captured before the sweep
        write_stats_cache(get_stats(db_path=path), db_mtime_ns=db_mtime)
    except Exception:
        pass

    # Steady-state auto-index. Belt-and-suspenders: the hook already isolates embedding
    # failures onto the report, but a completed ingest must never be undone by a bug in the
    # gating logic itself either.
    try:
        auto_index = _maybe_auto_index(path, on_notice=on_notice)
    except Exception:
        auto_index = None

    elapsed_ms = int((perf_counter() - started) * 1000)
    return IngestRunResult(
        db_path=path,
        db_created=db_created,
        mode="ingest",
        adapters=selected_names,
        scan_paths=list(scan_paths or []),
        stats=stats,
        elapsed_ms=elapsed_ms,
        dropin_failures=dropin_failures,
        auto_index=auto_index,
        adapter_tiers=adapter_tiers,
        disabled_adapters=disabled_adapters,
    )


def _maybe_auto_index(
    db_path: Path, embed_db_path: Path | None = None, *, on_notice: Callable[[str], None] | None = None
) -> AutoIndexReport | None:
    """Embed new/stale conversations at the end of ingest (steady-state only).

    Returns None when there is nothing to say — auto_index disabled, no usable backend, or
    no stale conversations. Skips inline work (surfacing an "awaiting" report) when the index
    is unbuilt or the backlog is large, so the first-run backlog never hangs ingest. Any
    embedding failure is isolated onto ``report.error`` and never propagates. ``on_notice`` is
    invoked with the remote first-egress disclosure BEFORE the embedding call (so it precedes
    the egress); the shown-once flag is persisted only after that emission.
    """
    from siftd.config import get_embed_auto_index

    if not get_embed_auto_index():
        return None

    from siftd.embeddings.availability import embedding_status

    st = embedding_status()
    if not st.usable:
        return None

    from siftd.paths import embeddings_db_path

    embed_db = embed_db_path or embeddings_db_path()

    from siftd.api.search import embed_status

    status = embed_status(db_path=db_path, embed_db_path=embed_db)

    # Nothing new or changed to embed.
    if status.conversations_stale == 0:
        return None

    # Backlog guard: an unbuilt index (first run) or a large stale set is deferred to an
    # explicit `siftd embed` — inline work here could hang ingest for a long time.
    if status.total_chunks == 0:
        return AutoIndexReport(awaiting=status.conversations_stale, skipped_reason="unbuilt")
    if status.conversations_stale > _AUTO_INDEX_BACKLOG_LIMIT:
        return AutoIndexReport(awaiting=status.conversations_stale, skipped_reason="backlog")

    report = AutoIndexReport()
    # Announce the first remote egress BEFORE the content leaves the machine: surface it
    # through the caller's live callback (not the after-the-fact result render), and only
    # then persist the shown-once flag. A caller with NO callback cannot satisfy
    # notice-precedes-egress, so the work is skipped instead — the disclosure rides
    # ``report.notice`` (skipped_reason="notice"), the flag is not burned, and indexing
    # resumes once any surface has shown the notice (interactive ingest, or the first
    # explicit `siftd embed`).
    from siftd.embeddings.egress import mark_egress_notified, pending_egress_notice

    notice = pending_egress_notice(st.backend, embed_db)
    if notice:
        if on_notice is None:
            return AutoIndexReport(
                awaiting=status.conversations_stale, skipped_reason="notice", notice=notice
            )
        report.notice = notice
        on_notice(notice)
        mark_egress_notified(embed_db)

    from siftd.api.search import build_index

    try:
        result = build_index(db_path=db_path, embed_db_path=embed_db, verbose=False)
        report.ran = True
        report.chunks_added = result["chunks_added"]
        report.conversations_indexed = result["conversations_indexed"]
    except Exception as e:  # noqa: BLE001 — any embedding failure is isolated, never aborts ingest
        # A locked DB (concurrent serve), a malformed remote 200 body, an ONNX runtime fault —
        # all degrade to a reported error. KeyboardInterrupt/SystemExit aren't Exception, so
        # they still propagate.
        report.error = str(e)
    return report


def egress_notice_pending(embed_db_path: Path | None = None) -> str | None:
    """The one-time remote first-egress disclosure if it hasn't been shown yet, else None.

    Public counterpart of the auto-index hook's internal check, for surfaces that trigger
    embedding egress directly — the explicit ``siftd embed`` build is often the FIRST
    egress (initial backlog), so it must disclose too. Read-only; pair with
    :func:`mark_egress_notified` once the notice has actually been surfaced.

    Thin wrapper over :mod:`siftd.embeddings.egress`, kept here so existing callers
    (``siftd embed``, this module's own auto-index hook) don't need to change import
    paths — the pure logic lives in ``embeddings`` because ``doctor`` cannot depend on
    the ``api`` layer but needs to read the same pending-notice state.
    """
    from siftd.embeddings.egress import egress_notice_pending as _egress_notice_pending

    return _egress_notice_pending(embed_db_path)


def mark_egress_notified(embed_db_path: Path | None = None) -> None:
    """Persist the first-egress shown-once flag (call only after the notice was surfaced)."""
    from siftd.embeddings.egress import mark_egress_notified_default

    mark_egress_notified_default(embed_db_path)


def run_rebuild_fts(*, db_path: Path) -> IngestRunResult:
    """Rebuild FTS index only (no ingestion)."""
    path = Path(db_path)
    db_created = not path.exists()
    started = perf_counter()

    conn = create_database(path)
    try:
        rebuild_fts_index(conn)
    finally:
        conn.close()

    elapsed_ms = int((perf_counter() - started) * 1000)
    return IngestRunResult(
        db_path=path,
        db_created=db_created,
        mode="rebuild_fts",
        adapters=[],
        scan_paths=[],
        stats=None,
        elapsed_ms=elapsed_ms,
    )
