"""Regression tests for codex_cli file identity under a legacy collision (ar/codex-port, #93).

Codex files were keyed on the last ``session_meta`` id. A forked child restates its
parent's meta after its own, so 35 child files took their parent's id and shared it
(11 ids in all). The file strategy then linked every colliding file to the one row
stored under that id, so the children were never stored as conversations.

Each file is now named by its first ``session_meta``. A child's previous key is the
parent's id, which it names as superseded, so ingest can adopt the legacy row. These
drive the real codex_cli parser through the real ingest_all against a temp database.
They pin three properties:

- after the documented recovery (clear the bookkeeping, ingest again), a family of
  files that shared one legacy row becomes one conversation per file, and the tags
  on the row stay with the parent's conversation;
- a changed child that is linked to the legacy row is released from it and stored
  under its own id, rather than settled as a duplicate of its parent;
- a file that really is a copy of the conversation the legacy row holds still
  settles as a copy and does not create a second conversation.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import tag_names

from siftd.adapters import codex_cli
from siftd.adapters.contract import adapt
from siftd.cli import main
from siftd.domain.source import Source
from siftd.ingestion import ingest_all
from siftd.storage.sqlite import (
    compute_file_hash,
    create_database,
    ensure_conversation_owners_table,
    record_ingested_file,
)
from siftd.storage.tags import apply_tag, get_or_create_tag

PARENT = "019f66ea-963d-7101-a549-37c4fd3fccaa"
CHILD_A = "019f66ea-bd42-7ea3-acbc-13a34a3aa4d0"
CHILD_B = "019f66ea-ccd2-71b0-b13b-886aad02b8a4"
LEGACY_KEY = f"{codex_cli.NAME}::{PARENT}"


def _line(ts, kind, payload):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def _meta(ts, ident, **extra):
    return _line(ts, "session_meta", {"id": ident, "cwd": "/Users/user/project", **extra})


def _message(ts, role, text):
    part = "input_text" if role == "user" else "output_text"
    return _line(ts, "response_item", {
        "type": "message", "role": role, "content": [{"type": part, "text": text}],
    })


def _user(ts, text):
    return _message(ts, "user", text)


def _assistant(ts, text):
    return _message(ts, "assistant", text)


def _parent_history():
    return [
        _user("2026-07-15T17:54:40.000Z", "alpha question"),
        _assistant("2026-07-15T17:54:41.000Z", "alpha answer"),
    ]


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _family(root: Path):
    """The parent and its two forked children.

    Each child names the parent as both its subagent parent and the session it forked
    from, and states the parent's meta after its own, as a fork does. The children
    share the parent's first prompt here, so a legacy row holding the parent's messages
    matches each child: the case where adoption is refused by a claim. In the local
    corpus the real children share no prompt timestamp with the parent.
    """
    day = root / "2026" / "07" / "15"
    lineage = {"forked_from_id": PARENT,
               "source": {"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}}
    parent = _write(day / f"rollout-a-{PARENT}.jsonl", [
        _meta("2026-07-15T17:54:37.000Z", PARENT),
        *_parent_history(),
    ])
    child_a = _write(day / f"rollout-b-{CHILD_A}.jsonl", [
        _meta("2026-07-15T17:54:47.000Z", CHILD_A, **lineage),
        _meta("2026-07-15T17:54:48.000Z", PARENT),
        *_parent_history(),
        _user("2026-07-15T17:54:50.000Z", "bravo child question"),
        _assistant("2026-07-15T17:54:51.000Z", "bravo child answer"),
    ])
    child_b = _write(day / f"rollout-c-{CHILD_B}.jsonl", [
        _meta("2026-07-15T17:54:51.000Z", CHILD_B, **lineage),
        _meta("2026-07-15T17:54:52.000Z", PARENT),
        *_parent_history(),
        _user("2026-07-15T17:54:55.000Z", "charlie child question"),
        _assistant("2026-07-15T17:54:56.000Z", "charlie child answer"),
    ])
    return parent, child_a, child_b


def _codex_adapter_at(root: Path, order=None):
    """The real codex_cli parser, discovering only under ``root``.

    ``order`` pins the discovery order, for tests whose result depends on which
    file is ingested first.
    """

    class _Adapter:
        ADAPTER_INTERFACE_VERSION = 1
        NAME = codex_cli.NAME
        DEFAULT_LOCATIONS: list[str] = []
        DEDUP_STRATEGY = codex_cli.DEDUP_STRATEGY
        HARNESS_SOURCE = codex_cli.HARNESS_SOURCE
        HARNESS_LOG_FORMAT = codex_cli.HARNESS_LOG_FORMAT
        HARNESS_DISPLAY_NAME = codex_cli.HARNESS_DISPLAY_NAME
        TOOL_ALIASES = codex_cli.TOOL_ALIASES
        can_handle = staticmethod(codex_cli.can_handle)
        parse = staticmethod(adapt(codex_cli).parse)

        @staticmethod
        def discover(locations=None):
            if order is None:
                yield from codex_cli.discover([str(root)])
            else:
                for path in order:
                    yield Source(kind="file", location=path)

    return _Adapter


def _ingest(conn, root: Path, order=None):
    return ingest_all(conn, [_codex_adapter_at(root, order)], filter_binary=False)


def _external_ids(conn) -> dict[str, str]:
    """external_id -> conversation id, for every stored conversation."""
    return {
        row["external_id"]: row["id"]
        for row in conn.execute("SELECT id, external_id FROM conversations")
    }


def _prompt_texts(conn, conversation_id) -> list[str]:
    rows = conn.execute(
        "SELECT ec.content FROM events e "
        "JOIN event_content ec ON ec.event_id = e.id "
        "WHERE e.conversation_id = ? AND e.kind = 'prompt' AND ec.block_type = 'text'",
        (conversation_id,),
    ).fetchall()
    return sorted(json.loads(r[0])["text"] for r in rows)


def _claim(conn, path: Path, conversation_id: str) -> None:
    """Record a path's bookkeeping row pointing at a conversation, as a legacy collision did."""
    st = path.stat()
    record_ingested_file(
        conn, str(path), compute_file_hash(path), conversation_id,
        file_mtime=st.st_mtime, file_size=st.st_size, commit=True,
    )


