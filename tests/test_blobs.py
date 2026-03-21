"""Tests for content-addressable blob storage."""

import pytest

from siftd.storage import (
    compute_content_hash,
    get_content,
    get_ref_count,
    open_database,
    release_content,
    store_content,
)
from siftd.storage.sqlite import (
    delete_conversation,
    get_or_create_harness,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_response,
    insert_tool_call,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "test.db")
    yield conn
    conn.close()


def _scaffold(conn):
    """Create minimal conversation scaffold, return (conv_id, response_id)."""
    h = get_or_create_harness(conn, "test", source="test")
    w = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
    c = insert_conversation(conn, "c1", h, w, "2024-01-01T10:00:00Z")
    p = insert_prompt(conn, c, "p1", "2024-01-01T10:00:00Z")
    r = insert_response(conn, c, p, None, None, "r1", "2024-01-01T10:00:01Z")
    return c, r


class TestBlobStorage:
    def test_store_returns_hash(self, db):
        h = store_content(db, "Hello, world!", commit=True)
        assert h == compute_content_hash("Hello, world!")

    def test_get_retrieves_stored(self, db):
        h = store_content(db, "Test content", commit=True)
        assert get_content(db, h) == "Test content"

    def test_get_returns_none_for_unknown(self, db):
        assert get_content(db, "nonexistent") is None

    def test_same_content_same_hash(self, db):
        assert store_content(db, "deterministic") == store_content(db, "deterministic")

    def test_different_content_different_hash(self, db):
        assert store_content(db, "A") != store_content(db, "B")


class TestDeduplication:
    def test_duplicate_increments_ref_count(self, db):
        h = store_content(db, "Dup", commit=True)
        assert get_ref_count(db, h) == 1
        store_content(db, "Dup", commit=True)
        assert get_ref_count(db, h) == 2

    def test_duplicate_single_blob(self, db):
        for _ in range(5):
            store_content(db, "multi")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 1
        assert get_ref_count(db, compute_content_hash("multi")) == 5

    def test_different_content_separate_blobs(self, db):
        for s in ("A", "B", "C"):
            store_content(db, s)
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 3


class TestRefCounting:
    def test_release_decrements(self, db):
        h = store_content(db, "rel")
        store_content(db, "rel")
        db.commit()
        release_content(db, h, commit=True)
        assert get_ref_count(db, h) == 1

    def test_release_deletes_at_zero(self, db):
        h = store_content(db, "gone", commit=True)
        release_content(db, h, commit=True)
        assert get_content(db, h) is None
        assert get_ref_count(db, h) == 0

    def test_release_preserves_with_refs(self, db):
        h = store_content(db, "keep")
        store_content(db, "keep")
        store_content(db, "keep")
        db.commit()
        release_content(db, h, commit=True)
        assert get_ref_count(db, h) == 2
        assert get_content(db, h) == "keep"

    def test_ref_count_zero_for_nonexistent(self, db):
        assert get_ref_count(db, "nope") == 0


class TestEdgeCases:
    def test_empty_string(self, db):
        h = store_content(db, "", commit=True)
        assert get_content(db, h) == ""

    def test_large_content(self, db):
        big = "x" * (1024 * 1024)
        h = store_content(db, big, commit=True)
        assert get_content(db, h) == big

    def test_unicode(self, db):
        s = "Hello 世界 🌍 émojis"
        h = store_content(db, s, commit=True)
        assert get_content(db, h) == s

    def test_json_content(self, db):
        s = '{"file_path": "/test/file.py", "content": "def foo():\\n    pass"}'
        h = store_content(db, s, commit=True)
        assert get_content(db, h) == s


class TestToolCallIntegration:
    def test_dedupes_result(self, db):
        c, r = _scaffold(db)
        insert_tool_call(db, r, c, None, "tc1", '{"file_path": "/t.py"}',
                         '{"content": "file"}', "success", "2024-01-01T10:00:01Z")
        db.commit()
        row = db.execute("SELECT result, result_hash FROM tool_calls WHERE external_id='tc1'").fetchone()
        assert row["result"] is None
        assert row["result_hash"] is not None
        assert get_content(db, row["result_hash"]) == '{"content": "file"}'

    def test_dedupe_disabled(self, db):
        c, r = _scaffold(db)
        insert_tool_call(db, r, c, None, "tc1", '{}', '{"inline": true}',
                         "success", "2024-01-01T10:00:01Z", dedupe_result=False)
        db.commit()
        row = db.execute("SELECT result, result_hash FROM tool_calls WHERE external_id='tc1'").fetchone()
        assert row["result"] == '{"inline": true}'
        assert row["result_hash"] is None

    def test_duplicate_results_share_blob(self, db):
        c, r = _scaffold(db)
        result = '{"content": "same"}'
        for i in range(3):
            insert_tool_call(db, r, c, None, f"tc{i}", '{}', result,
                             "success", f"2024-01-01T10:00:0{i}Z")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 1
        h = db.execute("SELECT result_hash FROM tool_calls LIMIT 1").fetchone()["result_hash"]
        assert get_ref_count(db, h) == 3

    def test_null_result_no_blob(self, db):
        c, r = _scaffold(db)
        insert_tool_call(db, r, c, None, "tc1", '{}', None, "success", "2024-01-01T10:00:01Z")
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 0


