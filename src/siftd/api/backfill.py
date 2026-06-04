"""Backfill API wrappers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Literal

from siftd.api.database import open_database
from siftd.backfill import (
    backfill_derivative_tags,
    backfill_filter_binary,
    backfill_models,
    backfill_response_attributes,
    backfill_shell_tags,
)

BackfillOperation = Literal[
    "response_attributes",
    "shell_tags",
    "derivative_tags",
    "filter_binary",
    "models",
    "pricing",
]


@dataclass
class BackfillRunResult:
    """Result metadata for a backfill API run."""

    db_path: Path
    operation: BackfillOperation
    dry_run: bool
    inserted_attributes: int = 0
    tagged_conversations: int = 0
    shell_tag_counts: dict[str, int] = field(default_factory=dict)
    filtered: int = 0
    skipped: int = 0
    errors: int = 0
    updated_models: int = 0
    repriced_rows: int = 0
    elapsed_ms: int = 0


__all__ = [
    "BackfillOperation",
    "BackfillRunResult",
    "run_backfill",
]


def run_backfill(
    *,
    db_path: Path,
    operation: BackfillOperation = "response_attributes",
    dry_run: bool = False,
) -> BackfillRunResult:
    """Run a backfill operation with API-owned DB lifecycle."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")

    started = perf_counter()
    conn = open_database(path)

    result = BackfillRunResult(
        db_path=path,
        operation=operation,
        dry_run=dry_run,
    )

    try:
        if operation == "shell_tags":
            result.shell_tag_counts = backfill_shell_tags(conn)
        elif operation == "derivative_tags":
            result.tagged_conversations = backfill_derivative_tags(conn)
        elif operation == "filter_binary":
            stats = backfill_filter_binary(conn, dry_run=dry_run)
            result.filtered = int(stats.get("filtered", 0))
            result.skipped = int(stats.get("skipped", 0))
            result.errors = int(stats.get("errors", 0))
        elif operation == "response_attributes":
            result.inserted_attributes = backfill_response_attributes(conn)
        elif operation == "models":
            # Re-parse raw_name → canonical name for rows the parser previously fell
            # back on (e.g. after a parse_model_name improvement). The pricing
            # reference reprojects onto any newly-canonical model on the next open;
            # cost refreshes on the next ingest/rollup rebuild.
            result.updated_models = backfill_models(conn)
        elif operation == "pricing":
            # open_database already reprojected the pricing reference onto the table
            # (ensure_pricing_table runs on every open). Re-materialize the rollup so
            # cost reflects the current reference — the rebuild trigger that ingest and
            # merge have but a bare price edit lacks. No log scan; just raw × pricing.
            from siftd.storage.usage_rollup import rebuild_rollups
            result.repriced_rows = rebuild_rollups(conn, commit=True)
        else:
            raise ValueError(f"Unsupported backfill operation: {operation}")
    finally:
        conn.close()

    result.elapsed_ms = int((perf_counter() - started) * 1000)
    return result