def _tag(conn, conversation_id, name="decision:codex-identity"):
    apply_tag(conn, "conversation", conversation_id, get_or_create_tag(conn, name), commit=True)
    return name


def _store_legacy_parent(conn, root: Path, parent: Path) -> str:
    """Ingest the parent alone: the row the legacy release kept under the parent's id."""
    _ingest(conn, root, order=[parent])
    return _external_ids(conn)[LEGACY_KEY]


def _store_legacy_child(conn, root: Path, child: Path) -> str:
    """Ingest the child as the legacy release named it: by the restated parent meta, the last one.

    The child's own meta is removed for this ingest only, so the parser names the file by
    the parent's id, as the legacy last-wins rule did; the file is restored afterwards.
    """
    text = child.read_text(encoding="utf-8")
    _, rest = text.split("\n", 1)
    child.write_text(rest, encoding="utf-8")
    try:
        _ingest(conn, root, order=[child])
    finally:
        child.write_text(text, encoding="utf-8")
    return _external_ids(conn)[LEGACY_KEY]


def test_a_cleared_family_keeps_each_child_as_its_own_conversation(tmp_path):
    """Children linked to the parent's row, then the bookkeeping is cleared.

    This is the documented recovery: every file is then new. The child's adoption of the
    legacy row matches on the shared prompt and is refused by the parent's claim; each
    child must still be stored under its own id, not settled as a copy of the parent.
    """
    root = tmp_path / "sessions"
    parent, child_a, child_b = _family(root)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_parent(conn, root, parent)
    tag = _tag(conn, legacy_id)
    _claim(conn, child_a, legacy_id)
    _claim(conn, child_b, legacy_id)
    conn.execute("DELETE FROM ingested_files")
    conn.commit()

    _ingest(conn, root, order=[parent, child_a, child_b])

    by_id = _external_ids(conn)
    assert set(by_id) == {LEGACY_KEY, f"{codex_cli.NAME}::{CHILD_A}", f"{codex_cli.NAME}::{CHILD_B}"}
    assert by_id[LEGACY_KEY] == legacy_id
    assert _prompt_texts(conn, by_id[f"{codex_cli.NAME}::{CHILD_A}"]) == [
        "alpha question", "bravo child question"]
    assert _prompt_texts(conn, by_id[f"{codex_cli.NAME}::{CHILD_B}"]) == [
        "alpha question", "charlie child question"]
    assert tag in tag_names(conn, "conversation", legacy_id)
    assert tag not in tag_names(conn, "conversation", by_id[f"{codex_cli.NAME}::{CHILD_A}"])
    linked = dict(conn.execute("SELECT path, conversation_id FROM ingested_files").fetchall())
    assert linked[str(child_a)] == by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert linked[str(child_b)] == by_id[f"{codex_cli.NAME}::{CHILD_B}"]


