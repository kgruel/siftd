"""Anti-drift tests for ingest/backfill result serialization."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from siftd.api.backfill import BackfillRunResult
from siftd.api.ingest import IngestRunResult
from siftd.ingestion import IngestStats
from siftd.serialization.backfill import (
    BackfillRunPayload,
    serialize_backfill_run_payload,
    to_backfill_run_payload,
)
from siftd.serialization.ingest import (
    IngestRunPayload,
    IngestStatsPayload,
    serialize_ingest_run_payload,
    to_ingest_run_payload,
)


def test_ingest_payload_serializer_key_drift_guard():
    result = IngestRunResult(
        db_path=Path("/tmp/siftd.db"),
        db_created=True,
        mode="ingest",
        adapters=["claude_code"],
        scan_paths=["/logs"],
        stats=IngestStats(
            files_found=2,
            files_ingested=1,
            files_skipped=1,
            files_replaced=0,
            files_errored=0,
            conversations=1,
            prompts=2,
            responses=2,
            tool_calls=3,
            by_harness={"claude_code": {"conversations": 1}},
        ),
        elapsed_ms=42,
    )

    payload = to_ingest_run_payload(result)
    out = serialize_ingest_run_payload(payload)

    assert set(out.keys()) == {f.name for f in fields(IngestRunPayload)}
    assert set(out["stats"].keys()) == {f.name for f in fields(IngestStatsPayload)}


def test_backfill_payload_serializer_key_drift_guard():
    payload = to_backfill_run_payload(
        BackfillRunResult(
            db_path=Path("/tmp/siftd.db"),
            operation="filter_binary",
            dry_run=True,
            inserted_attributes=0,
            tagged_conversations=0,
            shell_tag_counts={"git": 2},
            filtered=3,
            skipped=4,
            errors=1,
            elapsed_ms=10,
        )
    )
    out = serialize_backfill_run_payload(payload)

    assert set(out.keys()) == {f.name for f in fields(BackfillRunPayload)}
