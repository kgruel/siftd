"""Superseded-key adoption on the ``"file"`` dedup branch.

Born as the gemini_cli chat-file identity fix (ar/gemini-key, #93): Gemini CLI
reused one sessionId across several chat files, each a distinct conversation,
and the adapter's sessionId key kept only one of them. The re-keyed adapter
named the old key as superseded. siftd no longer ships gemini_cli (0.13.0), but
it remains the one file-strategy adapter that re-keys, so its parser lives on as
``fakes.gemini_chat`` and these keep covering the generic adoption path.

These drive that parser through the real ingest_all against a temp database.
They pin these properties:

- two chat files sharing a sessionId are two conversations, with all prompts;
- a database holding the superseded sessionId-keyed row (what the previous
  release wrote) does not grow a second conversation for the same chat file,
  whether the file is unchanged, changed, or re-parsed after its marker is
  cleared, and the tags on that row carry to the replacement;
- the superseded key never claims a sibling file's conversation, which shares
  the key and differs only by startTime;
- the same holds for a chat whose messages carry no ids: they are matched by
  kind and timestamp instead, so adoption does not depend on ids alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from conftest import tag_names

from fakes import gemini_chat
from siftd.domain.source import Source
from siftd.ingestion import ingest_all
from siftd.storage.sessions import get_pending_tags, queue_tag, recover_pending_tags
from siftd.storage.sqlite import (
    compute_file_hash,
    create_database,
    harness_id_for_conversation,
    record_ingested_file,
    record_session_file,
    store_conversation,
)
from siftd.storage.tags import apply_tag, get_or_create_tag

SESSION = "2c1f6a0e-0000-4000-8000-000000000001"
START_A = "2025-12-28T16:42:00.000Z"
START_B = "2025-12-28T16:46:00.000Z"


def _stamp(start, offset):
    """A message timestamp ``offset`` seconds after ``start``: distinct per message."""
    base = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return (base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_chat(project_dir, stem, *, session_id, start, last_updated, prompts, idless=False):
    """A real-shaped Gemini chat file: user/gemini pairs, one prompt per text.

    ``idless`` drops the message ids, which the parser accepts, and stamps each
    message distinctly so the timestamp is what tells the messages apart.
    """
    chats = project_dir / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    messages = []
    for i, text in enumerate(prompts):
        user = {
            "id": f"{stem}-u{i}",
            "timestamp": start,
            "type": "user",
            "content": text,
        }
        gemini = {
            "id": f"{stem}-g{i}",
            "timestamp": start,
            "type": "gemini",
            "content": f"reply {i}",
            "model": "gemini-3-flash-preview",
            "tokens": {"input": 10, "output": 5},
        }
        if idless:
            user.pop("id")
            gemini.pop("id")
            user["timestamp"] = _stamp(start, 2 * i)
            gemini["timestamp"] = _stamp(start, 2 * i + 1)
        messages += [user, gemini]
    doc = {
        "sessionId": session_id,
        "projectHash": "0" * 64,
        "startTime": start,
        "lastUpdated": last_updated,
        "messages": messages,
    }
    path = chats / f"{stem}.json"
    path.write_text(json.dumps(doc))
    return path


def _gemini_adapter_at(root, order=None):
    """The gemini_chat parser, discovering only under ``root``.

    ``order`` pins the sources' discovery order, for tests whose result depends on
    which file is ingested first.
    """

    class _Adapter:
        ADAPTER_INTERFACE_VERSION = 1
        NAME = gemini_chat.NAME
        DEFAULT_LOCATIONS: list[str] = []
        DEDUP_STRATEGY = gemini_chat.DEDUP_STRATEGY
        HARNESS_SOURCE = gemini_chat.HARNESS_SOURCE
        HARNESS_LOG_FORMAT = gemini_chat.HARNESS_LOG_FORMAT
        HARNESS_DISPLAY_NAME = gemini_chat.HARNESS_DISPLAY_NAME
        TOOL_ALIASES = gemini_chat.TOOL_ALIASES
        can_handle = staticmethod(gemini_chat.can_handle)
        parse = staticmethod(gemini_chat.parse)

        @staticmethod
        def discover(locations=None):
            if order is None:
                yield from gemini_chat.discover([str(root)])
            else:
                for path in order:
                    yield Source(kind="file", location=path)

    return _Adapter


def _prompt_texts(conn, conversation_id):
    """Prompt text of one conversation, sorted (message timestamps tie)."""
    rows = conn.execute(
        "SELECT ec.content FROM events e "
        "JOIN event_content ec ON ec.event_id = e.id "
        "WHERE e.conversation_id = ? AND e.kind = 'prompt' AND ec.block_type = 'text'",
        (conversation_id,),
    ).fetchall()
    return sorted(json.loads(r[0])["text"] for r in rows)


def _conversations_by_start(conn):
    return {
        row["started_at"]: row
        for row in conn.execute("SELECT id, external_id, started_at FROM conversations")
    }


def _tag(conn, conversation_id, name="decision:gemini-identity"):
    apply_tag(conn, "conversation", conversation_id, get_or_create_tag(conn, name), commit=True)
    return name


def _store_superseded_row(conn, path, superseded_external_id):
    """Write exactly what the previous release stored for one chat file.

    The conversation is keyed on the sessionId, and the file gets a marker with
    conversation_id NULL — the session-strategy bookkeeping that never pointed
    at a conversation.
    """
    conversation = next(gemini_chat.parse(Source(kind="file", location=path)))
    conversation.external_id = superseded_external_id
    conversation.superseded_external_ids = ()
    conv_id = store_conversation(conn, conversation, filter_binary=False)
    harness_id = harness_id_for_conversation(conn, conversation)
    st = path.stat()
    record_session_file(
        conn, str(path), compute_file_hash(path), harness_id,
        file_mtime=st.st_mtime, file_size=st.st_size, commit=True,
    )
    return conv_id


def _ingest(conn, root, order=None):
    return ingest_all(conn, [_gemini_adapter_at(root, order)], filter_binary=False)


def _legacy_pair(tmp_path):
    """Two chat files sharing a sessionId: A is the one the old key kept."""
    project = tmp_path / "proj"
    legacy_key = f"{gemini_chat.NAME}::{SESSION}"
    a = _write_chat(
        project, "session-2025-12-28T16-42-2c1f6a0e",
        session_id=SESSION, start=START_A, last_updated="2025-12-28T17:00:00.000Z",
        prompts=["alpha one", "alpha two"],
    )
    b = _write_chat(
        project, "session-2025-12-28T16-46-2c1f6a0e",
        session_id=SESSION, start=START_B, last_updated="2025-12-28T16:50:00.000Z",
        prompts=["bravo only"],
    )
    return project, a, b, legacy_key


def test_chat_files_sharing_a_session_id_are_each_a_conversation(tmp_path):
    """The defect: keyed on sessionId, one of these two files vanishes."""
    project, _a, _b, _key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")

    stats = _ingest(conn, tmp_path)

    assert stats.files_errored == 0
    by_start = _conversations_by_start(conn)
    assert set(by_start) == {START_A, START_B}
    assert _prompt_texts(conn, by_start[START_A]["id"]) == ["alpha one", "alpha two"]
    assert _prompt_texts(conn, by_start[START_B]["id"]) == ["bravo only"]


def test_unchanged_legacy_row_is_not_duplicated(tmp_path):
    """Upgrade with the file untouched: the legacy row stays one conversation."""
    project, a, _b, key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)

    _ingest(conn, tmp_path)
    _ingest(conn, tmp_path)  # the next ingest is a no-op, not a second copy

    rows = conn.execute("SELECT id, started_at FROM conversations ORDER BY started_at").fetchall()
    assert len(rows) == 2
    assert [r["id"] for r in rows if r["started_at"] == START_A] == [legacy_id]


def test_changed_legacy_file_is_adopted_and_keeps_its_tags(tmp_path):
    """The next change to a legacy file replaces the legacy row, not adds to it."""
    project, a, _b, key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    tag = _tag(conn, legacy_id)

    # The file grows after the upgrade, so its hash changes.
    doc = json.loads(a.read_text())
    doc["messages"] += [
        {"id": "late-u", "timestamp": START_A, "type": "user", "content": "alpha three"},
        {"id": "late-g", "timestamp": START_A, "type": "gemini", "content": "ok",
         "model": "gemini-3-flash-preview", "tokens": {"input": 1, "output": 1}},
    ]
    a.write_text(json.dumps(doc))

    _ingest(conn, tmp_path)

    by_start = _conversations_by_start(conn)
    assert len(conn.execute("SELECT id FROM conversations").fetchall()) == 2
    adopted = by_start[START_A]
    assert adopted["id"] != legacy_id  # replaced: delete-then-insert, as for any change
    assert _prompt_texts(conn, adopted["id"]) == ["alpha one", "alpha three", "alpha two"]
    assert tag in tag_names(conn, "conversation", adopted["id"])


def test_forced_reparse_of_a_legacy_file_adopts_its_row(tmp_path):
    """Recovery shape: the marker is cleared and the file re-parsed unchanged.

    No existing ingest path does this without touching the database, so the
    recovery in the release notes is exactly this: drop the marker, ingest.
    """
    project, a, _b, key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (str(a),))
    conn.commit()

    _ingest(conn, tmp_path)

    rows = conn.execute("SELECT id, started_at FROM conversations").fetchall()
    assert len(rows) == 2
    adopted = _conversations_by_start(conn)[START_A]
    assert adopted["id"] != legacy_id
    assert _prompt_texts(conn, adopted["id"]) == ["alpha one", "alpha two"]
    assert tag in tag_names(conn, "conversation", adopted["id"])


def test_superseded_key_never_claims_a_sibling(tmp_path):
    """B shares A's sessionId, so the legacy key names A's row. B must not take it.

    B is ingested first on purpose: it is the file that would claim the shared
    key. A tag on the legacy row is the witness. Taking the row would carry the
    tag onto B, so the tag must end on A's conversation and never on B's.
    """
    project, a, b, key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (str(a),))
    conn.commit()

    _ingest(conn, tmp_path, order=[b, a])

    by_start = _conversations_by_start(conn)
    assert len(by_start) == 2
    assert _prompt_texts(conn, by_start[START_A]["id"]) == ["alpha one", "alpha two"]
    assert _prompt_texts(conn, by_start[START_B]["id"]) == ["bravo only"]
    assert tag in tag_names(conn, "conversation", by_start[START_A]["id"])
    assert tag not in tag_names(conn, "conversation", by_start[START_B]["id"])


def _claim_by_other_path(conn, conversation_id, other_path="/elsewhere/backup/chats/copy.json"):
    """Another path's bookkeeping row names the conversation: a second copy of the transcript."""
    record_ingested_file(
        conn, other_path, "f" * 64, conversation_id, file_mtime=0.0, file_size=0, commit=True,
    )


