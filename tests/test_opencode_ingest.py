"""OpenCode change detection through real ingest: what a re-run must notice.

A container source (DEDUP_STRATEGY "session") is re-examined by its change
signal. Two things changed the picture for OpenCode: a commit can sit in the
database's write-ahead log without touching the database file, and a message
can be appended without the session row's update time moving. Each test pins one
of those with a real database, a real ingest, and a second run.
"""

from __future__ import annotations

import json
import sqlite3

from siftd.adapters import opencode
from siftd.adapters.contract import adapt
from siftd.adapters.records import CONTENT_REVISION_ATTRIBUTE, CONTENT_SOURCE_ATTRIBUTE
from siftd.domain.source import Source
from siftd.ingestion import ingest_all
from siftd.safecall import epoch_ms_to_iso
from siftd.storage.sqlite import create_database

_TS = 1710079200000
_SCHEMA = [
    "CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT, version TEXT, time_created INTEGER, time_updated INTEGER)",
    "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)",
    "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)",
]


def _create(conn):
    for ddl in _SCHEMA:
        conn.execute(ddl)


def _add_session(conn, sid, *, created=_TS, updated=_TS + 60000):
    conn.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)", (sid, "proj", "/ws", "T", "1", created, updated))
    _add_text(conn, sid, f"{sid}-u", "user", created + 1000, "question")


def _add_text(conn, sid, mid, role, at, text, *, updated=None):
    """One message with one text part, the message's own update time stated."""
    updated = at if updated is None else updated
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, at, updated, json.dumps({"role": role})))
    conn.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        (f"{mid}-p", mid, sid, at, updated, json.dumps({"type": "text", "text": text})),
    )


def _adapter_over(*db_paths):
    """An adapter whose discovery yields each database, in the order given."""

    class _Adapter:
        NAME = opencode.NAME
        DEDUP_STRATEGY = opencode.DEDUP_STRATEGY
        HARNESS_SOURCE = opencode.HARNESS_SOURCE
        can_handle = staticmethod(opencode.can_handle)
        parse = staticmethod(adapt(opencode).parse)
        change_paths = staticmethod(opencode.change_paths)

        @staticmethod
        def discover():
            for db_path in db_paths:
                yield Source(kind="sqlite", location=db_path)

    return _Adapter


def _adapter_at(db_path):
    return _adapter_over(db_path)


def _events(conn, external_id):
    return dict(conn.execute(
        "SELECT e.kind, COUNT(*) FROM events e JOIN conversations c ON c.id = e.conversation_id "
        "WHERE c.external_id = ? GROUP BY e.kind",
        (external_id,),
    ).fetchall())


def test_a_commit_still_in_the_write_ahead_log_is_seen_by_the_next_ingest(tmp_path):
    db = tmp_path / "opencode.db"
    writer = sqlite3.connect(str(db))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    _create(writer)
    _add_session(writer, "ses_a")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # ses_a is in the main file

    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_at(db)])
    assert [r[0] for r in conn.execute("SELECT external_id FROM conversations")] == ["opencode::ses_a"]

    # ses_b is committed to the log only: the main database file does not change.
    _add_session(writer, "ses_b")
    writer.commit()
    assert (tmp_path / "opencode.db-wal").stat().st_size > 0

    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.files_errored == 0
    assert sorted(r[0] for r in conn.execute("SELECT external_id FROM conversations")) == [
        "opencode::ses_a",
        "opencode::ses_b",
    ]
    writer.close()
    conn.close()


def test_a_message_appended_without_the_session_row_moving_replaces_the_session(tmp_path):
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a", created=_TS, updated=_TS + 60000)
    source.commit()

    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_at(db)])
    assert _events(conn, "opencode::ses_a") == {"prompt": 1}

    # The reply lands after the session row's last update, which does not move.
    _add_text(source, "ses_a", "ses_a-a", "assistant", _TS + 120000, "the reply")
    source.commit()

    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.files_errored == 0
    assert stats.by_harness["opencode"]["replaced"] == 1
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
    source.close()
    conn.close()


def test_an_unchanged_database_is_skipped_on_the_next_ingest(tmp_path):
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a")
    source.commit()
    source.close()

    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_at(db)])
    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.files_skipped == 1 and stats.files_ingested == 0 and stats.files_replaced == 0
    conn.close()


