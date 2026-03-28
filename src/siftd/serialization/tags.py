"""Tag mutation serialization helpers for serve routes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

TagMutationResult = Literal["applied", "removed", "not_found", "already_applied", "not_applied"]


@dataclass
class ApplyPayloadItem:
    tag: str
    status: Literal["applied", "removed", "not_found"]
    count: int


@dataclass
class ApplyPayload:
    action: Literal["apply", "remove"]
    results: list[ApplyPayloadItem]


@dataclass
class RenamePayload:
    status: str
    old_name: str
    new_name: str


@dataclass
class DeletePayload:
    status: str
    tag_name: str


def to_apply_payload(result: Any) -> ApplyPayload:
    """Normalize ApplyResult statuses to the /api/v1/tag wire contract."""
    status_map: dict[TagMutationResult, Literal["applied", "removed", "not_found"]] = {
        "applied": "applied",
        "removed": "removed",
        "not_found": "not_found",
        "already_applied": "applied",
        "not_applied": "removed",
    }
    items = [
        ApplyPayloadItem(
            tag=row.tag,
            status=status_map[row.status],
            count=row.count,
        )
        for row in result.results
    ]
    return ApplyPayload(action=result.action, results=items)


def to_rename_payload(result: Any) -> RenamePayload:
    return RenamePayload(status=result.status, old_name=result.old_name, new_name=result.new_name)


def to_delete_payload(result: Any) -> DeletePayload:
    return DeletePayload(status=result.status, tag_name=result.tag_name)


def serialize_apply_payload(payload: ApplyPayload) -> dict[str, object]:
    return asdict(payload)


def serialize_rename_payload(payload: RenamePayload) -> dict[str, object]:
    return asdict(payload)


def serialize_delete_payload(payload: DeletePayload) -> dict[str, object]:
    return asdict(payload)


def serialize_apply_result(result: Any) -> dict[str, object]:
    return serialize_apply_payload(to_apply_payload(result))


def serialize_rename_result(result: Any) -> dict[str, object]:
    return serialize_rename_payload(to_rename_payload(result))


def serialize_delete_result(result: Any) -> dict[str, object]:
    return serialize_delete_payload(to_delete_payload(result))
