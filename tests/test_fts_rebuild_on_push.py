"""Merged content is searchable, on every path and at every setting.

Two invariants live here, and they are two halves of one thing.

ST-3b: ``rebuild_fts=True`` makes ``content_fts`` searchable for phrases in
``event_content``, through both paths of ``receive_database`` — first-push
(``_create_from_source``) and subsequent-push (``merge_database``).

siftd#49: that flag chooses whether to pay for a *full* rebuild, and it is not
what decides whether merged content is indexed at all. Nothing may reach a
committed merge unindexed, whatever the caller passed — so
``test_merged_content_is_always_searchable`` runs the matrix of paths and
settings and asserts ``missing_count == 0`` across all of them. Before #49 six
of its nine cases failed, including a *replaced* conversation, which #20's
index-delete had made unsearchable rather than stale.
"""

import sqlite3

import pytest
from conftest import make_db

from siftd.api.receive import receive_database
from siftd.storage.fts import rebuild_fts_index


def _fts_hit_count(db_path, phrase):
    conn = sqlite3.connect(str(db_path))
    try:
        # Wrap in double quotes → FTS5 phrase query; required for hyphenated strings
        # because bare hyphens are the NOT operator in FTS5 query syntax.
        return conn.execute(
            'SELECT COUNT(*) FROM content_fts WHERE content_fts MATCH ?',
            (f'"{phrase}"',),
        ).fetchone()[0]
    finally:
        conn.close()


def _make_sender(tmp_path, name, anchor_phrase):
    """Build a sender DB with a conversation containing a distinctive anchor phrase."""
    db = make_db(
        tmp_path / name,
        conversations=[
            {
                "external_id": f"conv-{name}",
                "prompt_text": f"Question before anchor {anchor_phrase}",
                "response_text": anchor_phrase,
            }
        ],
    )
    # make_db does not rebuild FTS — push slices also have no FTS. Confirm.
    conn = sqlite3.connect(str(db))
    try:
        hits = conn.execute(
            'SELECT COUNT(*) FROM content_fts WHERE content_fts MATCH ?',
            (f'"{anchor_phrase}"',),
        ).fetchone()[0]
    finally:
        conn.close()
    assert hits == 0, "sender DB should have empty FTS before the test"
    return db


def test_first_push_rebuilds_fts(tmp_path):
    """receive_database with rebuild_fts=True on a new target rebuilds content_fts."""
    anchor = "smoke-test-anchor-alpha"
    sender = _make_sender(tmp_path, "sender.db", anchor)
    target = tmp_path / "target.db"

    assert not target.exists(), "target must not exist for first-push path"

    receive_database(sender, target, rebuild_fts=True, preflight=False)

    assert target.exists()
    assert _fts_hit_count(target, anchor) >= 1, (
        "FTS should find the anchor phrase after first-push with rebuild_fts=True"
    )


def test_first_push_indexes_even_with_rebuild_off(tmp_path):
    """rebuild_fts=False no longer leaves a first push entirely unindexed (#49).

    This asserted the opposite until #49. A push slice is built with the index
    off — it is transport, not a corpus — so a receive-only server whose
    `serve.fts_rebuild` was anything but `on_push` got a database that no
    search could reach and that no later push would repair, because every later
    push is a merge and merges never wrote the index either.
    """
    anchor = "smoke-test-anchor-beta"
    sender = _make_sender(tmp_path, "sender.db", anchor)
    target = tmp_path / "target.db"

    receive_database(sender, target, rebuild_fts=False, preflight=False)

    assert _fts_hit_count(target, anchor) >= 1


def test_subsequent_push_rebuilds_fts(tmp_path):
    """receive_database with rebuild_fts=True on an existing target rebuilds content_fts."""
    anchor = "smoke-test-anchor-gamma"

    # Seed an existing target with a different conversation
    target = make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-existing", "prompt_text": "existing content"}],
    )
    rebuild_fts_index(sqlite3.connect(str(target)), commit=True)

    sender = _make_sender(tmp_path, "sender.db", anchor)

    receive_database(sender, target, rebuild_fts=True, preflight=False)

    assert _fts_hit_count(target, anchor) >= 1, (
        "FTS should find the anchor phrase after subsequent push with rebuild_fts=True"
    )


