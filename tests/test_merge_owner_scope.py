"""Multi-tenant write-IDOR guard for the merge path (S0/S1/D1).

The merge was owner-blind: it threaded the pushing identity in only to *stamp*
new conversations, never to *gate* what could be written or replaced. A pusher
could forge child rows into — or destroy — another tenant's conversations.
These tests pin the owner-partitioned behavior: a push with user_id=B may only
write to conversations B owns or that are unowned.

See docs/dev/coverage-gap-review-2026-05-29-security.md (S0/S1/D1).
"""

from __future__ import annotations

import sqlite3

import pytest

from siftd.api.receive import receive_database
from siftd.storage.sqlite import (
    clear_vocabulary_caches,
    create_database,
    get_or_create_harness,
)

STARTED = "2024-01-01T00:00:00Z"
ALICE_CONV = "01ALICE0000000000000000000"
ALICE_EVENT = "01ALICEEVENT00000000000000"


def _target_with_alice(tmp_path):
    """A target DB holding one conversation + event owned by 'alice'."""
    target = tmp_path / "target.db"
    tc = create_database(target)
    th = get_or_create_harness(tc, "h", source="t", log_format="jsonl")
    tc.execute(
        "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?,?,?,?)",
        (ALICE_CONV, "conv-A", th, STARTED),
    )
    tc.execute(
        "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?,?,?,?)",
        (ALICE_EVENT, "prompt", ALICE_CONV, STARTED),
    )
    tc.execute(
        "INSERT INTO conversation_owners (conversation_id, user_id, push_id, assigned_at) "
        "VALUES (?,?,?,?)",
        (ALICE_CONV, "alice", "p1", STARTED),
    )
    tc.commit()
    tc.close()
    clear_vocabulary_caches()
    return target


def test_pusher_cannot_inject_children_into_another_tenants_conversation(tmp_path):
    """S0: Bob crafts rows referencing Alice's conversation/event IDs; none land."""
    target = _target_with_alice(tmp_path)

    # Bob's slice references Alice's conversation + event (he'd need to know/guess
    # her ULIDs). Source rows are FK-valid (the parent conv/event exist in src).
    source = tmp_path / "source.db"
    sc = create_database(source)
    sh = get_or_create_harness(sc, "h", source="t", log_format="jsonl")
    sc.execute(
        "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?,?,?,?)",
        (ALICE_CONV, "conv-A", sh, STARTED),
    )
    sc.execute(  # Alice's existing event, so forged content can target it
        "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?,?,?,?)",
        (ALICE_EVENT, "prompt", ALICE_CONV, STARTED),
    )
    sc.execute(  # a forged assistant turn injected into Alice's conversation
        "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?,?,?,?)",
        ("01BOBEVENT0000000000000000", "response", ALICE_CONV, STARTED),
    )
    sc.execute(  # forged content grafted onto Alice's existing event
        "INSERT INTO event_content (id, event_id, block_index, block_type, content) "
        "VALUES (?,?,?,?,?)",
        ("01BOBCONTENT00000000000000", ALICE_EVENT, 0, "text", "forged by bob"),
    )
    sc.commit()
    sc.close()

    receive_database(source, target, user_id="bob", push_id="p2")

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    try:
        # No forged event attached to Alice's conversation.
        ev_ids = {
            r["id"] for r in conn.execute(
                "SELECT id FROM events WHERE conversation_id=?", (ALICE_CONV,)
            ).fetchall()
        }
        assert ev_ids == {ALICE_EVENT}, "Bob's event was injected into Alice's conversation"
        # No forged content grafted onto Alice's event.
        grafted = conn.execute(
            "SELECT content FROM event_content WHERE event_id=?", (ALICE_EVENT,)
        ).fetchall()
        assert grafted == [], "Bob grafted content onto Alice's event"
        # Alice still owns her conversation.
        owner = conn.execute(
            "SELECT user_id FROM conversation_owners WHERE conversation_id=?", (ALICE_CONV,)
        ).fetchone()
        assert owner["user_id"] == "alice"
    finally:
        conn.close()


def test_pusher_can_create_and_own_its_new_conversation(tmp_path):
    """The guard must not over-block: Bob's own NEW conversation lands + is his."""
    target = _target_with_alice(tmp_path)

    bob_conv = "01BOBCONV00000000000000000"
    bob_event = "01BOBOWNEVENT00000000000000"
    source = tmp_path / "source.db"
    sc = create_database(source)
    sh = get_or_create_harness(sc, "h", source="t", log_format="jsonl")
    sc.execute(
        "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?,?,?,?)",
        (bob_conv, "conv-B", sh, STARTED),
    )
    sc.execute(
        "INSERT INTO events (id, kind, conversation_id, timestamp) VALUES (?,?,?,?)",
        (bob_event, "prompt", bob_conv, STARTED),
    )
    sc.commit()
    sc.close()

    receive_database(source, target, user_id="bob", push_id="p2")

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    try:
        # Bob's conversation + its event landed.
        assert conn.execute(
            "SELECT 1 FROM conversations WHERE id=?", (bob_conv,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM events WHERE id=? AND conversation_id=?", (bob_event, bob_conv)
        ).fetchone() is not None
        # Owned by Bob; Alice's conversation untouched.
        assert conn.execute(
            "SELECT user_id FROM conversation_owners WHERE conversation_id=?", (bob_conv,)
        ).fetchone()["user_id"] == "bob"
        assert conn.execute(
            "SELECT user_id FROM conversation_owners WHERE conversation_id=?", (ALICE_CONV,)
        ).fetchone()["user_id"] == "alice"
    finally:
        conn.close()


def test_single_tenant_merge_is_unrestricted(tmp_path):
    """No user_id (SSH / single-tenant): the merge behaves exactly as before —
    a newer-ULID re-push of the same natural key replaces the prior version."""
    target = tmp_path / "target.db"
    tc = create_database(target)
    th = get_or_create_harness(tc, "h", source="t", log_format="jsonl")
    old_conv = "01AAAAAAAAAAAAAAAAAAAAAAAAA"
    tc.execute(
        "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?,?,?,?)",
        (old_conv, "conv-A", th, STARTED),
    )
    tc.commit()
    tc.close()
    clear_vocabulary_caches()

    source = tmp_path / "source.db"
    sc = create_database(source)
    sh = get_or_create_harness(sc, "h", source="t", log_format="jsonl")
    new_conv = "01ZZZZZZZZZZZZZZZZZZZZZZZZZ"  # newer ULID, same natural key
    sc.execute(
        "INSERT INTO conversations (id, external_id, harness_id, started_at) VALUES (?,?,?,?)",
        (new_conv, "conv-A", sh, STARTED),
    )
    sc.commit()
    sc.close()

    receive_database(source, target)  # no user_id → unrestricted

    conn = sqlite3.connect(str(target))
    try:
        conv_ids = [r[0] for r in conn.execute("SELECT id FROM conversations").fetchall()]
        assert conv_ids == [new_conv]  # newer version replaced the older
    finally:
        conn.close()