def test_a_changed_child_linked_to_the_legacy_row_gets_its_own_conversation(tmp_path):
    """The reviewer's shape, on the changed-file path: a child linked to the parent's row whose file changed.

    The refusal comes from the claim before any adoption is considered, so the child is
    released from the row and stored under its own id, rather than skipped as a duplicate.
    """
    root = tmp_path / "sessions"
    parent, child_a, _child_b = _family(root)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_parent(conn, root, parent)
    tag = _tag(conn, legacy_id)
    _claim(conn, child_a, legacy_id)
    child_a.write_text(child_a.read_text() + _user("2026-07-15T17:55:00.000Z", "bravo follow-up") + "\n")

    _ingest(conn, root, order=[child_a])

    by_id = _external_ids(conn)
    own = by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert by_id[LEGACY_KEY] == legacy_id
    assert _prompt_texts(conn, own) == ["alpha question", "bravo child question", "bravo follow-up"]
    assert tag in tag_names(conn, "conversation", legacy_id)
    assert tag not in tag_names(conn, "conversation", own)


def test_a_true_copy_of_the_conversation_the_legacy_row_holds_still_settles(tmp_path):
    """A backup copy of the child claims a legacy row that holds the child's transcript: settle, not store.

    The row stores the child's whole transcript (the legacy release keyed it by the parent's id) and
    the copy's identity is the child's own, so the claim is a second copy and storing the child
    again would duplicate it.
    """
    root = tmp_path / "sessions"
    _parent, child_a, _child_b = _family(root)
    backup = _write(tmp_path / "backup" / "sessions" / "copy.jsonl", child_a.read_text().splitlines())
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_child(conn, root, child_a)
    _claim(conn, backup, legacy_id)

    _ingest(conn, root, order=[child_a])

    assert set(_external_ids(conn)) == {LEGACY_KEY}
    assert _external_ids(conn)[LEGACY_KEY] == legacy_id
    linked = dict(conn.execute("SELECT path, conversation_id FROM ingested_files").fetchall())
    assert linked[str(child_a)] == legacy_id


def _parent_row_claimed_by_a_backup_of_child_a(tmp_path):
    """The row holds only the parent's transcript, and a backup of child_a (child_a's identity) claims it."""
    root = tmp_path / "sessions"
    parent, child_a, _child_b = _family(root)
    backup = _write(tmp_path / "backup" / "sessions" / "copy.jsonl", child_a.read_text().splitlines())
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_parent(conn, root, parent)
    _claim(conn, backup, legacy_id)
    return root, child_a, backup, conn, legacy_id


