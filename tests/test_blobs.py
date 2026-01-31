"""Tests for content-addressable blob storage."""


from siftd.storage import (
    compute_content_hash,
    get_content,
    get_ref_count,
    open_database,
    release_content,
    store_content,
)


class TestBlobStorage:
    """Unit tests for blob storage functions."""

    def test_store_content_returns_hash(self, tmp_path):
        """store_content returns SHA256 hash of content."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Hello, world!"
        result_hash = store_content(conn, content, commit=True)

        assert result_hash == compute_content_hash(content)
        conn.close()

    def test_get_content_retrieves_stored(self, tmp_path):
        """get_content retrieves previously stored content."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Test content for retrieval"
        content_hash = store_content(conn, content, commit=True)

        retrieved = get_content(conn, content_hash)

        assert retrieved == content
        conn.close()

    def test_get_content_returns_none_for_unknown(self, tmp_path):
        """get_content returns None for unknown hash."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        result = get_content(conn, "nonexistent_hash")

        assert result is None
        conn.close()

    def test_same_content_same_hash(self, tmp_path):
        """Same content always produces same hash."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Deterministic content"
        hash1 = store_content(conn, content)
        hash2 = store_content(conn, content)

        assert hash1 == hash2
        conn.close()

    def test_different_content_different_hash(self, tmp_path):
        """Different content produces different hashes."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        hash1 = store_content(conn, "Content A")
        hash2 = store_content(conn, "Content B")

        assert hash1 != hash2
        conn.close()


class TestDeduplication:
    """Tests for deduplication behavior."""

    def test_duplicate_storage_increments_ref_count(self, tmp_path):
        """Storing same content twice increments ref_count."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Duplicate me"
        content_hash = store_content(conn, content, commit=True)

        assert get_ref_count(conn, content_hash) == 1

        store_content(conn, content, commit=True)

        assert get_ref_count(conn, content_hash) == 2
        conn.close()

    def test_duplicate_storage_single_blob(self, tmp_path):
        """Storing same content multiple times creates single blob."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Store me many times"
        for _ in range(5):
            store_content(conn, content)
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM content_blobs")
        assert cur.fetchone()[0] == 1

        content_hash = compute_content_hash(content)
        assert get_ref_count(conn, content_hash) == 5
        conn.close()

    def test_different_content_separate_blobs(self, tmp_path):
        """Different content creates separate blobs."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        store_content(conn, "Content A")
        store_content(conn, "Content B")
        store_content(conn, "Content C")
        conn.commit()

        cur = conn.execute("SELECT COUNT(*) FROM content_blobs")
        assert cur.fetchone()[0] == 3
        conn.close()