def _without_start_time(path):
    doc = json.loads(path.read_text())
    del doc["startTime"]
    path.write_text(json.dumps(doc))


def _starts(conn, started_at):
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM conversations WHERE started_at = ?", (started_at,)
        )
    ]


def test_refused_adoption_on_a_new_file_does_not_store_a_second_copy(tmp_path):
    """Finding 1, new-file path: the legacy row is claimed elsewhere, so it is not adopted.

    The file must settle as a duplicate of that row. Storing it under its new
    external_id instead would leave two conversations for one chat file.
    """
    project, a, _b, key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    _claim_by_other_path(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (str(a),))
    conn.commit()

    _ingest(conn, tmp_path)

    assert _starts(conn, START_A) == [legacy_id]
    assert _prompt_texts(conn, legacy_id) == ["alpha one", "alpha two"]


def test_refused_adoption_on_a_changed_file_does_not_store_a_second_copy(tmp_path):
    """Finding 1, re-ingest path: the changed legacy file meets a claimed legacy row."""
    project, a, _b, key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    _claim_by_other_path(conn, legacy_id)
    doc = json.loads(a.read_text())
    doc["messages"] += [
        {"id": "late-u", "timestamp": START_A, "type": "user", "content": "alpha three"},
    ]
    a.write_text(json.dumps(doc))

    _ingest(conn, tmp_path)

    assert _starts(conn, START_A) == [legacy_id]
    assert len(conn.execute("SELECT id FROM conversations").fetchall()) == 2


