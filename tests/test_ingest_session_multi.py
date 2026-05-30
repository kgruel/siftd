"""Regression tests for C01 (comprehensive-review 2026-05-28).

A session-strategy adapter (DEDUP_STRATEGY = "session", e.g. OpenCode/Gemini)
yields one Conversation per session from a single source file. The ingest loop
used to funnel every source through `_get_single_conversation`, which raises
when a source produces more than one conversation — so any real multi-session
DB errored out and stored ZERO conversations.

These tests pin the general contract (a session source with N conversations
ingests all N), and the re-ingest traps: adding a session on a later run, and
replacing a single session without losing the per-file marker to the
conversation_id ON DELETE CASCADE.
"""

from __future__ import annotations

import json
import sqlite3

from conftest import make_conversation

from siftd.adapters import opencode
from siftd.domain.source import Source
from siftd.ingestion import ingest_all
from siftd.storage.sqlite import create_database

_TS = 1710079200000
_OPENCODE_SCHEMA = [
    "CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT, version INTEGER, time_created INTEGER, time_updated INTEGER)",
    "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)",
    "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)",
]


def _make_opencode_db(path, session_ids):
    """Build a real OpenCode SQLite DB with one minimal session per id."""
    conn = sqlite3.connect(str(path))
    for ddl in _OPENCODE_SCHEMA:
        conn.execute(ddl)
    for i, sid in enumerate(session_ids):
        ts = _TS + i * 100000
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
            (sid, "proj", "/ws", "Test", 1, ts, ts + 60000),
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (f"{sid}-u", sid, ts + 1000, ts + 1000, json.dumps({"role": "user", "summary": {"title": "Hi"}})),
        )
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (f"{sid}-up", f"{sid}-u", sid, ts + 1000, ts + 1000, json.dumps({"type": "text", "text": "hello"})),
        )
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (f"{sid}-a", sid, ts + 2000, ts + 2000, json.dumps({"role": "assistant", "modelID": "m1", "providerID": "anthropic"})),
        )
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (f"{sid}-ap", f"{sid}-a", sid, ts + 2000, ts + 2000, json.dumps({"type": "text", "text": "hi back"})),
        )
    conn.commit()
    conn.close()


def _opencode_adapter_at(db_path):
    """The real opencode parser, with discover() pinned to a test DB."""

    class _Adapter:
        NAME = opencode.NAME
        DEDUP_STRATEGY = opencode.DEDUP_STRATEGY
        HARNESS_SOURCE = getattr(opencode, "HARNESS_SOURCE", "opencode")
        can_handle = staticmethod(opencode.can_handle)
        parse = staticmethod(opencode.parse)

        @staticmethod
        def discover():
            yield Source(kind="sqlite", location=db_path)

    return _Adapter


def _session_adapter(db_file, ref):
    """A session-strategy adapter whose parse() returns ref['items']."""

    class _Adapter:
        NAME = "test_session"
        DEDUP_STRATEGY = "session"
        HARNESS_SOURCE = "test"

        @staticmethod
        def can_handle(source):
            return True

        @staticmethod
        def parse(source):
            return list(ref["items"])

        @staticmethod
        def discover():
            yield Source(kind="file", location=str(db_file))

    return _Adapter


def _external_ids(conn):
    return [r[0] for r in conn.execute("SELECT external_id FROM conversations ORDER BY external_id")]


def _file_rows(conn):
    return conn.execute("SELECT path, conversation_id FROM ingested_files").fetchall()


def test_session_source_with_multiple_conversations_ingests_all(tmp_path):
    """First ingest of a 2-session source stores both conversations (C01)."""
    conn = create_database(tmp_path / "db.sqlite")
    db_file = tmp_path / "sessions.db"
    db_file.write_text("v1")

    ref = {
        "items": [
            make_conversation(external_id="sess-a", ended_at="2024-01-01T11:00:00Z"),
            make_conversation(external_id="sess-b", ended_at="2024-01-02T11:00:00Z"),
        ]
    }
    stats = ingest_all(conn, [_session_adapter(db_file, ref)])

    assert stats.conversations == 2
    assert stats.files_ingested == 1  # one FILE, two conversations
    assert stats.files_errored == 0
    assert _external_ids(conn) == ["sess-a", "sess-b"]

    # Exactly one per-file marker, with NULL conversation_id.
    rows = _file_rows(conn)
    assert len(rows) == 1
    assert rows[0][1] is None
    conn.close()


