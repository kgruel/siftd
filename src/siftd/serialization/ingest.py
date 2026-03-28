"""Ingest result serialization helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass
class IngestStatsPayload:
    files_found: int
    files_ingested: int
    files_skipped: int
    files_replaced: int
    files_errored: int
    conversations: int
    prompts: int
    responses: int
    tool_calls: int
    by_harness: dict[str, dict[str, int]]


@dataclass
class IngestRunPayload:
    db_path: str
    db_created: bool
    mode: Literal["ingest", "rebuild_fts"]
    adapters: list[str]
    scan_paths: list[str]
    stats: IngestStatsPayload | None
    elapsed_ms: int


def _to_stats_payload(stats: Any) -> IngestStatsPayload:
    return IngestStatsPayload(
        files_found=stats.files_found,
        files_ingested=stats.files_ingested,
        files_skipped=stats.files_skipped,
        files_replaced=stats.files_replaced,
        files_errored=stats.files_errored,
        conversations=stats.conversations,
        prompts=stats.prompts,
        responses=stats.responses,
        tool_calls=stats.tool_calls,
        by_harness=dict(stats.by_harness),
    )


def to_ingest_run_payload(result: Any) -> IngestRunPayload:
    stats = result.stats
    payload_stats = _to_stats_payload(stats) if stats is not None else None
    return IngestRunPayload(
        db_path=str(result.db_path),
        db_created=result.db_created,
        mode=result.mode,
        adapters=list(result.adapters),
        scan_paths=list(result.scan_paths),
        stats=payload_stats,
        elapsed_ms=result.elapsed_ms,
    )


def serialize_ingest_run_payload(payload: IngestRunPayload) -> dict[str, object]:
    return asdict(payload)


def serialize_ingest_run_result(result: Any) -> dict[str, object]:
    return serialize_ingest_run_payload(to_ingest_run_payload(result))