class TestRefCounting:
    """Tests for reference counting and cleanup."""

    def test_release_decrements_ref_count(self, tmp_path):
        """release_content decrements ref_count."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Release me"
        content_hash = store_content(conn, content)
        store_content(conn, content)  # ref_count = 2
        conn.commit()

        assert get_ref_count(conn, content_hash) == 2

        release_content(conn, content_hash, commit=True)

        assert get_ref_count(conn, content_hash) == 1
        conn.close()

    def test_release_deletes_at_zero(self, tmp_path):
        """release_content deletes blob when ref_count reaches 0."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Delete me when done"
        content_hash = store_content(conn, content, commit=True)

        assert get_content(conn, content_hash) == content

        release_content(conn, content_hash, commit=True)

        assert get_content(conn, content_hash) is None
        assert get_ref_count(conn, content_hash) == 0
        conn.close()

    def test_release_preserves_with_refs_remaining(self, tmp_path):
        """release_content preserves blob while refs remain."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Multiple refs"
        content_hash = store_content(conn, content)
        store_content(conn, content)
        store_content(conn, content)  # ref_count = 3
        conn.commit()

        release_content(conn, content_hash, commit=True)

        assert get_ref_count(conn, content_hash) == 2
        assert get_content(conn, content_hash) == content
        conn.close()

    def test_get_ref_count_zero_for_nonexistent(self, tmp_path):
        """get_ref_count returns 0 for nonexistent blob."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        assert get_ref_count(conn, "nonexistent_hash") == 0
        conn.close()


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_string_content(self, tmp_path):
        """Empty string is valid content."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content_hash = store_content(conn, "", commit=True)
        retrieved = get_content(conn, content_hash)

        assert retrieved == ""
        conn.close()

    def test_large_content(self, tmp_path):
        """Large content is stored and retrieved correctly."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        # ~1MB of content
        large_content = "x" * (1024 * 1024)
        content_hash = store_content(conn, large_content, commit=True)
        retrieved = get_content(conn, content_hash)

        assert retrieved == large_content
        conn.close()

    def test_unicode_content(self, tmp_path):
        """Unicode content is handled correctly."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = "Hello 世界 🌍 émojis"
        content_hash = store_content(conn, content, commit=True)
        retrieved = get_content(conn, content_hash)

        assert retrieved == content
        conn.close()

    def test_json_content(self, tmp_path):
        """JSON content (typical tool_calls.result) works correctly."""
        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        content = '{"file_path": "/test/file.py", "content": "def foo():\\n    pass"}'
        content_hash = store_content(conn, content, commit=True)
        retrieved = get_content(conn, content_hash)

        assert retrieved == content
        conn.close()


class TestToolCallIntegration:
    """Integration tests for tool_calls with blob storage."""

    def test_insert_tool_call_dedupes_result(self, tmp_path):
        """insert_tool_call stores result in content_blobs by default."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        result_json = '{"content": "file contents here"}'
        insert_tool_call(
            conn, response_id, conv_id, None, "tc1",
            '{"file_path": "/test.py"}', result_json, "success", "2024-01-01T10:00:01Z",
        )
        conn.commit()

        # Verify result_hash is set, result is NULL
        cur = conn.execute("SELECT result, result_hash FROM tool_calls WHERE external_id = 'tc1'")
        row = cur.fetchone()
        assert row["result"] is None
        assert row["result_hash"] is not None

        # Verify content is in blob storage
        retrieved = get_content(conn, row["result_hash"])
        assert retrieved == result_json
        conn.close()

    def test_insert_tool_call_dedupe_disabled(self, tmp_path):
        """insert_tool_call stores inline when dedupe_result=False."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        result_json = '{"content": "inline content"}'
        insert_tool_call(
            conn, response_id, conv_id, None, "tc1",
            '{"file_path": "/test.py"}', result_json, "success", "2024-01-01T10:00:01Z",
            dedupe_result=False,
        )
        conn.commit()

        # Verify result is inline, result_hash is NULL
        cur = conn.execute("SELECT result, result_hash FROM tool_calls WHERE external_id = 'tc1'")
        row = cur.fetchone()
        assert row["result"] == result_json
        assert row["result_hash"] is None
        conn.close()

    def test_duplicate_results_share_blob(self, tmp_path):
        """Multiple tool_calls with same result share one blob."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        # Same result content for multiple tool calls
        result_json = '{"content": "same file contents"}'
        insert_tool_call(
            conn, response_id, conv_id, None, "tc1",
            '{}', result_json, "success", "2024-01-01T10:00:01Z",
        )
        insert_tool_call(
            conn, response_id, conv_id, None, "tc2",
            '{}', result_json, "success", "2024-01-01T10:00:02Z",
        )
        insert_tool_call(
            conn, response_id, conv_id, None, "tc3",
            '{}', result_json, "success", "2024-01-01T10:00:03Z",
        )
        conn.commit()

        # Verify single blob with ref_count=3
        cur = conn.execute("SELECT COUNT(*) FROM content_blobs")
        assert cur.fetchone()[0] == 1

        cur = conn.execute("SELECT result_hash FROM tool_calls LIMIT 1")
        content_hash = cur.fetchone()["result_hash"]
        assert get_ref_count(conn, content_hash) == 3
        conn.close()

    def test_null_result_no_blob(self, tmp_path):
        """Tool call with NULL result doesn't create blob."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        insert_tool_call(
            conn, response_id, conv_id, None, "tc1",
            '{"file_path": "/test.py"}', None, "success", "2024-01-01T10:00:01Z",
        )
        conn.commit()

        # Verify no blob created
        cur = conn.execute("SELECT COUNT(*) FROM content_blobs")
        assert cur.fetchone()[0] == 0

        # Verify result_hash is NULL
        cur = conn.execute("SELECT result_hash FROM tool_calls WHERE external_id = 'tc1'")
        assert cur.fetchone()["result_hash"] is None
        conn.close()


class TestDeleteCascade:
    """Tests for delete cascade with blob cleanup."""

    def test_delete_conversation_releases_blob(self, tmp_path):
        """Deleting conversation decrements blob ref_count via trigger."""
        from siftd.storage.sqlite import (
            delete_conversation,
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        result_json = '{"content": "will be orphaned"}'
        insert_tool_call(
            conn, response_id, conv_id, None, "tc1",
            '{}', result_json, "success", "2024-01-01T10:00:01Z",
        )
        conn.commit()

        content_hash = compute_content_hash(result_json)
        assert get_ref_count(conn, content_hash) == 1

        delete_conversation(conn, conv_id)
        conn.commit()

        # Blob should be deleted (ref_count was 1, now 0)
        assert get_ref_count(conn, content_hash) == 0
        assert get_content(conn, content_hash) is None
        conn.close()

    def test_delete_preserves_shared_blob(self, tmp_path):
        """Deleting conversation preserves blob if other refs exist."""
        from siftd.storage.sqlite import (
            delete_conversation,
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")

        # Two conversations referencing same content
        result_json = '{"content": "shared content"}'

        conv1_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt1_id = insert_prompt(conn, conv1_id, "p1", "2024-01-01T10:00:00Z")
        response1_id = insert_response(conn, conv1_id, prompt1_id, None, None, "r1", "2024-01-01T10:00:01Z")
        insert_tool_call(conn, response1_id, conv1_id, None, "tc1", '{}', result_json, "success", "2024-01-01T10:00:01Z")

        conv2_id = insert_conversation(conn, "c2", harness_id, workspace_id, "2024-01-02T10:00:00Z")
        prompt2_id = insert_prompt(conn, conv2_id, "p2", "2024-01-02T10:00:00Z")
        response2_id = insert_response(conn, conv2_id, prompt2_id, None, None, "r2", "2024-01-02T10:00:01Z")
        insert_tool_call(conn, response2_id, conv2_id, None, "tc2", '{}', result_json, "success", "2024-01-02T10:00:01Z")
        conn.commit()

        content_hash = compute_content_hash(result_json)
        assert get_ref_count(conn, content_hash) == 2

        # Delete first conversation
        delete_conversation(conn, conv1_id)
        conn.commit()

        # Blob should persist with ref_count=1
        assert get_ref_count(conn, content_hash) == 1
        assert get_content(conn, content_hash) == result_json

        # Delete second conversation
        delete_conversation(conn, conv2_id)
        conn.commit()

        # Now blob should be deleted
        assert get_ref_count(conn, content_hash) == 0
        assert get_content(conn, content_hash) is None
        conn.close()

    def test_delete_multiple_tool_calls_same_blob(self, tmp_path):
        """Deleting conversation with multiple refs to same blob handles correctly."""
        from siftd.storage.sqlite import (
            delete_conversation,
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        # Same result stored 3 times in same conversation
        result_json = '{"content": "repeated read"}'
        insert_tool_call(conn, response_id, conv_id, None, "tc1", '{}', result_json, "success", "2024-01-01T10:00:01Z")
        insert_tool_call(conn, response_id, conv_id, None, "tc2", '{}', result_json, "success", "2024-01-01T10:00:02Z")
        insert_tool_call(conn, response_id, conv_id, None, "tc3", '{}', result_json, "success", "2024-01-01T10:00:03Z")
        conn.commit()

        content_hash = compute_content_hash(result_json)
        assert get_ref_count(conn, content_hash) == 3

        delete_conversation(conn, conv_id)
        conn.commit()

        # All refs released, blob deleted
        assert get_ref_count(conn, content_hash) == 0
        assert get_content(conn, content_hash) is None
        conn.close()


class TestMigration:
    """Tests for migration of existing data."""

    def test_count_pending_migrations(self, tmp_path):
        """count_pending_migrations reports correct stats."""
        from siftd.storage.migrate_blobs import count_pending_migrations
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        # Insert with dedupe disabled (simulates legacy data)
        insert_tool_call(conn, response_id, conv_id, None, "tc1", '{}', '{"a": 1}', "success", None, dedupe_result=False)
        insert_tool_call(conn, response_id, conv_id, None, "tc2", '{}', '{"a": 1}', "success", None, dedupe_result=False)
        insert_tool_call(conn, response_id, conv_id, None, "tc3", '{}', '{"b": 2}', "success", None, dedupe_result=False)
        conn.commit()

        stats = count_pending_migrations(conn)

        assert stats["total"] == 3
        assert stats["unique"] == 2  # Two unique values
        assert stats["size_bytes"] > 0
        conn.close()

    def test_migrate_existing_results(self, tmp_path):
        """migrate_existing_results migrates all pending rows."""
        from siftd.storage.migrate_blobs import migrate_existing_results, verify_migration
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        # Legacy data (inline results)
        insert_tool_call(conn, response_id, conv_id, None, "tc1", '{}', '{"content": "file1"}', "success", None, dedupe_result=False)
        insert_tool_call(conn, response_id, conv_id, None, "tc2", '{}', '{"content": "file1"}', "success", None, dedupe_result=False)
        insert_tool_call(conn, response_id, conv_id, None, "tc3", '{}', '{"content": "file2"}', "success", None, dedupe_result=False)
        conn.commit()

        # Migrate
        stats = migrate_existing_results(conn)

        assert stats["migrated"] == 3
        assert stats["blobs_created"] == 2  # 2 unique contents
        assert stats["blobs_reused"] == 1   # 1 duplicate
        assert stats["bytes_before"] > 0
        assert stats["bytes_after"] > 0
        assert stats["bytes_after"] < stats["bytes_before"]  # Dedup saved space

        # Verify
        verification = verify_migration(conn)
        assert verification["pending"] == 0
        assert verification["migrated"] == 3
        assert verification["orphaned_blobs"] == 0

        # Verify result is now in blob
        cur = conn.execute("SELECT result, result_hash FROM tool_calls WHERE external_id = 'tc1'")
        row = cur.fetchone()
        assert row["result"] is None
        assert row["result_hash"] is not None
        assert get_content(conn, row["result_hash"]) == '{"content": "file1"}'

        conn.close()

    def test_migrate_with_progress(self, tmp_path):
        """migrate_existing_results calls progress callback."""
        from siftd.storage.migrate_blobs import migrate_existing_results
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        for i in range(5):
            insert_tool_call(conn, response_id, conv_id, None, f"tc{i}", '{}', f'{{"n": {i}}}', "success", None, dedupe_result=False)
        conn.commit()

        progress_calls = []

        def on_progress(processed, total):
            progress_calls.append((processed, total))

        migrate_existing_results(conn, batch_size=2, on_progress=on_progress)

        assert len(progress_calls) > 0
        assert progress_calls[-1][0] == 5  # All processed
        assert progress_calls[-1][1] == 5  # Total
        conn.close()

    def test_migrate_empty_table(self, tmp_path):
        """migrate_existing_results handles empty table."""
        from siftd.storage.migrate_blobs import migrate_existing_results

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        stats = migrate_existing_results(conn)

        assert stats["migrated"] == 0
        assert stats["blobs_created"] == 0
        conn.close()

    def test_migrate_preserves_existing_blobs(self, tmp_path):
        """Migration correctly handles pre-existing blobs."""
        from siftd.storage.migrate_blobs import migrate_existing_results
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)

        harness_id = get_or_create_harness(conn, "test", source="test")
        workspace_id = get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(conn, "c1", harness_id, workspace_id, "2024-01-01T10:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T10:00:00Z")
        response_id = insert_response(conn, conv_id, prompt_id, None, None, "r1", "2024-01-01T10:00:01Z")

        shared_content = '{"shared": true}'

        # One new tool call using dedup (creates blob)
        insert_tool_call(conn, response_id, conv_id, None, "tc_new", '{}', shared_content, "success", None, dedupe_result=True)

        # One legacy tool call with same content
        insert_tool_call(conn, response_id, conv_id, None, "tc_legacy", '{}', shared_content, "success", None, dedupe_result=False)
        conn.commit()

        # Verify initial state
        cur = conn.execute("SELECT COUNT(*) FROM content_blobs")
        assert cur.fetchone()[0] == 1

        content_hash = compute_content_hash(shared_content)
        assert get_ref_count(conn, content_hash) == 1

        # Migrate
        stats = migrate_existing_results(conn)

        assert stats["migrated"] == 1
        assert stats["blobs_created"] == 0  # Reused existing
        assert stats["blobs_reused"] == 1

        # Blob ref_count should now be 2
        assert get_ref_count(conn, content_hash) == 2
        conn.close()