def test_reingest_adds_a_new_session_and_dedups_existing(tmp_path):
    """A changed multi-session DB picks up the new session without duplicating."""
    conn = create_database(tmp_path / "db.sqlite")
    db_file = tmp_path / "sessions.db"
    db_file.write_text("v1")

    a = make_conversation(external_id="sess-a", ended_at="2024-01-01T11:00:00Z")
    b = make_conversation(external_id="sess-b", ended_at="2024-01-02T11:00:00Z")
    ref = {"items": [a, b]}
    adapter = _session_adapter(db_file, ref)
    ingest_all(conn, [adapter])

    # Add a session; grow the file so the stat/hash fast-path doesn't skip it.
    db_file.write_text("v2-with-more-bytes-so-size-changes")
    c = make_conversation(external_id="sess-c", ended_at="2024-01-03T11:00:00Z")
    ref["items"] = [a, b, c]
    stats2 = ingest_all(conn, [adapter])

    # Existing sessions deduped (no duplicates), new session inserted.
    assert _external_ids(conn) == ["sess-a", "sess-b", "sess-c"]
    assert stats2.conversations == 1  # only the newly-arrived session counts
    assert stats2.files_ingested == 1
    assert len(_file_rows(conn)) == 1
    conn.close()


def test_reingest_replaces_newer_session_and_keeps_file_marker(tmp_path):
    """Replacing one session must not cascade-delete the per-file marker.

    The marker carries conversation_id=NULL precisely so delete_conversation
    (ON DELETE CASCADE) on a replaced session cannot wipe the file's fast-path
    record.
    """
    conn = create_database(tmp_path / "db.sqlite")
    db_file = tmp_path / "sessions.db"
    db_file.write_text("v1")

    a = make_conversation(external_id="sess-a", ended_at="2024-01-01T11:00:00Z")
    b = make_conversation(external_id="sess-b", ended_at="2024-01-02T11:00:00Z")
    ref = {"items": [a, b]}
    adapter = _session_adapter(db_file, ref)
    ingest_all(conn, [adapter])

    # sess-b gets a later ended_at -> should replace; sess-a unchanged.
    db_file.write_text("v2-with-more-bytes-so-size-changes")
    b_newer = make_conversation(external_id="sess-b", ended_at="2024-01-09T11:00:00Z")
    ref["items"] = [a, b_newer]
    stats2 = ingest_all(conn, [adapter])

    assert stats2.files_replaced == 1
    assert stats2.files_ingested == 0
    assert _external_ids(conn) == ["sess-a", "sess-b"]  # both still present

    # The marker survived the replace and remains a single NULL-conv row.
    rows = _file_rows(conn)
    assert len(rows) == 1
    assert rows[0][1] is None

    # DB is FK-clean.
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_real_opencode_db_with_two_sessions_ingests_both(tmp_path):
    """Report recommendation #2: a real multi-session opencode.db, full ingest_all.

    Exercises the real OpenCode parser through the real orchestration loop —
    the path the static-only C01 repro could not cover.
    """
    db_path = tmp_path / "opencode.db"
    _make_opencode_db(db_path, ["ses_a", "ses_b"])

    conn = create_database(tmp_path / "siftd.db")
    stats = ingest_all(conn, [_opencode_adapter_at(db_path)])

    assert stats.files_errored == 0
    assert stats.conversations == 2
    assert stats.files_ingested == 1
    assert _external_ids(conn) == ["opencode::ses_a", "opencode::ses_b"]
    conn.close()
