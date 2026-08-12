"""Regression tests for #36: aider history files with more than one session.

Aider appends every session for a project to one `.aider.chat.history.md`, so
the adapter yields one conversation per `# aider chat started at` header. The
adapter declared `DEDUP_STRATEGY = "file"`, whose ingest branch fails any
source yielding more than one conversation — so an aider file worked for
exactly one session and then failed permanently, storing nothing.

These drive the **real** aider parser through the **real** `ingest_all`, which
is the only layer where the bug was visible: the golden fixture asserted
`parse()` yields two conversations and passed for the adapter's whole life.

Three properties are pinned, one per defect the fix had to close:

- both sessions ingest (the reported symptom);
- an append to the live session lands, and only that session is touched (the
  trap in flipping the strategy alone — `ended_at` is the branch's change
  detector and aider has no end timestamp, so every session would have frozen
  at its first ingest instead);
- a file already poisoned by the bug heals on upgrade, without the user having
  to make the file change again.
"""

from __future__ import annotations

import pytest

from siftd.adapters import aider
from siftd.ingestion import ingest_all
from siftd.storage.sqlite import create_database

_SESSION_ONE = """# aider chat started at 2025-07-15 14:32:01

#### write a hello world script

Here it is.

> Tokens: 2.1k sent, 256 received. Cost: $0.01
"""

_SESSION_TWO = """
# aider chat started at 2025-07-15 15:10:00

#### fix the bug in auth.py

Fixed.

> Tokens: 4.5k sent, 1.2k received. Cost: $0.05
"""

_SESSION_THREE = """
# aider chat started at 2025-07-15 16:00:00

#### add a test

Added.
"""


def _aider_adapter_at(project_dir, *, strategy=None):
    """The real aider parser, discovering only under `project_dir`.

    `strategy` overrides `DEDUP_STRATEGY` so a test can reproduce the shipped
    pre-fix behaviour; it defaults to whatever the adapter declares.
    """

    class _Adapter:
        ADAPTER_INTERFACE_VERSION = 1
        NAME = aider.NAME
        DEFAULT_LOCATIONS: list[str] = []
        DEDUP_STRATEGY = strategy or aider.DEDUP_STRATEGY
        HARNESS_SOURCE = aider.HARNESS_SOURCE
        HARNESS_LOG_FORMAT = aider.HARNESS_LOG_FORMAT
        HARNESS_DISPLAY_NAME = aider.HARNESS_DISPLAY_NAME
        TOOL_ALIASES = aider.TOOL_ALIASES
        can_handle = staticmethod(aider.can_handle)
        parse = staticmethod(aider.parse)

        @staticmethod
        def discover(locations=None):
            yield from aider.discover([str(project_dir)])

    return _Adapter


