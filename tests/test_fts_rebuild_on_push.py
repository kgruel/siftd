"""AC test for ST-3b: FTS rebuild on push.

Probes the two paths through receive_database():
  1. First-push (target doesn't exist) — _create_from_source path.
  2. Subsequent-push (target exists)   — merge_database path.

In both cases, calling with rebuild_fts=True must make content_fts
searchable for phrases that live in event_content.
"""

import sqlite3

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


def test_first_push_no_rebuild_leaves_fts_empty(tmp_path):
    """receive_database with rebuild_fts=False on a new target leaves content_fts empty."""
    anchor = "smoke-test-anchor-beta"
    sender = _make_sender(tmp_path, "sender.db", anchor)
    target = tmp_path / "target.db"

    receive_database(sender, target, rebuild_fts=False, preflight=False)

    assert _fts_hit_count(target, anchor) == 0, (
        "FTS should remain empty after first-push with rebuild_fts=False"
    )


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