def test_legacy_row_of_a_file_without_start_time_is_adopted(tmp_path):
    """Finding 2: a legacy row stamped at parse time must still be adopted.

    The previous release stored started_at = now_iso() for a file with no
    startTime, which no later parse can reproduce. The row is simulated with a
    fixed, different instant so the test never depends on the wall clock.
    """
    project, a, _b, key = _legacy_pair(tmp_path)
    _without_start_time(a)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    conn.execute(
        "UPDATE conversations SET started_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00.000Z", legacy_id),
    )
    tag = _tag(conn, legacy_id)
    doc = json.loads(a.read_text())
    doc["messages"] += [
        {"id": "late-u", "timestamp": START_A, "type": "user", "content": "alpha three"},
        {"id": "late-g", "timestamp": START_A, "type": "gemini", "content": "ok",
         "model": "gemini-3-flash-preview", "tokens": {"input": 1, "output": 1}},
    ]
    a.write_text(json.dumps(doc))

    _ingest(conn, tmp_path)

    assert len(conn.execute("SELECT id FROM conversations").fetchall()) == 2
    assert _starts(conn, "2026-01-01T00:00:00.000Z") == []
    adopted = conn.execute(
        "SELECT id FROM conversations WHERE external_id = ?",
        (f"{gemini_chat.NAME}::{a.stem}",),
    ).fetchone()["id"]
    assert adopted != legacy_id
    assert _prompt_texts(conn, adopted) == ["alpha one", "alpha three", "alpha two"]
    assert tag in tag_names(conn, "conversation", adopted)


