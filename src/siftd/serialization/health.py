"""Canonical JSON serialization for serve health status."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from siftd.api.serve_status import HealthStatus


def serialize_health_status(status: HealthStatus) -> dict[str, Any]:
    """Serialize HealthStatus to a JSON-safe dict."""
    return {
        "service": status.service,
        "status": status.status,
        "db_id": status.db_id,
        "db_size_bytes": status.db_size_bytes,
        "conversations": status.conversations,
    }