def test_a_reply_timestamped_before_the_session_end_replaces_the_session(tmp_path):
    """The session row's end does not move for it, so only the content revision can see it."""
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a", created=_TS, updated=_TS + 60000)
    _add_session(source, "ses_b")
    source.commit()

    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_at(db)])

    _add_text(source, "ses_a", "ses_a-a", "assistant", _TS + 30000, "the reply")
    source.commit()

    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.files_errored == 0
    assert stats.by_harness["opencode"]["replaced"] == 1
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
    source.close()
    conn.close()


def test_an_edit_that_moves_no_time_replaces_only_the_edited_session(tmp_path):
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a")
    _add_session(source, "ses_b")
    source.commit()

    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_at(db)])

    source.execute("UPDATE part SET data = ? WHERE id = 'ses_a-u-p'", (json.dumps({"type": "text", "text": "edited"}),))
    source.commit()

    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.files_errored == 0
    assert stats.by_harness["opencode"]["replaced"] == 1
    source.close()
    conn.close()


def test_a_session_stored_before_the_revision_existed_is_replaced_when_its_end_moves(tmp_path):
    """A stored copy with no revision has no source to match, so only its end time decides; the next real change stamps it."""
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a")
    source.commit()

    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_at(db)])
    conn.execute("DELETE FROM attributes WHERE key IN (?, ?)", (CONTENT_REVISION_ATTRIBUTE, CONTENT_SOURCE_ATTRIBUTE))
    conn.commit()

    source.execute("UPDATE session SET time_updated = ? WHERE id = 'ses_a'", (_TS + 120000,))
    _add_session(source, "ses_b")  # changes the file, so the session is examined
    source.commit()

    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.by_harness["opencode"]["replaced"] == 1
    assert conn.execute("SELECT COUNT(*) FROM attributes WHERE key = ?", (CONTENT_REVISION_ATTRIBUTE,)).fetchone()[0] == 2
    source.close()
    conn.close()


def test_an_older_backup_cannot_replace_a_stored_copy_that_predates_the_revision(tmp_path):
    """The stored copy has no revision to match, so the older backup must lose on end time, as the newer copy would."""
    live, backup = _two_copies_of_one_session(tmp_path, reply_moves_session_row=True)
    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_over(live)])
    conn.execute("DELETE FROM attributes WHERE key IN (?, ?)", (CONTENT_REVISION_ATTRIBUTE, CONTENT_SOURCE_ATTRIBUTE))
    conn.commit()

    stats = ingest_all(conn, [_adapter_over(live, backup)])
    assert stats.files_errored == 0
    assert stats.by_harness["opencode"]["replaced"] == 0
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
    conn.close()


def test_a_token_count_beyond_the_sqlite_integer_range_does_not_abort_the_ingest(tmp_path):
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a")
    _add_session(source, "ses_b")
    source.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("ses_b-a", "ses_b", _TS + 1000, _TS + 1000, json.dumps({"role": "assistant", "tokens": {"input": 2**80}})),
    )
    source.commit()
    source.close()

    conn = create_database(tmp_path / "siftd.db")
    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.files_errored == 0
    assert sorted(r[0] for r in conn.execute("SELECT external_id FROM conversations")) == [
        "opencode::ses_a",
        "opencode::ses_b",
    ]
    conn.close()


def _two_copies_of_one_session(tmp_path, *, reply_moves_session_row):
    """A live database and an older backup copy of the same session.

    The backup predates the reply. The live copy holds the reply, stamped before the
    session row's update time, so only the revision can tell the two apart. With
    ``reply_moves_session_row`` the reply also moves the session row, as a live
    write does, so the live copy's end is strictly later.
    """
    backup = tmp_path / "backup" / "opencode.db"
    live = tmp_path / "live" / "opencode.db"
    backup.parent.mkdir()
    live.parent.mkdir()

    copy = sqlite3.connect(str(backup))
    _create(copy)
    _add_session(copy, "ses_a", created=_TS, updated=_TS + 60000)
    copy.commit()
    copy.close()

    copy = sqlite3.connect(str(live))
    _create(copy)
    _add_session(copy, "ses_a", created=_TS, updated=_TS + (120000 if reply_moves_session_row else 60000))
    _add_text(copy, "ses_a", "ses_a-a", "assistant", _TS + 30000 if not reply_moves_session_row else _TS + 120000, "the reply")
    copy.commit()
    copy.close()
    return live, backup


