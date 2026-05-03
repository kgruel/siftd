"""Tests for tr_event_tool_call_* blob reference-count triggers.

Verifies:
- DELETE from event_tool_call decrements content_blobs.ref_count
- UPDATE on event_tool_call.result_hash decrements old ref, increments new ref
- MAX(ref_count - 1, 0) clamp prevents negative ref_count
- Blob is deleted when ref_count reaches zero
"""

import pytest

from siftd.ids import ulid as _ulid
from siftd.storage import get_ref_count
from siftd.storage.blobs import store_content
from siftd.storage.events import insert_event, insert_event_tool_call
from siftd.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_workspace,
    insert_conversation,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "triggers.db"
    conn = create_database(path)
    harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
    ws_id = get_or_create_workspace(conn, "/ws", "2024-01-01T00:00:00Z")
    conv_id = insert_conversation(conn, external_id="c1", harness_id=harness_id,
                                  workspace_id=ws_id, started_at="2024-01-01T00:00:00Z")
    conn.commit()
    yield conn, conv_id
    conn.close()


def _make_tool_call(conn, conv_id, result_json):
    """Insert a prompt→response→tool_call chain and return (tc_id, result_hash)."""
    p_id = _ulid()
    insert_event(conn, p_id, "prompt", conv_id, "2024-01-01T10:00:00Z")
    r_id = _ulid()
    insert_event(conn, r_id, "response", conv_id, "2024-01-01T10:00:01Z", parent_id=p_id)
    tc_id = _ulid()
    insert_event(conn, tc_id, "tool_call", conv_id, "2024-01-01T10:00:02Z", parent_id=r_id)
    insert_event_tool_call(conn, tc_id, input_json='{}', result_json=result_json, status="success")
    row = conn.execute("SELECT result_hash FROM event_tool_call WHERE event_id = ?", (tc_id,)).fetchone()
    return tc_id, row["result_hash"]


class TestDeleteTrigger:
    def test_delete_decrements_ref_count(self, db):
        conn, conv_id = db
        tc_id, blob_hash = _make_tool_call(conn, conv_id, '{"out": "data"}')
        assert get_ref_count(conn, blob_hash) == 1

        conn.execute("DELETE FROM event_tool_call WHERE event_id = ?", (tc_id,))
        assert get_ref_count(conn, blob_hash) == 0

    def test_delete_removes_blob_at_zero(self, db):
        conn, conv_id = db
        tc_id, blob_hash = _make_tool_call(conn, conv_id, '{"out": "todelete"}')
        assert blob_hash is not None

        conn.execute("DELETE FROM event_tool_call WHERE event_id = ?", (tc_id,))
        blob = conn.execute("SELECT 1 FROM content_blobs WHERE hash = ?", (blob_hash,)).fetchone()
        assert blob is None

    def test_delete_null_result_hash_is_noop(self, db):
        conn, conv_id = db
        tc_id, blob_hash = _make_tool_call(conn, conv_id, None)
        assert blob_hash is None
        before = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]
        conn.execute("DELETE FROM event_tool_call WHERE event_id = ?", (tc_id,))
        after = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]
        assert before == after

    def test_shared_blob_not_deleted_while_referenced(self, db):
        """Two tool calls sharing a blob: only deleted after both are removed."""
        conn, conv_id = db
        result_json = '{"shared": "content"}'
        tc1_id, blob_hash = _make_tool_call(conn, conv_id, result_json)
        # Reuse the same blob by inserting another tc that stores the same content
        p2_id = _ulid()
        insert_event(conn, p2_id, "prompt", conv_id, "2024-01-01T11:00:00Z")
        r2_id = _ulid()
        insert_event(conn, r2_id, "response", conv_id, "2024-01-01T11:00:01Z", parent_id=p2_id)
        tc2_id = _ulid()
        insert_event(conn, tc2_id, "tool_call", conv_id, "2024-01-01T11:00:02Z", parent_id=r2_id)
        insert_event_tool_call(conn, tc2_id, input_json='{}', result_json=result_json, status="success")

        assert get_ref_count(conn, blob_hash) == 2

        conn.execute("DELETE FROM event_tool_call WHERE event_id = ?", (tc1_id,))
        assert get_ref_count(conn, blob_hash) == 1

        conn.execute("DELETE FROM event_tool_call WHERE event_id = ?", (tc2_id,))
        assert get_ref_count(conn, blob_hash) == 0


class TestUpdateTrigger:
    def test_update_null_to_hash_increments(self, db):
        conn, conv_id = db
        tc_id, _ = _make_tool_call(conn, conv_id, None)
        new_hash = store_content(conn, '{"new": "data"}')
        assert get_ref_count(conn, new_hash) == 1

        # Reset to 0 to isolate the trigger's increment
        conn.execute("UPDATE content_blobs SET ref_count = 0 WHERE hash = ?", (new_hash,))

        conn.execute("UPDATE event_tool_call SET result_hash = ? WHERE event_id = ?", (new_hash, tc_id))
        assert get_ref_count(conn, new_hash) == 1

    def test_update_hash_to_null_decrements(self, db):
        conn, conv_id = db
        tc_id, blob_hash = _make_tool_call(conn, conv_id, '{"out": "original"}')
        assert get_ref_count(conn, blob_hash) == 1

        conn.execute("UPDATE event_tool_call SET result_hash = NULL WHERE event_id = ?", (tc_id,))
        assert get_ref_count(conn, blob_hash) == 0

    def test_update_hash_to_new_hash_swaps_refs(self, db):
        conn, conv_id = db
        tc_id, old_hash = _make_tool_call(conn, conv_id, '{"out": "old"}')
        new_hash = store_content(conn, '{"out": "new"}')
        # Reset new blob ref_count to 0 to isolate the trigger increment
        conn.execute("UPDATE content_blobs SET ref_count = 0 WHERE hash = ?", (new_hash,))

        assert get_ref_count(conn, old_hash) == 1
        conn.execute("UPDATE event_tool_call SET result_hash = ? WHERE event_id = ?", (new_hash, tc_id))
        assert get_ref_count(conn, old_hash) == 0
        assert get_ref_count(conn, new_hash) == 1

    def test_update_clamps_ref_count_to_zero(self, db):
        """MAX(ref_count - 1, 0) prevents negative values."""
        conn, conv_id = db
        tc_id, blob_hash = _make_tool_call(conn, conv_id, '{"x": 1}')
        # Force ref_count to 0 (simulating prior decrement)
        conn.execute("UPDATE content_blobs SET ref_count = 0 WHERE hash = ?", (blob_hash,))

        conn.execute("UPDATE event_tool_call SET result_hash = NULL WHERE event_id = ?", (tc_id,))
        # ref_count should remain >= 0, not go negative; blob may have been deleted
        row = conn.execute("SELECT ref_count FROM content_blobs WHERE hash = ?", (blob_hash,)).fetchone()
        if row:
            assert row["ref_count"] >= 0
