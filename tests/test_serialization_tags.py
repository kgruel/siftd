from __future__ import annotations

from dataclasses import fields

from siftd.api.tags import ApplyResult, ApplyTagOutcome, DeleteResult, RenameResult
from siftd.serialization.tags import (
    ApplyPayload,
    ApplyPayloadItem,
    DeletePayload,
    RenamePayload,
    serialize_apply_payload,
    serialize_delete_payload,
    serialize_rename_payload,
    to_apply_payload,
    to_delete_payload,
    to_rename_payload,
)


def test_apply_payload_serializer_key_drift_guard():
    result = ApplyResult(
        action="apply",
        results=[
            ApplyTagOutcome(tag="topic:x", status="already_applied", count=0),
            ApplyTagOutcome(tag="topic:y", status="not_found", count=0),
        ],
        target_count=1,
        entity_type="conversation",
        resolved_entity_id="cid",
    )

    payload = to_apply_payload(result)
    out = serialize_apply_payload(payload)

    assert set(out.keys()) == {f.name for f in fields(ApplyPayload)}
    assert set(out["results"][0].keys()) == {f.name for f in fields(ApplyPayloadItem)}

    # Wire-contract normalization
    assert out["results"][0]["status"] == "applied"
    assert out["results"][1]["status"] == "not_found"


def test_rename_payload_serializer_key_drift_guard():
    payload = to_rename_payload(RenameResult(status="renamed", old_name="a", new_name="b"))
    out = serialize_rename_payload(payload)

    assert set(out.keys()) == {f.name for f in fields(RenamePayload)}


def test_delete_payload_serializer_key_drift_guard():
    payload = to_delete_payload(DeleteResult(status="deleted", tag_name="x"))
    out = serialize_delete_payload(payload)

    assert set(out.keys()) == {f.name for f in fields(DeletePayload)}
