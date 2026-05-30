"""D2 — orphan content_blob GC on merge.

content_blobs are copied wholesale into the target, but the event_tool_call
rows that reference them are filtered (to landed/eligible events). A blob whose
only referrer was filtered out (e.g. its conversation was skipped as a
duplicate) ends up ref_count=0 with nothing to collect it — unbounded growth on
overlapping re-pushes. The merge now GCs those orphans; legitimately-referenced
blobs must survive.
"""

from __future__ import annotations

import sqlite3

from siftd.api.merge import merge_database
from siftd.storage.sqlite import (
    clear_vocabulary_caches,
    create_database,
    get_or_create_harness,
)

STARTED = "2024-01-01T00:00:00Z"


def _seed_event_with_blob(conn, *, conv_id, event_id, blob_hash, harness):
    conn.execute(
        "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?,?,?,?)",
        (conv_id, "conv-A", harness, STARTED),
    )
    conn.execute(
        "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?,?,?,?)",
        (event_id, "tool_call", conv_id, STARTED),
    )
    conn.execute(
        "INSERT INTO content_blobs (hash, content, ref_count, created_at) VALUES (?,?,?,?)",
        (blob_hash, "tool output", 1, STARTED),
    )
    conn.execute(
        "INSERT INTO event_tool_call (event_id, tool_id, input, result_hash, status) "
        "VALUES (?,?,?,?,?)",
        (event_id, None, "{}", blob_hash, "ok"),
    )


def test_orphan_blob_from_skipped_conversation_is_gced(tmp_path):
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"

    # Target already has conv-A with a NEWER ulid → the source copy is a
    # duplicate that gets skipped (not replaced), so its blob is never referenced.
    tc = create_database(target)
    th = get_or_create_harness(tc, "h", source="t", log_format="jsonl")
    tc.execute(
        "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?,?,?,?)",
        ("01ZZZZZZZZZZZZZZZZZZZZZZZZZ", "conv-A", th, STARTED),
    )
    tc.commit()
    tc.close()
    clear_vocabulary_caches()

    sc = create_database(source)
    sh = get_or_create_harness(sc, "h", source="t", log_format="jsonl")
    _seed_event_with_blob(
        sc, conv_id="01AAAAAAAAAAAAAAAAAAAAAAAAA",  # older ulid → skipped
        event_id="01EVENT00000000000000000000", blob_hash="ORPHANHASH", harness=sh,
    )
    sc.commit()
    sc.close()

    merge_database(target, source)

    conn = sqlite3.connect(str(target))
    try:
        # The orphan blob (its tool_call's conversation was skipped) is gone...
        assert conn.execute(
            "SELECT COUNT(*) FROM content_blobs WHERE hash='ORPHANHASH'"
        ).fetchone()[0] == 0
        # ...and no ref_count=0 orphans remain at all.
        assert conn.execute(
            "SELECT COUNT(*) FROM content_blobs WHERE ref_count=0"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_referenced_blob_survives_gc(tmp_path):
    """The GC must not delete a blob whose tool_call actually lands."""
    target = tmp_path / "target.db"
    source = tmp_path / "source.db"

    create_database(target).close()  # empty target — source conversation is new
    sc = create_database(source)
    sh = get_or_create_harness(sc, "h", source="t", log_format="jsonl")
    _seed_event_with_blob(
        sc, conv_id="01CONV00000000000000000000",
        event_id="01EVENT00000000000000000000", blob_hash="LIVEHASH", harness=sh,
    )
    sc.commit()
    sc.close()

    merge_database(target, source)

    conn = sqlite3.connect(str(target))
    try:
        row = conn.execute(
            "SELECT ref_count FROM content_blobs WHERE hash='LIVEHASH'"
        ).fetchone()
        assert row is not None, "referenced blob was wrongly GC'd"
        assert row[0] == 1
    finally:
        conn.close()