def test_subsequent_push_drops_replaced_text_without_rebuild(tmp_path):
    """A push that replaces a conversation retires its old text, even with rebuild_fts=False.

    siftd#20. content_fts is virtual — no FK, no cascade, invisible to
    foreign_key_check — so the merge's stale-conversation delete has to clear it
    explicitly. rebuild_fts=False is the only setting under which that matters,
    and it is this function's default and what every sync caller passes.
    """
    import time

    stale_anchor = "smoke-test-anchor-delta"
    fresh_anchor = "smoke-test-anchor-epsilon"

    target = make_db(
        tmp_path / "target.db",
        conversations=[{"external_id": "conv-1", "response_text": stale_anchor}],
    )
    rebuild_fts_index(sqlite3.connect(str(target)), commit=True)
    assert _fts_hit_count(target, stale_anchor) == 1

    # Later ULID for the same (harness, external_id) → the merge replaces it.
    time.sleep(0.01)
    sender = make_db(
        tmp_path / "sender.db",
        conversations=[{"external_id": "conv-1", "response_text": fresh_anchor}],
    )

    result = receive_database(sender, target, rebuild_fts=False, preflight=False)
    assert result["replaced_conversations"] == 1

    assert _fts_hit_count(target, stale_anchor) == 0, (
        "the replaced conversation must stop answering searches from its deleted text"
    )
    assert _fts_hit_count(target, fresh_anchor) == 1, (
        "...and must start answering from the text that replaced it (#49) — "
        "deleting the old rows without writing the new ones is not 'stale', "
        "it is gone"
    )


# --- siftd#49: the invariant, across every path a merge can arrive by --------

def _seed(tmp_path, name, phrase, *, indexed):
    """A one-conversation DB carrying ``phrase``, optionally already indexed.

    Targets are seeded *indexed* so a non-zero ``missing_count`` afterwards is
    content the merge under test wrote and failed to index, not pre-existing
    drift the merge inherited.
    """
    db = make_db(
        tmp_path / name,
        conversations=[{"external_id": f"conv-{name}",
                        "prompt_text": f"question {phrase}", "response_text": phrase}],
    )
    if indexed:
        conn = sqlite3.connect(str(db))
        try:
            rebuild_fts_index(conn, commit=True)
        finally:
            conn.close()
    return db


def _fts_sync_status(db_path):
    from siftd.storage.fts import get_fts_sync_status

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return get_fts_sync_status(conn)
    finally:
        conn.close()


# (label, rebuild_fts) — the value each caller actually passes.
#   receive_database's own default; `siftd db receive` / inbox / `db pull`;
#   and the three `serve.fts_rebuild` settings, of which only "on_push" maps
#   to True. "scheduled" is documented as "rebuild periodically" but nothing
#   schedules anything, so it is `off` with a friendlier name — which is why
#   it must not be the difference between searchable and not.
_MERGE_CALLERS = [
    ("receive default / sync._push_local", False),
    ("db receive, inbox, db pull", True),
    ("serve.fts_rebuild='on_push'", True),
    ("serve.fts_rebuild='scheduled'", False),
    ("serve.fts_rebuild='off'", False),
]


@pytest.mark.parametrize("label,rebuild_fts", _MERGE_CALLERS, ids=[c[0] for c in _MERGE_CALLERS])
def test_merged_content_is_always_searchable(tmp_path, label, rebuild_fts):
    """No caller may commit a merge that leaves its own content unindexed."""
    anchor = "merge-invariant-anchor"
    target = _seed(tmp_path, "target.db", "pre-existing-text", indexed=True)
    receive_database(_seed(tmp_path, "sender.db", anchor, indexed=False),
                     target, rebuild_fts=rebuild_fts, preflight=False)

    assert _fts_sync_status(target) == {"orphaned_count": 0, "missing_count": 0}
    assert _fts_hit_count(target, anchor) >= 1
    # The conversation already there keeps its index rows — a scoped index
    # write must not clear what it did not touch.
    assert _fts_hit_count(target, "pre-existing-text") >= 1


@pytest.mark.parametrize("rebuild_fts", [False, True])
def test_first_push_is_always_searchable(tmp_path, rebuild_fts):
    """The copy path has nothing to scope to: the whole file is what arrived."""
    anchor = "first-push-invariant-anchor"
    target = tmp_path / "fresh.db"
    receive_database(_seed(tmp_path, "sender.db", anchor, indexed=False),
                     target, rebuild_fts=rebuild_fts, preflight=False)

    assert _fts_sync_status(target) == {"orphaned_count": 0, "missing_count": 0}
    assert _fts_hit_count(target, anchor) >= 1


def test_local_path_push_indexes_the_remote(tmp_path):
    """`siftd db push` to a local-path remote merges with rebuild_fts=False.

    Exercised through `_push_local` rather than `receive_database` because it
    reaches `merge_database` directly — a second door onto the same merge, and
    the one no `serve.fts_rebuild` setting can reopen.
    """
    from siftd.api.sync import _push_local
    from siftd.domain.sync import SyncRemote

    anchor = "local-push-invariant-anchor"
    remote = _seed(tmp_path, "remote.db", "pre-existing-text", indexed=True)
    _push_local(
        SyncRemote(name="r", host=None, path=str(remote)),
        _seed(tmp_path, "slice.db", anchor, indexed=False),
        tmp_path / "local.db",
    )

    assert _fts_sync_status(remote) == {"orphaned_count": 0, "missing_count": 0}
    assert _fts_hit_count(remote, anchor) >= 1