def test_queued_tag_under_a_legacy_session_id_follows_its_chat_file(tmp_path):
    """Finding 3: a tag queued under the old sessionId lands on the file that legacy kept."""
    project, a, _b, key = _legacy_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    _store_superseded_row(conn, a, key)
    queue_tag(conn, SESSION, "decision:queued-before-upgrade", commit=True)
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (str(a),))
    conn.commit()

    _ingest(conn, tmp_path)
    recovery = recover_pending_tags(conn, commit=True)

    adopted = _conversations_by_start(conn)[START_A]["id"]
    assert [a.tag_name for a in recovery.applied] == ["decision:queued-before-upgrade"]
    assert recovery.unresolved == []
    assert "decision:queued-before-upgrade" in tag_names(conn, "conversation", adopted)
    assert get_pending_tags(conn, SESSION) == []


# --- Messages without ids ----------------------------------------------------
# The parser accepts a message with no id (Gemini chat JSON does not guarantee
# one), so adoption must not depend on ids alone. A message without one is
# matched by kind and timestamp. None of these files has an id.


def _idless_pair(tmp_path):
    """Two id-less chat files sharing a sessionId, as _legacy_pair but without ids."""
    project = tmp_path / "proj"
    legacy_key = f"{gemini_chat.NAME}::{SESSION}"
    a = _write_chat(
        project, "session-2025-12-28T16-42-2c1f6a0e",
        session_id=SESSION, start=START_A, last_updated="2025-12-28T17:00:00.000Z",
        prompts=["alpha one", "alpha two"], idless=True,
    )
    b = _write_chat(
        project, "session-2025-12-28T16-46-2c1f6a0e",
        session_id=SESSION, start=START_B, last_updated="2025-12-28T16:50:00.000Z",
        prompts=["bravo only"], idless=True,
    )
    return project, a, b, legacy_key


def _append_idless_prompt(path, text, offset):
    doc = json.loads(path.read_text())
    doc["messages"] += [
        {"timestamp": _stamp(START_A, offset), "type": "user", "content": text},
        {"timestamp": _stamp(START_A, offset + 1), "type": "gemini", "content": "ok",
         "model": "gemini-3-flash-preview", "tokens": {"input": 1, "output": 1}},
    ]
    path.write_text(json.dumps(doc))