def test_a_claimant_with_the_childs_identity_does_not_freeze_a_child_the_row_never_held(tmp_path):
    """The row holds only the parent's transcript, so a claim by a copy of the child is not the child's storage.

    A matching claimant identity shows that two files are copies of each other, not that either was
    stored. The child's transcript is absent from the row, so settling against it would leave the
    child without a conversation: it must be stored under its own id, once, beside the parent's row.
    """
    root, child_a, _backup, conn, legacy_id = _parent_row_claimed_by_a_backup_of_child_a(tmp_path)

    _ingest(conn, root, order=[child_a])

    by_id = _external_ids(conn)
    own = by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert by_id[LEGACY_KEY] == legacy_id
    assert _prompt_texts(conn, legacy_id) == ["alpha question"]
    assert _prompt_texts(conn, own) == ["alpha question", "bravo child question"]
    linked = dict(conn.execute("SELECT path, conversation_id FROM ingested_files").fetchall())
    assert linked[str(child_a)] == own


def test_a_child_prompt_appended_after_it_was_stored_beside_a_claimed_row_lands(tmp_path):
    """Growth of a child whose row was claimed by its copy is ingested, not skipped as a duplicate."""
    root, child_a, _backup, conn, _legacy_id = _parent_row_claimed_by_a_backup_of_child_a(tmp_path)
    _ingest(conn, root, order=[child_a])
    child_a.write_text(child_a.read_text() + _user("2026-07-15T17:55:00.000Z", "bravo follow-up") + "\n")

    _ingest(conn, root, order=[child_a])

    own = _external_ids(conn)[f"{codex_cli.NAME}::{CHILD_A}"]
    assert _prompt_texts(conn, own) == ["alpha question", "bravo child question", "bravo follow-up"]
    assert len(_external_ids(conn)) == 2


def test_a_copy_of_the_child_resolves_against_the_stored_child_not_the_parents_row(tmp_path):
    """Once the child is stored, its backup copy (still linked to the parent's row) changes and links to the child."""
    root, child_a, backup, conn, legacy_id = _parent_row_claimed_by_a_backup_of_child_a(tmp_path)
    _ingest(conn, root, order=[child_a])
    own = _external_ids(conn)[f"{codex_cli.NAME}::{CHILD_A}"]
    backup.write_text(backup.read_text() + _user("2026-07-15T17:55:00.000Z", "bravo follow-up") + "\n")

    ingest_all(conn, [_codex_adapter_at(root, [backup])], filter_binary=False)

    assert set(_external_ids(conn)) == {LEGACY_KEY, f"{codex_cli.NAME}::{CHILD_A}"}
    linked = dict(conn.execute("SELECT path, conversation_id FROM ingested_files").fetchall())
    assert linked[str(backup)] == own
    assert _prompt_texts(conn, legacy_id) == ["alpha question"]


_FAMILY_FILES = ("parent", "child_a", "child_b")


@pytest.mark.parametrize("order", list(itertools.permutations(_FAMILY_FILES)), ids="-".join)
def test_a_legacy_row_holding_a_child_keeps_its_tags_with_the_child_in_any_discovery_order(tmp_path, order):
    """The legacy row under the parent's id holds child_a's transcript, and the user tagged it.

    The tag belongs to child_a, the file whose transcript the row stores, whatever the order
    the family is discovered in. It must not land on the parent, and the second run must
    not move it: a replacement of the row by the parent would carry it there.
    """
    root = tmp_path / "sessions"
    files = dict(zip(_FAMILY_FILES, _family(root)))
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_child(conn, root, files["child_a"])
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files")
    conn.commit()

    for _ in range(2):
        _ingest(conn, root, order=[files[name] for name in order])

    by_id = _external_ids(conn)
    child_a = by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert set(by_id) == {LEGACY_KEY, f"{codex_cli.NAME}::{CHILD_A}", f"{codex_cli.NAME}::{CHILD_B}"}
    assert tag in tag_names(conn, "conversation", child_a)
    assert tag not in tag_names(conn, "conversation", by_id[LEGACY_KEY])
    assert tag not in tag_names(conn, "conversation", by_id[f"{codex_cli.NAME}::{CHILD_B}"])
    assert _prompt_texts(conn, by_id[LEGACY_KEY]) == ["alpha question"]
    assert _prompt_texts(conn, child_a) == ["alpha question", "bravo child question"]


