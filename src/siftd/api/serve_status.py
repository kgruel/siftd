"""API helpers for serve status and audit logging."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from siftd.ids import ulid
from siftd.storage.sqlite import (
    ensure_audit_log_table,
    ensure_push_log_table,
    open_database,
)


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


def record_audit_event(
    *,
    db_path: Path,
    actor: str,
    action: str,
    target_type: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    source_ip: str | None = None,
) -> None:
    """Record a state-changing operation in the audit_log table.

    Best-effort provenance for mutations on the shared multi-tenant DB (tag
    apply/remove/rename/delete, session tag queueing). Never raises into the
    caller — an audit-write failure must not fail the underlying operation. See
    finding F6.
    """
    try:
        conn = open_database(db_path)
        try:
            ensure_audit_log_table(conn)
            conn.execute(
                "INSERT INTO audit_log "
                "(id, actor, action, target_type, target, detail, source_ip, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ulid(),
                    actor,
                    action,
                    target_type,
                    target,
                    detail,
                    source_ip,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        import logging

        logging.getLogger("siftd.serve").warning(
            "audit_log write failed for action=%s target=%s", action, target,
        )
