"""JSON serialization for event detail.

The shape is owned by EventDetail.to_dict() in siftd.api.events; this
module is a thin pass-through so serve and CLI consumers go through the
expected serialization layer for indirection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from siftd.api.events import EventDetail


def serialize_event_detail(detail: EventDetail) -> dict[str, Any]:
    """Serialize an EventDetail to a JSON-safe dict.

    Delegates to EventDetail.to_dict() so the shape stays in one place.
    """
    return detail.to_dict()