def test_a_changed_child_linked_to_a_legacy_row_it_holds_takes_its_tags_with_it(tmp_path):
    """The legacy row holds child_a's transcript and is claimed by both the parent and child_a.

    child_a changes, so its link is refused by the parent's claim. It is a different
    conversation from the parent, so it is stored under its own id; the tags belong to the
    transcript the row holds, which is child_a's, so they move with it. The row stays for
    the parent's claim, untagged.
    """
    root = tmp_path / "sessions"
    parent, child_a, _child_b = _family(root)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_child(conn, root, child_a)
    tag = _tag(conn, legacy_id)
    ensure_conversation_owners_table(conn)
    conn.execute(
        "INSERT INTO conversation_owners (conversation_id, user_id, push_id, assigned_at)"
        " VALUES (?, 'alice', 'push-1', '2024-01-01T00:00:00Z')",
        (legacy_id,),
    )
    conn.execute("DELETE FROM ingested_files")
    conn.commit()
    _claim(conn, parent, legacy_id)
    _claim(conn, child_a, legacy_id)
    child_a.write_text(child_a.read_text() + _user("2026-07-15T17:55:00.000Z", "bravo follow-up") + "\n")

    _ingest(conn, root, order=[child_a])

    by_id = _external_ids(conn)
    own = by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert by_id[LEGACY_KEY] == legacy_id
    assert _prompt_texts(conn, own) == ["alpha question", "bravo child question", "bravo follow-up"]
    assert tag in tag_names(conn, "conversation", own)
    assert tag not in tag_names(conn, "conversation", legacy_id)
    owners = conn.execute("SELECT conversation_id FROM conversation_owners").fetchall()
    assert [row[0] for row in owners] == [own]


def test_a_row_its_claimant_also_holds_keeps_its_assignments(tmp_path):
    """The legacy row holds the parent's transcript, and the parent still claims it.

    The parent's content is a prefix of the child's, so the row is ambiguous between them:
    the claimant holds it too, and the tag stays on the row rather than moving to a child.
    """
    root = tmp_path / "sessions"
    parent, child_a, _child_b = _family(root)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_parent(conn, root, parent)
    tag = _tag(conn, legacy_id)
    _claim(conn, child_a, legacy_id)
    child_a.write_text(child_a.read_text() + _user("2026-07-15T17:55:00.000Z", "bravo follow-up") + "\n")

    _ingest(conn, root, order=[child_a])

    own = _external_ids(conn)[f"{codex_cli.NAME}::{CHILD_A}"]
    assert tag in tag_names(conn, "conversation", legacy_id)
    assert tag not in tag_names(conn, "conversation", own)


def test_a_legacy_row_holding_the_parent_is_not_adopted_by_a_child_discovered_first(tmp_path):
    """The parent's row is a prefix of child_a's transcript, and no claim is left to tell them apart.

    child_a inherits the parent's id, so the containment that makes the row a candidate
    is what any fork of the parent shows, and the row began before child_a did.
    Adoption is refused and reported: the row and its tag stay for the parent.
    """
    root = tmp_path / "sessions"
    parent, child_a, _child_b = _family(root)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_parent(conn, root, parent)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files")
    conn.commit()

    stats = _ingest(conn, root, order=[child_a, parent])

    by_id = _external_ids(conn)
    assert by_id[LEGACY_KEY] == legacy_id
    assert tag in tag_names(conn, "conversation", legacy_id)
    assert tag not in tag_names(conn, "conversation", by_id[f"{codex_cli.NAME}::{CHILD_A}"])
    assert _prompt_texts(conn, by_id[f"{codex_cli.NAME}::{CHILD_A}"]) == ["alpha question", "bravo child question"]
    assert [(r.path, r.conversation_id, r.external_id) for r in stats.refused_adoptions] == [
        (str(child_a), legacy_id, f"{codex_cli.NAME}::{CHILD_A}")
    ]


