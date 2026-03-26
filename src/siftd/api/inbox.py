"""Sync inbox — stage received payloads for deferred merge.

The inbox decouples payload delivery from processing. A push sender gets
a fast ACK after the payload is staged; the merge happens later via
``process_inbox()``.

Staged payloads live as SQLite files in ``paths.inbox_dir()`` and are
tracked in the ``sync_inbox`` table of the target database.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from siftd.storage.sqlite import open_database


def stage_payload(
    payload_path: Path,
    db_path: Path,
    *,
    source_host: str | None = None,
) -> dict:
    """Move a push payload into the inbox and record it in sync_inbox.

    Returns a dict with ``id`` and ``status`` suitable for JSON response.
    """
    from siftd.paths import inbox_dir

    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)

    from siftd.ids import ulid

    payload_id = ulid()
    inbox_payload_path = inbox / f"{payload_id}.db"
    size_bytes = payload_path.stat().st_size

    shutil.move(str(payload_path), str(inbox_payload_path))

    conn = open_database(db_path)
    try:
        conn.execute(
            """INSERT INTO sync_inbox
               (id, received_at, status, source_host, size_bytes)
               VALUES (?, ?, 'staged', ?, ?)""",
            (payload_id, datetime.now(UTC).isoformat(), source_host,
             size_bytes),
        )
        conn.commit()
    except Exception:
        inbox_payload_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    return {"id": payload_id, "status": "staged"}


def process_inbox(db_path: Path) -> list[dict]:
    """Merge all staged inbox payloads into the target database.

    Returns a list of per-payload result dicts with ``id``, ``status``,
    ``conversations`` (on success), or ``error`` (on failure).
    """
    from siftd.paths import inbox_dir

    inbox = inbox_dir()
    conn = open_database(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM sync_inbox WHERE status = 'staged' ORDER BY received_at",
        ).fetchall()
    finally:
        conn.close()

    results: list[dict] = []
    for (payload_id,) in rows:
        payload_path = inbox / f"{payload_id}.db"
        result = _process_one(db_path, payload_id, payload_path)
        results.append(result)

    return results


def _process_one(db_path: Path, payload_id: str, payload_path: Path) -> dict:
    """Process a single staged payload. Returns a result dict."""
    now = datetime.now(UTC).isoformat()

    # Atomically claim staged payload (avoid double-processing with concurrent processors).
    conn = open_database(db_path)
    try:
        cur = conn.execute(
            """UPDATE sync_inbox
               SET status = 'processing', processed_at = NULL, error = NULL, conversations = NULL
               WHERE id = ? AND status = 'staged'""",
            (payload_id,),
        )
        conn.commit()
    finally:
        conn.close()

    if cur.rowcount == 0:
        return {"id": payload_id, "status": "skipped"}

    if not payload_path.exists():
        _update_status(db_path, payload_id, "error", now,
                       error="Staged file missing")
        return {"id": payload_id, "status": "error",
                "error": "Staged file missing"}

    try:
        from siftd.api.receive import receive_database

        result = receive_database(payload_path, db_path, rebuild_fts=True)
        conversations = result.get("conversations", 0)

        _update_status(db_path, payload_id, "done", now,
                       conversations=conversations)

        # Clean up staged file
        payload_path.unlink(missing_ok=True)

        return {"id": payload_id, "status": "done",
                "conversations": conversations}
    except Exception as e:
        _update_status(db_path, payload_id, "error", now, error=str(e))
        return {"id": payload_id, "status": "error", "error": str(e)}


def _update_status(
    db_path: Path,
    payload_id: str,
    status: str,
    processed_at: str | None = None,
    *,
    error: str | None = None,
    conversations: int | None = None,
) -> None:
    """Update a sync_inbox row."""
    conn = open_database(db_path)
    try:
        conn.execute(
            """UPDATE sync_inbox
               SET status = ?, processed_at = ?, error = ?, conversations = ?
               WHERE id = ?""",
            (status, processed_at, error, conversations, payload_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_inbox_status(db_path: Path) -> dict:
    """Return inbox status summary.

    Returns dict with ``pending``, ``total``, and optionally ``last``
    (the most recent entry).
    """
    if not db_path.exists():
        return {"pending": 0, "total": 0}

    conn = open_database(db_path)
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM sync_inbox WHERE status IN ('staged', 'processing')",
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM sync_inbox",
        ).fetchone()[0]

        last_row = conn.execute(
            """SELECT id, received_at, processed_at, status, error,
                      size_bytes, conversations
               FROM sync_inbox ORDER BY received_at DESC LIMIT 1""",
        ).fetchone()
    except sqlite3.OperationalError:
        # Table doesn't exist (old DB without migration)
        return {"pending": 0, "total": 0}
    finally:
        conn.close()

    result: dict = {"pending": pending, "total": total}
    if last_row:
        result["last"] = {
            "id": last_row[0],
            "received_at": last_row[1],
            "processed_at": last_row[2],
            "status": last_row[3],
            "error": last_row[4],
            "size_bytes": last_row[5],
            "conversations": last_row[6],
        }
    return result