@pytest.fixture
def project(tmp_path):
    """A project directory holding a one-session aider history file."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".aider.chat.history.md").write_text(_SESSION_ONE)
    return proj


def _history(project):
    return project / ".aider.chat.history.md"


def _append(project, text):
    """Grow the history file the way aider does."""
    path = _history(project)
    path.write_text(path.read_text() + text)


def _conversations(conn):
    """{session header timestamp: (conversation id, ended_at)}."""
    return {
        row["external_id"].split("::")[-1]: (row["id"], row["ended_at"])
        for row in conn.execute("SELECT id, external_id, ended_at FROM conversations")
    }


def _exchange_count(conn, conversation_id):
    return conn.execute(
        "SELECT count(*) FROM events WHERE conversation_id = ? AND kind = 'prompt'",
        (conversation_id,),
    ).fetchone()[0]


def _file_row(conn):
    return conn.execute(
        "SELECT path, conversation_id, error FROM ingested_files"
    ).fetchone()


def test_two_session_history_file_ingests_both(tmp_path, project):
    """The reported defect: a second session must not fail the whole file."""
    _append(project, _SESSION_TWO)
    conn = create_database(tmp_path / "siftd.db")

    stats = ingest_all(conn, [_aider_adapter_at(project)])

    assert stats.files_errored == 0
    assert stats.conversations == 2
    assert sorted(_conversations(conn)) == ["2025-07-15 14:32:01", "2025-07-15 15:10:00"]

    # One per-file marker with a NULL pointer, so replacing any single session
    # cannot cascade the file's fast-path record away.
    row = _file_row(conn)
    assert row["conversation_id"] is None and row["error"] is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_appending_to_the_live_session_lands(tmp_path, project):
    """Growth in the last session must be ingested, not silently dropped.

    Aider stamps a session's start and nothing else, so the live session
    reports no `ended_at` — and `newer than` is false forever against it. Left
    alone, moving aider to the session strategy would have frozen every
    session at its first ingest, trading one silent loss for another.
    """
    _append(project, _SESSION_TWO)
    conn = create_database(tmp_path / "siftd.db")
    adapter = _aider_adapter_at(project)
    ingest_all(conn, [adapter])

    live_id = _conversations(conn)["2025-07-15 15:10:00"][0]
    assert _exchange_count(conn, live_id) == 1

    _append(project, "\n#### and add logging\n\nAdded.\n")
    ingest_all(conn, [adapter])

    live_id = _conversations(conn)["2025-07-15 15:10:00"][0]
    assert _exchange_count(conn, live_id) == 2, "the append to the live session was lost"
    conn.close()


def test_an_append_touches_only_the_sessions_that_moved(tmp_path, project):
    """A finalized session settles after one replacement.

    Replacement is delete-then-insert: it mints a new id and drops the
    conversation's ownership rows. Deriving `ended_at` from the *next*
    session's header is what bounds that — a session with a successor reports
    a value that never moves again, so an append rewrites the live session
    alone rather than every session in the file.
    """
    _append(project, _SESSION_TWO)
    conn = create_database(tmp_path / "siftd.db")
    adapter = _aider_adapter_at(project)
    ingest_all(conn, [adapter])
    first = _conversations(conn)

    # Session 2 closes and session 3 opens: session 1 is untouched, session 2
    # finalizes (its ended_at moves from NULL to session 3's header) once.
    _append(project, _SESSION_THREE)
    ingest_all(conn, [adapter])
    second = _conversations(conn)

    assert second["2025-07-15 14:32:01"] == first["2025-07-15 14:32:01"]
    assert second["2025-07-15 15:10:00"][0] != first["2025-07-15 15:10:00"][0]
    assert second["2025-07-15 15:10:00"][1] is not None
    assert second["2025-07-15 16:00:00"][1] is None  # the live session

    # A further append to session 3 leaves both finalized sessions alone.
    _append(project, "\n#### one more\n\nok\n")
    ingest_all(conn, [adapter])
    third = _conversations(conn)

    assert third["2025-07-15 14:32:01"] == second["2025-07-15 14:32:01"]
    assert third["2025-07-15 15:10:00"] == second["2025-07-15 15:10:00"]
    assert third["2025-07-15 16:00:00"][0] != second["2025-07-15 16:00:00"][0]
    conn.close()


def test_two_sessions_opened_in_the_same_second_both_survive(tmp_path, project):
    """Aider's header resolves to the second, so its key can collide.

    Both sessions are real, and before the ordinal the second silently
    replaced the first — one conversation stored while the run reported two,
    which is data loss that even the stats disagreed with.
    """
    _history(project).write_text(
        "# aider chat started at 2025-01-01 00:00:00\n\n#### first\n\nx\n"
        "# aider chat started at 2025-01-01 00:00:00\n\n#### second\n\ny\n"
    )
    conn = create_database(tmp_path / "siftd.db")

    stats = ingest_all(conn, [_aider_adapter_at(project)])

    stored = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
    assert stored == 2, "one of two same-second sessions was dropped"
    assert stats.conversations == stored, "stats counted a conversation that was not stored"
    conn.close()


def test_distinct_timestamps_keep_the_ids_they_already_have(tmp_path, project):
    """The ordinal must not re-key sessions that never collided.

    `external_id` is the dedup key across machines and across upgrades, so
    widening it unconditionally would duplicate every aider conversation
    already ingested. Only the second and later occurrence of a repeated
    timestamp carries the suffix.
    """
    _append(project, _SESSION_TWO)
    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_aider_adapter_at(project)])

    ids = [r[0] for r in conn.execute("SELECT external_id FROM conversations")]
    assert all("#" not in external_id for external_id in ids), ids
    conn.close()


def test_a_row_poisoned_by_the_bug_heals_on_upgrade(tmp_path, project):
    """An already-broken file must recover without changing again.

    A user upgrading into the fix has an errored `ingested_files` row whose
    hash and mtime are the failing file's own — so both fast-path skips match
    forever, and the fix would never reach the file. Only re-examining errored
    rows makes the recovery reachable.

    The row here carries a NULL pointer: the file failed on its very first
    ingest, which is what happens to any aider project already past its first
    session when siftd first sees it.
    """
    _append(project, _SESSION_TWO)
    conn = create_database(tmp_path / "siftd.db")

    # Pre-fix behaviour: the file-strategy branch fails the whole source.
    stats = ingest_all(conn, [_aider_adapter_at(project, strategy="file")])
    assert stats.files_errored == 1
    assert _file_row(conn)["error"] is not None
    assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0

    # Upgrade. The file is byte-identical to the one that failed.
    stats = ingest_all(conn, [_aider_adapter_at(project)])

    assert stats.files_errored == 0
    assert stats.conversations == 2
    assert _file_row(conn)["error"] is None
    conn.close()


def test_a_poisoned_row_still_pointing_at_a_conversation_heals(tmp_path, project):
    """The other upgrade shape: the row has a live pointer when it heals.

    A file that ingested cleanly under the file strategy and only *later* grew
    a second session keeps its `conversation_id` through the failure —
    `_record_file_error` deliberately does not NULL a pointer that still
    resolves (#29). Healing then replaces that conversation, and the pointer
    is `ON DELETE CASCADE`, so the delete takes the file's own bookkeeping row
    with it and `record_session_file` has to put it back (#20's shape).
    """
    conn = create_database(tmp_path / "siftd.db")
    file_strategy = _aider_adapter_at(project, strategy="file")

    # One session, ingested cleanly the way it shipped: row -> conversation.
    ingest_all(conn, [file_strategy])
    assert _file_row(conn)["conversation_id"] is not None

    # aider opens a second session; the file now fails, pointer retained.
    _append(project, _SESSION_TWO)
    stats = ingest_all(conn, [file_strategy])
    assert stats.files_errored == 1
    row = _file_row(conn)
    assert row["error"] is not None and row["conversation_id"] is not None

    # Upgrade: the tracked conversation is replaced and the marker restored.
    stats = ingest_all(conn, [_aider_adapter_at(project)])

    assert stats.files_errored == 0
    assert sorted(_conversations(conn)) == ["2025-07-15 14:32:01", "2025-07-15 15:10:00"]
    row = _file_row(conn)
    assert row is not None, "the marker was cascade-deleted and never restored"
    assert row["conversation_id"] is None and row["error"] is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