def test_a_childs_legacy_pointer_at_the_parents_row_is_not_an_adoption(tmp_path):
    """External review r2, finding 2: a collision pointer is not evidence the row holds that file's conversation.

    The legacy release pointed child_a's bookkeeping at the parent's row, and the
    parent's own bookkeeping is gone, so no claim is left to tell them apart. The row
    is under the id child_a inherits and began before child_a did, so the grown child
    is stored beside it, the row keeps its tag, and the refusal is reported.
    """
    root = tmp_path / "sessions"
    parent, child_a, _child_b = _family(root)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_parent(conn, root, parent)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files")
    _claim(conn, child_a, legacy_id)
    child_a.write_text(child_a.read_text() + _user("2026-07-15T17:55:00.000Z", "bravo follow-up") + "\n")

    stats = _ingest(conn, root, order=[child_a])

    by_id = _external_ids(conn)
    own = by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert by_id[LEGACY_KEY] == legacy_id
    assert _prompt_texts(conn, legacy_id) == ["alpha question"]
    assert tag in tag_names(conn, "conversation", legacy_id)
    assert tag not in tag_names(conn, "conversation", own)
    assert [(r.path, r.conversation_id) for r in stats.refused_adoptions] == [(str(child_a), legacy_id)]


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_siftd_ingest_names_a_refused_adoption(tmp_path, monkeypatch, capsys, json_mode):
    """The refusal is reported where the unrecognized-records line is, even when auto-quiet."""
    root = tmp_path / "sessions"
    parent, child_a, _child_b = _family(root)
    db = tmp_path / "siftd.db"
    conn = create_database(db)
    legacy_id = _store_legacy_parent(conn, root, parent)
    conn.execute("DELETE FROM ingested_files")
    conn.commit()
    conn.close()
    monkeypatch.setattr("siftd.paths.ensure_dirs", lambda: None)
    monkeypatch.setattr(
        "siftd.api.ingest.load_all_adapters",
        lambda **_kw: [SimpleNamespace(name=codex_cli.NAME, module=_codex_adapter_at(root, [child_a]))],
    )

    rc = main(["--db", str(db), "ingest", *(["--json"] if json_mode else [])])

    assert rc == 0
    captured = capsys.readouterr()
    child = f"{codex_cli.NAME}::{CHILD_A}"
    if json_mode:
        events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
        (warning,) = [e for e in events if e.get("kind") == "refused_adoption"]
        assert (warning["adapter"], warning["path"], warning["conversation_id"], warning["external_id"]) == (
            codex_cli.NAME, str(child_a), legacy_id, child,
        )
    else:
        assert f"did not adopt conversation {legacy_id} for {child}" in captured.err


def _fork(root: Path, name: str, own_lines: list[str], **lineage) -> Path:
    """A fork of the parent: its own meta (with ``lineage`` fields, if any), the parent's restated meta and history."""
    return _write(root / "2026" / "07" / "15" / f"rollout-d-{name}.jsonl", [
        _meta("2026-07-15T17:54:47.000Z", CHILD_A, **lineage),
        _meta("2026-07-15T17:54:48.000Z", PARENT),
        *_parent_history(),
        *own_lines,
    ])


