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
from siftd.api.ingest import AutoIndexReport
from siftd.serialization.ingest import (
    AutoIndexPayload,
    IngestRunPayload,
    IngestStatsPayload,
    serialize_ingest_run_payload,
    to_ingest_run_payload,
)

# IngestRunResult fields that intentionally do NOT cross the wire payload.
# - dropin_failures: a CLI-only adapter-health surface (list of (Path, str)); pre-existing
#   exclusion, out of scope for this slice — do not "fix" it here.
_INGEST_RESULT_WIRE_EXCLUSIONS = {"dropin_failures"}


def test_ingest_payload_covers_every_result_field():
    """Every IngestRunResult field is represented in IngestRunPayload except an explicit,
    documented exclusion list — this catches a new result field silently dropped from the
    wire (exactly how auto_index was missed on first pass)."""
    result_fields = {f.name for f in fields(IngestRunResult)}
    payload_fields = {f.name for f in fields(IngestRunPayload)}
    assert result_fields - _INGEST_RESULT_WIRE_EXCLUSIONS == payload_fields


def test_ingest_payload_serializer_shapes():
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
        auto_index=AutoIndexReport(ran=True, chunks_added=5, conversations_indexed=2),
    )

    payload = to_ingest_run_payload(result)
    out = serialize_ingest_run_payload(payload)

    assert set(out.keys()) == {f.name for f in fields(IngestRunPayload)}
    assert set(out["stats"].keys()) == {f.name for f in fields(IngestStatsPayload)}
    assert set(out["auto_index"].keys()) == {f.name for f in fields(AutoIndexPayload)}
    assert out["auto_index"]["chunks_added"] == 5 and out["auto_index"]["conversations_indexed"] == 2


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
