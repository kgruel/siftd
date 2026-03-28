"""Backfill result serialization helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass
class BackfillRunPayload:
    db_path: str
    operation: Literal[
        "response_attributes",
        "shell_tags",
        "derivative_tags",
        "filter_binary",
    ]
    dry_run: bool
    inserted_attributes: int
    tagged_conversations: int
    shell_tag_counts: dict[str, int]
    filtered: int
    skipped: int
    errors: int
    elapsed_ms: int


def to_backfill_run_payload(result: Any) -> BackfillRunPayload:
    return BackfillRunPayload(
        db_path=str(result.db_path),
        operation=result.operation,
        dry_run=result.dry_run,
        inserted_attributes=result.inserted_attributes,
        tagged_conversations=result.tagged_conversations,
        shell_tag_counts=dict(result.shell_tag_counts),
        filtered=result.filtered,
        skipped=result.skipped,
        errors=result.errors,
        elapsed_ms=result.elapsed_ms,
    )


def serialize_backfill_run_payload(payload: BackfillRunPayload) -> dict[str, object]:
    return asdict(payload)


def serialize_backfill_run_result(result: Any) -> dict[str, object]:
    return serialize_backfill_run_payload(to_backfill_run_payload(result))