@pytest.mark.parametrize(
    "own_lines, lineage",
    [
        pytest.param([], {"forked_from_id": PARENT}, id="fork-adding-no-prompt"),
        pytest.param([_user("2026-07-15T17:54:50.000Z", "bravo child question")], {}, id="fork-stating-no-lineage"),
    ],
)
def test_a_parent_row_is_not_adopted_on_prompts_or_lineage_alone(tmp_path, own_lines, lineage):
    """External review r1, finding 2: equal prompt sets and absent lineage fields are not ownership.

    A fork that adds no prompt holds exactly its parent's prompts, and a fork whose meta
    states no lineage still restates its parent's id. Either way the parent's row (a read
    of the parent's file, which began before the fork) keeps its tag, and the refusal is
    reported.
    """
    root = tmp_path / "sessions"
    parent, _child_a, _child_b = _family(root)
    child = _fork(root, CHILD_A, own_lines, **lineage)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_parent(conn, root, parent)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files")
    conn.commit()

    stats = _ingest(conn, root, order=[child])

    by_id = _external_ids(conn)
    assert by_id[LEGACY_KEY] == legacy_id
    assert tag in tag_names(conn, "conversation", legacy_id)
    assert tag not in tag_names(conn, "conversation", by_id[f"{codex_cli.NAME}::{CHILD_A}"])
    assert [r.conversation_id for r in stats.refused_adoptions] == [legacy_id]


def test_a_legacy_row_holding_an_older_read_of_the_child_is_adopted(tmp_path):
    """The row a previous release stored from the child, before the child grew, is the child's: it began when the child began."""
    root = tmp_path / "sessions"
    _parent, child_a, _child_b = _family(root)
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_child(conn, root, child_a)
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files")
    conn.commit()
    child_a.write_text(child_a.read_text() + _user("2026-07-15T17:55:00.000Z", "bravo follow-up") + "\n")

    stats = _ingest(conn, root, order=[child_a])

    by_id = _external_ids(conn)
    own = by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert set(by_id) == {f"{codex_cli.NAME}::{CHILD_A}"}
    assert _prompt_texts(conn, own) == ["alpha question", "bravo child question", "bravo follow-up"]
    assert tag in tag_names(conn, "conversation", own)
    assert stats.refused_adoptions == []


def test_a_legacy_child_stored_with_indexed_injected_text_keeps_its_tags_through_recovery(tmp_path):
    """Delta review: the release before the arc stored injected context as indexed ``text``.

    The parser now keeps it on ``meta_text``. If a message's identity followed that
    display choice, the child could not recognise its own legacy row, the parent would
    link and then replace it, and the child's tag would move to the parent.
    """
    root = tmp_path / "sessions"
    parent, _child_a, _child_b = _family(root)
    _child_b.unlink()
    lineage = {"forked_from_id": PARENT,
               "source": {"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}}
    child = _write(_child_a, [
        _meta("2026-07-15T17:54:47.000Z", CHILD_A, **lineage),
        _meta("2026-07-15T17:54:48.000Z", PARENT),
        *_parent_history(),
        _user("2026-07-15T17:54:49.000Z", "# AGENTS.md instructions for /Users/user/project\n\nbe brief"),
        _user("2026-07-15T17:54:50.000Z", "bravo child question"),
        _assistant("2026-07-15T17:54:51.000Z", "bravo child answer"),
    ])
    conn = create_database(tmp_path / "siftd.db")
    legacy_id = _store_legacy_child(conn, root, child)
    restated = conn.execute(
        "UPDATE event_content SET block_type = 'text',"
        " content = json_object('text', json_extract(content, '$.meta_text'))"
        " WHERE block_type = 'meta_text'"
    ).rowcount
    conn.commit()
    assert restated == 1
    tag = _tag(conn, legacy_id)
    conn.execute("DELETE FROM ingested_files")
    conn.commit()

    for _ in range(2):
        _ingest(conn, root, order=[parent, child])

    by_id = _external_ids(conn)
    own = by_id[f"{codex_cli.NAME}::{CHILD_A}"]
    assert tag in tag_names(conn, "conversation", own)
    assert tag not in tag_names(conn, "conversation", by_id[LEGACY_KEY])
    assert _prompt_texts(conn, by_id[LEGACY_KEY]) == ["alpha question"]
