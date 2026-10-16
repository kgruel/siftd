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

import hashlib
from pathlib import Path

import pytest
from conftest import tag_names

from siftd.adapters import aider
from siftd.ingestion import ingest_all
from siftd.domain.source import Source
from siftd.storage.sessions import get_pending_tags, queue_tag
from siftd.storage.sqlite import compute_file_hash, create_database, harness_id_for_conversation, record_session_file, store_conversation
from siftd.storage.tags import apply_tag, get_or_create_tag

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


def _digest(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def test_a_single_session_file_that_grows_is_re_stored_on_reingest(tmp_path, project):
    """The live session of a one-session file has no successor, so it must still be re-checked as it grows."""
    conn = create_database(tmp_path / "siftd.db")
    adapter = _aider_adapter_at(project)
    ingest_all(conn, [adapter])
    (live,) = _conversations(conn).values()
    assert live[1] is None and _exchange_count(conn, live[0]) == 1

    _append(project, "\n#### and add logging\n\nAdded.\n")
    ingest_all(conn, [adapter])

    (grown,) = _conversations(conn).values()
    assert _exchange_count(conn, grown[0]) == 2, "the append to the only session was lost"
    conn.close()


def test_a_row_the_previous_release_stored_under_a_relative_id_is_adopted(tmp_path, project, monkeypatch):
    """The previous release keyed a session on its path as spelled. Scanning the same file absolutely must adopt
    that row, tags and all, rather than store a second copy beside it.
    """
    monkeypatch.chdir(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy = f"aider::{_digest('proj/.aider.chat.history.md')}::2025-07-15 14:32:01"
    # What the previous release wrote for this session: the same conversation under the spelled-relative id.
    (conversation,) = aider.parse(Source(kind="file", location=Path("proj/.aider.chat.history.md")))
    conversation.external_id = legacy
    conversation.superseded_external_ids = ()
    legacy_id = store_conversation(conn, conversation, filter_binary=False)
    name = "decision:aider-identity"
    apply_tag(conn, "conversation", legacy_id, get_or_create_tag(conn, name), commit=True)

    stats = ingest_all(conn, [_aider_adapter_at(project)])

    assert stats.files_errored == 0
    rows = conn.execute("SELECT id, external_id FROM conversations").fetchall()
    assert len(rows) == 1, "the absolute scan stored a second copy of the same session"
    assert rows[0]["external_id"] == f"aider::{_digest(str(project.resolve() / '.aider.chat.history.md'))}::2025-07-15 14:32:01"
    assert name in tag_names(conn, "conversation", rows[0]["id"])
    conn.close()


def _store_legacy_row(conn, legacy_external_id, conversation, tag):
    """The row the previous release wrote: the same session under the id it keyed on."""
    conversation.external_id = legacy_external_id
    conversation.superseded_external_ids = ()
    legacy_id = store_conversation(conn, conversation, filter_binary=False)
    apply_tag(conn, "conversation", legacy_id, get_or_create_tag(conn, tag), commit=True)
    return legacy_id


def test_a_same_second_session_of_another_project_is_never_adopted(tmp_path, monkeypatch):
    """Adoption must not take a row that belongs to a different file.

    Project A was scanned as `--path .` by the previous release, so its row is
    keyed on the basename-only digest. Project B has a session opened in the same
    second, and it is scanned from its own directory, where that same basename
    spelling is one of its candidates. Adoption must verify the stored messages
    against B's own content, not trust a shared start second: the session header
    is the only timestamp the sessions have in common.
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / ".aider.chat.history.md").write_text(_SESSION_ONE)
    (b / ".aider.chat.history.md").write_text(_SESSION_ONE.replace("write a hello world script", "refactor the parser"))
    conn = create_database(tmp_path / "siftd.db")

    monkeypatch.chdir(a)
    (a_conversation,) = aider.parse(Source(kind="file", location=Path(".aider.chat.history.md")))
    a_id = _store_legacy_row(
        conn,
        f"aider::{_digest('.aider.chat.history.md')}::2025-07-15 14:32:01",
        a_conversation,
        "decision:project-a",
    )

    monkeypatch.chdir(b)
    stats = ingest_all(conn, [_aider_adapter_at(b)])

    assert stats.files_errored == 0
    survivor = conn.execute("SELECT id FROM conversations WHERE id = ?", (a_id,)).fetchone()
    assert survivor is not None, "project B's ingest deleted project A's conversation"
    assert "decision:project-a" in tag_names(conn, "conversation", a_id)
    assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 2
    conn.close()


def test_a_legacy_row_is_adopted_after_the_working_directory_changes(tmp_path, monkeypatch):
    """The previous release keyed a file on the spelling its scan root gave, relative to the cwd then.

    Scanning the same file from a different directory now must still adopt that
    row, tags and all. The legacy spelling of a file is its path relative to some
    directory that contains it, so every such spelling is a candidate, and the
    stored messages decide which one (if any) was really this file.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".aider.chat.history.md").write_text(_SESSION_ONE)
    conn = create_database(tmp_path / "siftd.db")

    monkeypatch.chdir(proj)
    (legacy_conversation,) = aider.parse(Source(kind="file", location=Path(".aider.chat.history.md")))
    legacy_id = _store_legacy_row(
        conn,
        f"aider::{_digest('.aider.chat.history.md')}::2025-07-15 14:32:01",
        legacy_conversation,
        "decision:moved-cwd",
    )

    monkeypatch.chdir(tmp_path)
    stats = ingest_all(conn, [_aider_adapter_at(proj)])

    assert stats.files_errored == 0
    rows = conn.execute("SELECT id, external_id FROM conversations").fetchall()
    assert len(rows) == 1, "the absolute scan stored a second copy"
    assert rows[0]["external_id"] == f"aider::{_digest(str(proj.resolve() / '.aider.chat.history.md'))}::2025-07-15 14:32:01"
    assert "decision:moved-cwd" in tag_names(conn, "conversation", rows[0]["id"])
    conn.close()


def test_a_queued_tag_under_another_files_legacy_key_stays_with_that_file(tmp_path, monkeypatch):
    """Review round 2, finding 2: only the verified legacy key's queue moves.

    Project A's legacy row is keyed on the basename digest, and B's on the
    `b/`-relative digest. Both are candidates for B's session. A's row is refused
    (its prompt is not B's) and B's is adopted. A tag queued under A's key belongs
    to A, so it must stay under A's key rather than move to B's new id.
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / ".aider.chat.history.md").write_text(_SESSION_ONE)
    (b / ".aider.chat.history.md").write_text(_SESSION_ONE.replace("write a hello world script", "refactor the parser"))
    conn = create_database(tmp_path / "siftd.db")

    monkeypatch.chdir(a)
    (a_conversation,) = aider.parse(Source(kind="file", location=Path(".aider.chat.history.md")))
    a_key = f"aider::{_digest('.aider.chat.history.md')}::2025-07-15 14:32:01"
    a_id = _store_legacy_row(conn, a_key, a_conversation, "decision:project-a")
    queue_tag(conn, a_key, "decision:project-a", commit=True)

    monkeypatch.chdir(tmp_path)
    (b_conversation,) = aider.parse(Source(kind="file", location=Path("b/.aider.chat.history.md")))
    _store_legacy_row(conn, f"aider::{_digest('b/.aider.chat.history.md')}::2025-07-15 14:32:01", b_conversation, "decision:project-b")

    stats = ingest_all(conn, [_aider_adapter_at(b)])

    assert stats.files_errored == 0
    assert conn.execute("SELECT id FROM conversations WHERE id = ?", (a_id,)).fetchone() is not None
    b_key = f"aider::{_digest(str(b.resolve() / '.aider.chat.history.md'))}::2025-07-15 14:32:01"
    assert [t.tag_name for t in get_pending_tags(conn, b_key)] == [], "A's queued tag moved to project B"
    assert [t.tag_name for t in get_pending_tags(conn, a_key)] == ["decision:project-a"]
    conn.close()


def test_a_parent_relative_legacy_row_is_adopted_from_any_directory(tmp_path, monkeypatch):
    """Review round 2, finding 3: `--path ../proj` is a spelling no ancestor enumeration yields.

    The previous release scanned `../proj` from `work/`, a directory that is a
    sibling of the project, not an ancestor. Its id is the digest of that
    spelling, and the file was recorded in the bookkeeping under that spelling.
    Scanning absolutely from elsewhere must adopt the row, tag and all.
    """
    proj = tmp_path / "proj"
    work = tmp_path / "work"
    proj.mkdir()
    work.mkdir()
    (proj / ".aider.chat.history.md").write_text(_SESSION_ONE)
    conn = create_database(tmp_path / "siftd.db")

    spelled = "../proj/.aider.chat.history.md"
    monkeypatch.chdir(work)
    (legacy_conversation,) = aider.parse(Source(kind="file", location=Path(spelled)))
    legacy_id = _store_legacy_row(
        conn,
        f"aider::{_digest(spelled)}::2025-07-15 14:32:01",
        legacy_conversation,
        "decision:parent-relative",
    )
    record_session_file(
        conn, spelled, compute_file_hash(proj / ".aider.chat.history.md"),
        harness_id_for_conversation(conn, legacy_conversation),
        commit=True,
    )

    monkeypatch.chdir(tmp_path)
    stats = ingest_all(conn, [_aider_adapter_at(proj)])

    assert stats.files_errored == 0
    rows = conn.execute("SELECT id, external_id FROM conversations").fetchall()
    assert len(rows) == 1, "the absolute scan stored a second copy beside the parent-relative row"
    assert rows[0]["id"] != legacy_id
    assert "decision:parent-relative" in tag_names(conn, "conversation", rows[0]["id"])
    conn.close()