class TestDeleteCascade:
    def test_delete_releases_blob(self, db):
        c, r = _scaffold(db)
        insert_tool_call(db, r, c, None, "tc1", '{}', '{"content": "orphan"}',
                         "success", "2024-01-01T10:00:01Z")
        db.commit()
        h = compute_content_hash('{"content": "orphan"}')
        assert get_ref_count(db, h) == 1
        delete_conversation(db, c)
        db.commit()
        assert get_ref_count(db, h) == 0

    def test_delete_preserves_shared_blob(self, db):
        harness = get_or_create_harness(db, "test", source="test")
        ws = get_or_create_workspace(db, "/test", "2024-01-01T10:00:00Z")
        result = '{"shared": true}'
        ids = []
        for i in range(2):
            c = insert_conversation(db, f"c{i}", harness, ws, f"2024-01-0{i+1}T10:00:00Z")
            p = insert_prompt(db, c, f"p{i}", f"2024-01-0{i+1}T10:00:00Z")
            r = insert_response(db, c, p, None, None, f"r{i}", f"2024-01-0{i+1}T10:00:01Z")
            insert_tool_call(db, r, c, None, f"tc{i}", '{}', result, "success", f"2024-01-0{i+1}T10:00:01Z")
            ids.append(c)
        db.commit()
        h = compute_content_hash(result)
        assert get_ref_count(db, h) == 2
        delete_conversation(db, ids[0])
        db.commit()
        assert get_ref_count(db, h) == 1
        delete_conversation(db, ids[1])
        db.commit()
        assert get_ref_count(db, h) == 0

    def test_delete_multi_refs_same_blob(self, db):
        c, r = _scaffold(db)
        result = '{"repeated": true}'
        for i in range(3):
            insert_tool_call(db, r, c, None, f"tc{i}", '{}', result,
                             "success", f"2024-01-01T10:00:0{i}Z")
        db.commit()
        h = compute_content_hash(result)
        assert get_ref_count(db, h) == 3
        delete_conversation(db, c)
        db.commit()
        assert get_ref_count(db, h) == 0


class TestMigration:
    def test_count_pending(self, db):
        from siftd.storage.migrate_blobs import count_pending_migrations
        c, r = _scaffold(db)
        for i in range(3):
            insert_tool_call(db, r, c, None, f"tc{i}", '{}', f'{{"n": {i}}}',
                             "success", None, dedupe_result=False)
        db.commit()
        stats = count_pending_migrations(db)
        assert stats["total"] == 3
        assert stats["unique"] == 3

    def test_migrate_results(self, db):
        from siftd.storage.migrate_blobs import migrate_existing_results, verify_migration
        c, r = _scaffold(db)
        insert_tool_call(db, r, c, None, "tc1", '{}', '{"a":1}', "s", None, dedupe_result=False)
        insert_tool_call(db, r, c, None, "tc2", '{}', '{"a":1}', "s", None, dedupe_result=False)
        insert_tool_call(db, r, c, None, "tc3", '{}', '{"b":2}', "s", None, dedupe_result=False)
        db.commit()
        stats = migrate_existing_results(db)
        assert stats["migrated"] == 3
        assert stats["blobs_created"] == 2
        v = verify_migration(db)
        assert v["pending"] == 0

    def test_migrate_with_progress(self, db):
        from siftd.storage.migrate_blobs import migrate_existing_results
        c, r = _scaffold(db)
        for i in range(5):
            insert_tool_call(db, r, c, None, f"tc{i}", '{}', f'{{"n":{i}}}',
                             "s", None, dedupe_result=False)
        db.commit()
        calls = []
        migrate_existing_results(db, batch_size=2, on_progress=lambda p, t: calls.append((p, t)))
        assert calls[-1] == (5, 5)

    def test_migrate_empty(self, db):
        from siftd.storage.migrate_blobs import migrate_existing_results
        assert migrate_existing_results(db)["migrated"] == 0

    def test_migrate_preserves_existing(self, db):
        from siftd.storage.migrate_blobs import migrate_existing_results
        c, r = _scaffold(db)
        shared = '{"shared": true}'
        insert_tool_call(db, r, c, None, "tc_new", '{}', shared, "s", None, dedupe_result=True)
        insert_tool_call(db, r, c, None, "tc_old", '{}', shared, "s", None, dedupe_result=False)
        db.commit()
        stats = migrate_existing_results(db)
        assert stats["migrated"] == 1
        assert stats["blobs_reused"] == 1
        assert get_ref_count(db, compute_content_hash(shared)) == 2
