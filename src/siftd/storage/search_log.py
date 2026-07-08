"""Search-log storage: capture executed searches and later 'opened' signals.

Two side-tables (``search_events``, ``search_opens``), created idempotently via
``ensure_search_log_tables`` in the same ``ensure_*``/no-SCHEMA_VERSION-bump
convention as ``active_sessions``/``tag_pins``/``sync_inbox`` (see
storage/sessions.py, storage/tags.py). Search-log capture is operational
telemetry, not core ingested data — see
docs/dev/search-log-design-2026-07-07.md for the dissolution check.

Reads guard on table presence via ``has_search_log_table`` so a read-only open
of an older-but-not-yet-write-opened DB degrades to "no history" rather than
``no such table``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from siftd.ids import ulid as _ulid

# Cap on how many result IDs are persisted per search (OJ-1). The full result
# count is stored separately in result_count even when the list is truncated.
MAX_RESULT_IDS = 50

# CLI heuristic open-signal window when no session_id links the query <id> back
# to a search (OJ-2).
OPEN_LINK_WINDOW_MINUTES = 30


@dataclass
class SearchEventFingerprint:
    """Which engine config produced a search's ranking (see design doc)."""

    fp_backend: str | None = None
    fp_model: str | None = None
    fp_dimension: int | None = None
    fp_strategy: str = "fts"
    fp_preset: str | None = None
    fp_recall: int | None = None
    fp_mmr_lambda: float | None = None
    fp_mode: str = "hybrid"


def ensure_search_log_tables(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Create search_events and search_opens tables. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_events (
            id            TEXT PRIMARY KEY,
            query         TEXT NOT NULL,
            issued_at     TEXT NOT NULL,
            issuer        TEXT NOT NULL,
            owner         TEXT NOT NULL DEFAULT '',

            fp_backend    TEXT,
            fp_model      TEXT,
            fp_dimension  INTEGER,
            fp_strategy   TEXT NOT NULL,
            fp_preset     TEXT,
            fp_recall     INTEGER,
            fp_mmr_lambda REAL,
            fp_mode       TEXT NOT NULL,
            executed_mode TEXT NOT NULL,

            result_ids    TEXT NOT NULL,
            result_count  INTEGER NOT NULL,

            workspace     TEXT,
            session_id    TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_events_owner_time
        ON search_events(owner, issued_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_events_session
        ON search_events(session_id) WHERE session_id IS NOT NULL
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_opens (
            id               TEXT PRIMARY KEY,
            search_event_id  TEXT NOT NULL REFERENCES search_events(id) ON DELETE CASCADE,
            conversation_id  TEXT NOT NULL,
            rank             INTEGER,
            opened_at        TEXT NOT NULL,
            surface          TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_search_opens_event
        ON search_opens(search_event_id)
    """)

    if commit:
        conn.commit()


def has_search_log_table(conn: sqlite3.Connection) -> bool:
    """Whether search_events exists — guards reads on a table-less DB."""
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_events'"
    )
    return cur.fetchone() is not None


def record_search(
    conn: sqlite3.Connection,
    *,
    query: str,
    issuer: str,
    fingerprint: SearchEventFingerprint,
    executed_mode: str,
    result_ids: list[str],
    result_count: int,
    owner: str = "",
    workspace: str | None = None,
    session_id: str | None = None,
    commit: bool = False,
) -> str:
    """Insert one search_events row. Returns the new row's ULID."""
    ulid = _ulid()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO search_events (
            id, query, issued_at, issuer, owner,
            fp_backend, fp_model, fp_dimension, fp_strategy, fp_preset,
            fp_recall, fp_mmr_lambda, fp_mode, executed_mode,
            result_ids, result_count, workspace, session_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ulid, query, now, issuer, owner,
            fingerprint.fp_backend, fingerprint.fp_model, fingerprint.fp_dimension,
            fingerprint.fp_strategy, fingerprint.fp_preset,
            fingerprint.fp_recall, fingerprint.fp_mmr_lambda, fingerprint.fp_mode, executed_mode,
            json.dumps(result_ids[:MAX_RESULT_IDS]), result_count, workspace, session_id,
        ),
    )
    if commit:
        conn.commit()
    return ulid


def recent_searches(
    conn: sqlite3.Connection,
    *,
    owner: str = "",
    limit: int = 20,
) -> list[dict]:
    """Return the last `limit` searches for this owner, most recent first.

    Each row carries an ``opened`` flag — whether any search_opens row links
    back to the search (the weak opened-signal; see the design doc's
    epistemics note) — so the history surface can mark linked opens without
    a second query.
    """
    if not has_search_log_table(conn):
        return []
    cur = conn.execute(
        """
        SELECT id, query, issued_at, issuer, executed_mode, result_count,
               EXISTS(
                   SELECT 1 FROM search_opens o WHERE o.search_event_id = search_events.id
               ) AS opened
        FROM search_events
        WHERE owner = ?
        ORDER BY issued_at DESC, id DESC
        LIMIT ?
        """,
        (owner, limit),
    )
    return [{**dict(row), "opened": bool(row["opened"])} for row in cur.fetchall()]


def record_open(
    conn: sqlite3.Connection,
    *,
    search_event_id: str,
    conversation_id: str,
    rank: int | None,
    surface: str,
    commit: bool = False,
) -> str:
    """Insert one search_opens row. Returns the new row's ULID."""
    ulid = _ulid()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO search_opens (id, search_event_id, conversation_id, rank, opened_at, surface)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ulid, search_event_id, conversation_id, rank, now, surface),
    )
    if commit:
        conn.commit()
    return ulid


def find_open_link(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    owner: str = "",
    session_id: str | None = None,
    window_minutes: int = OPEN_LINK_WINDOW_MINUTES,
) -> tuple[str, int | None] | None:
    """Find the most recent search this conversation_id's open should bind to.

    Binds to a search that (a) shares `session_id`, OR (b) absent a session,
    was issued within `window_minutes` for the same owner — AND, in both
    cases, has `conversation_id` in its result_ids (the load-bearing safety
    that keeps an unrelated `query <id>` from creating a spurious label; see
    OJ-2 in the design doc). Returns (search_event_id, rank) or None.
    """
    if not has_search_log_table(conn):
        return None

    if session_id:
        rows = conn.execute(
            """
            SELECT id, result_ids FROM search_events
            WHERE session_id = ? AND owner = ?
            ORDER BY issued_at DESC, id DESC
            """,
            (session_id, owner),
        ).fetchall()
    else:
        cutoff = (datetime.now(UTC) - timedelta(minutes=window_minutes)).isoformat()
        rows = conn.execute(
            """
            SELECT id, result_ids FROM search_events
            WHERE owner = ? AND issued_at >= ?
            ORDER BY issued_at DESC, id DESC
            """,
            (owner, cutoff),
        ).fetchall()

    for row in rows:
        ids = json.loads(row["result_ids"])
        if conversation_id in ids:
            return row["id"], ids.index(conversation_id) + 1  # 1-based rank
    return None