def test_idless_legacy_row_is_adopted_not_duplicated_on_forced_reparse(tmp_path):
    """Finding 1 (major): the legacy row of an id-less file must be adopted.

    Store the legacy row, clear the marker, ingest. Before the fallback this
    stored a second conversation with the same prompts, and the tag stayed on
    the legacy row.
    """
    project, a, _b, key = _idless_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (str(a),))
    conn.commit()

    _ingest(conn, tmp_path)

    assert len(conn.execute("SELECT id FROM conversations").fetchall()) == 2
    adopted = _conversations_by_start(conn)[START_A]
    assert adopted["id"] != legacy_id
    assert _prompt_texts(conn, adopted["id"]) == ["alpha one", "alpha two"]
    assert tag in tag_names(conn, "conversation", adopted["id"])
    assert legacy_id not in {r["id"] for r in conn.execute("SELECT id FROM conversations")}


def test_idless_legacy_row_is_adopted_when_the_file_changes(tmp_path):
    """The changed-file path, with no ids: the legacy row is replaced, tags kept."""
    project, a, _b, key = _idless_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    tag = _tag(conn, legacy_id)
    _append_idless_prompt(a, "alpha three", offset=100)

    _ingest(conn, tmp_path)

    assert len(conn.execute("SELECT id FROM conversations").fetchall()) == 2
    adopted = _conversations_by_start(conn)[START_A]
    assert adopted["id"] != legacy_id
    assert _prompt_texts(conn, adopted["id"]) == ["alpha one", "alpha three", "alpha two"]
    assert tag in tag_names(conn, "conversation", adopted["id"])


def test_idless_sibling_does_not_claim_the_legacy_row(tmp_path):
    """The shared key never claims a sibling, with or without ids.

    B is ingested first. Its messages carry B's own timestamps, so they do not
    match the legacy row's (A's), and B must not take it.
    """
    project, a, b, key = _idless_pair(tmp_path)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, key)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (str(a),))
    conn.commit()

    _ingest(conn, tmp_path, order=[b, a])

    by_start = _conversations_by_start(conn)
    assert len(by_start) == 2
    assert _prompt_texts(conn, by_start[START_A]["id"]) == ["alpha one", "alpha two"]
    assert _prompt_texts(conn, by_start[START_B]["id"]) == ["bravo only"]
    assert tag in tag_names(conn, "conversation", by_start[START_A]["id"])
    assert tag not in tag_names(conn, "conversation", by_start[START_B]["id"])


def test_idless_sibling_a_month_later_with_the_same_prompt_does_not_claim_the_legacy_row(tmp_path):
    """Review round 2, finding 1: an id-less message is matched by text AND time.

    A and B share a sessionId and both prompt "continue", but B opened a month
    after A. Matching id-less messages by text alone let B adopt A's legacy row,
    deleting A's conversation and moving its tag to B. The timestamp is evidence
    too: B's message is not A's message.
    """
    project = tmp_path / "proj"
    B_START = "2026-01-28T16:42:00.000Z"
    a = _write_chat(
        project, "session-2025-12-28T16-42-2c1f6a0e",
        session_id=SESSION, start=START_A, last_updated="2025-12-28T17:00:00.000Z",
        prompts=["continue"], idless=True,
    )
    b = _write_chat(
        project, "session-2026-01-28T16-42-2c1f6a0e",
        session_id=SESSION, start=B_START,
        last_updated="2026-01-28T17:00:00.000Z",
        prompts=["continue"], idless=True,
    )
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_superseded_row(conn, a, f"{gemini_chat.NAME}::{SESSION}")
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files WHERE path = ?", (str(a),))
    conn.commit()

    _ingest(conn, tmp_path, order=[b, a])

    by_start = _conversations_by_start(conn)
    assert len(by_start) == 2, "B's ingest deleted A's legacy conversation"
    assert tag in tag_names(conn, "conversation", by_start[START_A]["id"]), "A's tag moved to B"
    assert tag not in tag_names(conn, "conversation", by_start[B_START]["id"])