def test_an_older_backup_copy_does_not_replace_the_newer_copy_stored_first(tmp_path):
    """Two databases share a session; the older copy is discovered second and must not overwrite the reply."""
    live, backup = _two_copies_of_one_session(tmp_path, reply_moves_session_row=False)
    conn = create_database(tmp_path / "siftd.db")

    stats = ingest_all(conn, [_adapter_over(live, backup)])
    assert stats.files_errored == 0
    assert stats.by_harness["opencode"]["replaced"] == 0
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}

    # Nothing moved on the next run, and the reply stays: the loss is not deferred.
    stats = ingest_all(conn, [_adapter_over(live, backup)])
    assert stats.files_skipped == 2
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
    conn.close()


def test_reparse_does_not_let_a_backup_with_an_equal_end_replace_the_live_copy(tmp_path):
    """--reparse refreshes a copy only from the source it was read from.

    Both copies end at the same instant, and only the live one holds the reply. An
    equal end time used to replace under --reparse, so the backup, processed last,
    deleted the reply. Run in both discovery orders: the order must not decide it.
    """
    live, backup = _two_copies_of_one_session(tmp_path, reply_moves_session_row=False)
    for order in ((live, backup), (backup, live)):
        conn = create_database(tmp_path / f"siftd-{order[0].parent.name}.db")
        ingest_all(conn, [_adapter_over(live)])
        assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}

        stats = ingest_all(conn, [_adapter_over(*order)], reparse=True)
        assert stats.files_errored == 0
        assert stats.by_harness["opencode"]["replaced"] == 1  # the live source refreshes itself
        assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
        conn.close()


def test_a_newer_copy_replaces_an_older_backup_stored_first(tmp_path):
    """Latest-wins across sources still holds: the newer copy replaces the older one stored before it."""
    live, backup = _two_copies_of_one_session(tmp_path, reply_moves_session_row=True)
    conn = create_database(tmp_path / "siftd.db")

    ingest_all(conn, [_adapter_over(backup, live)])
    stats = ingest_all(conn, [_adapter_over(backup, live)])
    assert stats.files_errored == 0
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
    conn.close()

    conn = create_database(tmp_path / "siftd-2.db")
    stats = ingest_all(conn, [_adapter_over(backup, live)])
    assert stats.by_harness["opencode"]["replaced"] == 1
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
    conn.close()


def test_a_later_update_to_a_row_replaces_the_session_and_keeps_its_end(tmp_path):
    """A row updated after the session's last update replaces the stored copy, and the end does not move."""
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a", created=_TS, updated=_TS + 60000)
    source.commit()

    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [_adapter_at(db)])

    source.execute(
        "UPDATE part SET data = ?, time_updated = ? WHERE id = 'ses_a-u-p'",
        (json.dumps({"type": "text", "text": "edited later"}), _TS + 3600000),
    )
    source.commit()

    stats = ingest_all(conn, [_adapter_at(db)])
    assert stats.by_harness["opencode"]["replaced"] == 1
    (ended,) = conn.execute("SELECT ended_at FROM conversations WHERE external_id = 'opencode::ses_a'").fetchone()
    assert ended == epoch_ms_to_iso(_TS + 60000)
    source.close()
    conn.close()


def test_a_commit_that_lands_after_the_first_parse_is_seen_by_the_next_ingest(tmp_path):
    """The change marker describes what was read: a commit made while the first parse ran is re-examined, not hidden."""
    db = tmp_path / "opencode.db"
    source = sqlite3.connect(str(db))
    _create(source)
    _add_session(source, "ses_a")
    source.commit()
    source.close()

    writer = sqlite3.connect(str(db))
    parse = adapt(opencode).parse
    written = []

    def parse_then_commit(src):
        conversations = list(parse(src))
        if not written:
            _add_text(writer, "ses_a", "ses_a-a", "assistant", _TS + 5000, "the reply")
            writer.commit()
            written.append(True)
        return conversations

    first = _adapter_over(db)
    first.parse = staticmethod(parse_then_commit)
    conn = create_database(tmp_path / "siftd.db")
    ingest_all(conn, [first])
    assert _events(conn, "opencode::ses_a") == {"prompt": 1}

    stats = ingest_all(conn, [_adapter_over(db)])
    assert stats.files_errored == 0
    assert _events(conn, "opencode::ses_a") == {"prompt": 1, "response": 1}
    writer.close()
    conn.close()
