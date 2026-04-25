"""API helpers for serve status and audit logging."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from siftd.ids import ulid
from siftd.storage.sqlite import ensure_push_log_table, open_database


@dataclass
class HealthStatus:
    """Serve health payload."""

    service: str
    status: str
    db_id: str
    db_size_bytes: int
    conversations: int


def get_health_status(db_path: Path) -> HealthStatus:
    """Return database-backed health status for serve."""
    db_path_str = str(db_path.resolve())
    db_id = hashlib.sha256(db_path_str.encode("utf-8")).hexdigest()
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    conversations = 0

    if db_path.exists():
        conn = open_database(db_path, read_only=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
            conversations = int(row[0]) if row else 0
        finally:
            conn.close()

    return HealthStatus(
        service="siftd",
        status="ok",
        db_id=db_id,
        db_size_bytes=size_bytes,
        conversations=conversations,
    )


def record_push_log(
    *,
    db_path: Path,
    identity: str,
    conversations: int,
    size_bytes: int,
    source_ip: str | None,
    push_id: str | None = None,
) -> None:
    """Record a push event in the push_log table."""
    conn = open_database(db_path)
    try:
        ensure_push_log_table(conn)
        conn.execute(
            "INSERT INTO push_log (push_id, user_identity, pushed_at, conversations, size_bytes, source_ip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                push_id or ulid(),
                identity,
                datetime.now(UTC).isoformat(),
                conversations,
                size_bytes,
                source_ip,
            ),
        )
        conn.commit()
    finally:
        conn.close()
