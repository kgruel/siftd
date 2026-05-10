"""Ingest API wrappers.

Provides API-level write primitives for ingestion and FTS rebuild operations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal

from siftd.adapters.registry import load_all_adapters, wrap_adapter_paths
from siftd.api.database import create_database
from siftd.api.search import rebuild_fts_index
from siftd.ingestion import IngestEvent, IngestStats, ingest_all


class AdapterSelectionError(ValueError):
    """Raised when requested adapter names match no discovered adapters."""

    def __init__(self, requested: list[str], available: list[str]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(f"No adapters matched: {', '.join(requested)}")


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


__all__ = [
    "AdapterSelectionError",
    "IngestRunResult",
    "run_ingest",
    "run_rebuild_fts",
]


def _resolve_adapters(
    *,
    adapter_names: list[str] | None,
    scan_paths: list[str] | None,
    failures_out: list[tuple[Path, str]] | None = None,
) -> tuple[list, list[str]]:
    """Resolve discovered adapter modules with optional filtering/overrides."""
    plugins = load_all_adapters(failures_out=failures_out)

    if adapter_names:
        requested = set(adapter_names)
        plugins = [p for p in plugins if p.name in requested]
        if not plugins:
            raise AdapterSelectionError(requested=adapter_names, available=[])

    if scan_paths:
        adapters = [wrap_adapter_paths(p.module, scan_paths) for p in plugins]
    else:
        adapters = [p.module for p in plugins]

    return adapters, [p.name for p in plugins]


def run_ingest(
    *,
    db_path: Path,
    adapter_names: list[str] | None = None,
    scan_paths: list[str] | None = None,
    filter_binary: bool | None = None,
    on_event: Callable[[IngestEvent], None] | None = None,
) -> IngestRunResult:
    """Run ingestion from discovered adapters.

    API owns DB lifecycle for this write operation.
    """
    path = Path(db_path)
    db_created = not path.exists()
    started = perf_counter()

    dropin_failures: list[tuple[Path, str]] = []
    conn = create_database(path)
    try:
        adapters, selected_names = _resolve_adapters(
            adapter_names=adapter_names,
            scan_paths=scan_paths,
            failures_out=dropin_failures,
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
        from siftd.api.stats import get_stats, write_stats_cache

        write_stats_cache(get_stats(db_path=path))
    except Exception:
        pass

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
    )


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
