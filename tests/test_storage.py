"""Tests for siftd.storage.sqlite — core database layer."""

import sqlite3

import pytest
from conftest import make_conversation, make_db

from siftd.storage.sqlite import (
    backup_database,
    check_file_ingested,
    clear_ingested_file_error,
    compute_file_hash,
    create_database,
    create_empty_database,
    delete_conversation,
    ensure_canonical_tools,
    ensure_tool_aliases,
    find_conversation_by_external_id,
    get_harness_id_by_name,
    get_ingested_file_info,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_provider,
    get_or_create_tool,
    get_or_create_tool_by_alias,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_prompt_content,
    insert_response,
    insert_response_attribute,
    insert_response_content,
    insert_tool_call,
    open_database,
    record_empty_file,
    record_failed_file,
    record_ingested_file,
    store_conversation,
    update_file_stat,
)


# ---------------------------------------------------------------------------
# Connection and database creation
# ---------------------------------------------------------------------------


class TestOpenDatabase:
    def test_creates_new_database(self, tmp_path):
        db = tmp_path / "new.db"
        conn = open_database(db)
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        assert "conversations" in tables
        assert "prompts" in tables
        assert "responses" in tables

    def test_opens_existing_database(self, tmp_path):
        db = tmp_path / "test.db"
        conn1 = open_database(db)
        conn1.close()
        conn2 = open_database(db)
        # Should open without error
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        conn2.close()
        assert version >= 1

    def test_read_only_mode(self, tmp_path):
        db = tmp_path / "test.db"
        conn = create_database(db)
        conn.close()
        ro_conn = open_database(db, read_only=True)
        # Should be able to read
        ro_conn.execute("SELECT COUNT(*) FROM conversations")
        ro_conn.close()

    def test_read_only_missing_db_raises(self, tmp_path):
        db = tmp_path / "missing.db"
        with pytest.raises(FileNotFoundError):
            open_database(db, read_only=True)

    def test_row_factory_is_set(self, tmp_path):
        db = tmp_path / "test.db"
        conn = open_database(db)
        assert conn.row_factory == sqlite3.Row
        conn.close()

    def test_foreign_keys_enabled(self, tmp_path):
        db = tmp_path / "test.db"
        conn = open_database(db)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.close()
        assert fk == 1


class TestCreateEmptyDatabase:
    def test_creates_schema(self, tmp_path):
        db = tmp_path / "empty.db"
        create_empty_database(db)
        conn = sqlite3.connect(str(db))
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        assert "conversations" in tables

    def test_sets_schema_version(self, tmp_path):
        db = tmp_path / "empty.db"
        create_empty_database(db)
        conn = sqlite3.connect(str(db))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version >= 1


class TestBackupDatabase:
    def test_backup_creates_copy(self, tmp_path):
        source = tmp_path / "source.db"
        make_db(source, conversations=[{"external_id": "c1"}])
        target = tmp_path / "backup.db"
        backup_database(source, target)
        assert target.exists()
        conn = sqlite3.connect(str(target))
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 1

    def test_backup_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            backup_database(tmp_path / "missing.db", tmp_path / "backup.db")

    def test_backup_creates_parent_dirs(self, tmp_path):
        source = tmp_path / "source.db"
        make_db(source, conversations=[{"external_id": "c1"}])
        target = tmp_path / "sub" / "dir" / "backup.db"
        backup_database(source, target)
        assert target.exists()


# ---------------------------------------------------------------------------
# Vocabulary get-or-create functions
# ---------------------------------------------------------------------------


class TestGetOrCreateHarness:
    def test_creates_new(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        id1 = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
        assert id1
        row = conn.execute("SELECT name FROM harnesses WHERE id = ?", (id1,)).fetchone()
        assert row["name"] == "test_harness"
        conn.close()

    def test_returns_existing(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        id1 = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
        id2 = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
        assert id1 == id2
        conn.close()


class TestGetOrCreateModel:
    def test_creates_with_parsed_fields(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        model_id = get_or_create_model(conn, "claude-3-opus-20240229")
        row = conn.execute("SELECT raw_name, creator FROM models WHERE id = ?", (model_id,)).fetchone()
        assert row["raw_name"] == "claude-3-opus-20240229"
        assert row["creator"] == "anthropic"
        conn.close()

    def test_returns_existing(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        id1 = get_or_create_model(conn, "gpt-4")
        id2 = get_or_create_model(conn, "gpt-4")
        assert id1 == id2
        conn.close()


class TestGetOrCreateProvider:
    def test_idempotent(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        id1 = get_or_create_provider(conn, "anthropic")
        id2 = get_or_create_provider(conn, "anthropic")
        assert id1 == id2
        conn.close()


class TestGetOrCreateTool:
    def test_idempotent(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        id1 = get_or_create_tool(conn, "file.read")
        id2 = get_or_create_tool(conn, "file.read")
        assert id1 == id2
        conn.close()


class TestGetOrCreateToolByAlias:
    def test_creates_tool_and_alias(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        tool_id = get_or_create_tool_by_alias(conn, "Read", harness_id)
        assert tool_id
        # Second call returns same tool
        tool_id2 = get_or_create_tool_by_alias(conn, "Read", harness_id)
        assert tool_id == tool_id2
        conn.close()

    def test_maps_to_existing_tool(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        harness_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        # Create canonical tool first
        canonical_id = get_or_create_tool(conn, "file.read")
        # Ensure alias mapping
        ensure_tool_aliases(conn, harness_id, {"Read": "file.read"})
        # Now alias lookup should return the canonical tool
        result_id = get_or_create_tool_by_alias(conn, "Read", harness_id)
        assert result_id == canonical_id
        conn.close()


class TestEnsureCanonicalTools:
    def test_idempotent(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        ensure_canonical_tools(conn)
        count1 = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
        ensure_canonical_tools(conn)
        count2 = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
        assert count1 == count2
        assert count1 > 0
        conn.close()


# ---------------------------------------------------------------------------
# Insert operations
# ---------------------------------------------------------------------------


class TestInsertOperations:
    @pytest.fixture()
    def db_conn(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        yield conn
        conn.close()

    def test_insert_conversation(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        assert conv_id
        row = db_conn.execute("SELECT external_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        assert row["external_id"] == "ext-1"

    def test_insert_conversation_with_branch(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(
            db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z", branch="main"
        )
        row = db_conn.execute("SELECT branch FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        assert row["branch"] == "main"

    def test_insert_prompt(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(db_conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        assert prompt_id
        row = db_conn.execute("SELECT conversation_id FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        assert row["conversation_id"] == conv_id

    def test_insert_response(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        m_id = get_or_create_model(db_conn, "test-model")
        conv_id = insert_conversation(db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(db_conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        resp_id = insert_response(
            db_conn, conv_id, prompt_id, m_id, None, "r1", "2024-01-01T00:00:01Z",
            input_tokens=100, output_tokens=50,
        )
        assert resp_id
        row = db_conn.execute("SELECT input_tokens, output_tokens FROM responses WHERE id = ?", (resp_id,)).fetchone()
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50

    def test_insert_prompt_content(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(db_conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        pc_id = insert_prompt_content(db_conn, prompt_id, 0, "text", '{"text": "hello"}')
        assert pc_id

    def test_insert_response_content(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        m_id = get_or_create_model(db_conn, "test-model")
        conv_id = insert_conversation(db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(db_conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        resp_id = insert_response(db_conn, conv_id, prompt_id, m_id, None, "r1", "2024-01-01T00:00:01Z")
        rc_id = insert_response_content(db_conn, resp_id, 0, "text", '{"text": "response"}')
        assert rc_id

    def test_insert_response_attribute_upserts(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        m_id = get_or_create_model(db_conn, "test-model")
        conv_id = insert_conversation(db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(db_conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        resp_id = insert_response(db_conn, conv_id, prompt_id, m_id, None, "r1", "2024-01-01T00:00:01Z")
        # Provide explicit scope so UNIQUE(response_id, key, scope) triggers upsert
        # (NULL scope columns don't match in SQLite unique constraints)
        insert_response_attribute(db_conn, resp_id, "cache_read", "100", scope="anthropic")
        insert_response_attribute(db_conn, resp_id, "cache_read", "200", scope="anthropic")
        count = db_conn.execute(
            "SELECT COUNT(*) FROM response_attributes WHERE response_id = ? AND key = 'cache_read'",
            (resp_id,),
        ).fetchone()[0]
        assert count == 1
        val = db_conn.execute(
            "SELECT value FROM response_attributes WHERE response_id = ? AND key = 'cache_read'",
            (resp_id,),
        ).fetchone()["value"]
        assert val == "200"

    def test_insert_tool_call_with_blob_dedup(self, db_conn):
        h_id = get_or_create_harness(db_conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(db_conn, "/test", "2024-01-01T00:00:00Z")
        m_id = get_or_create_model(db_conn, "test-model")
        t_id = get_or_create_tool(db_conn, "shell.execute")
        conv_id = insert_conversation(db_conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(db_conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        resp_id = insert_response(db_conn, conv_id, prompt_id, m_id, None, "r1", "2024-01-01T00:00:01Z")
        tc_id = insert_tool_call(
            db_conn, resp_id, conv_id, t_id, "tc1",
            '{"command": "ls"}', '{"output": "file.txt"}', "success", "2024-01-01T00:00:01Z",
        )
        assert tc_id
        # Result should be stored via content_blobs (result_hash set)
        row = db_conn.execute("SELECT result_hash FROM tool_calls WHERE id = ?", (tc_id,)).fetchone()
        assert row["result_hash"] is not None


# ---------------------------------------------------------------------------
# Lookup and delete
# ---------------------------------------------------------------------------


class TestLookupAndDelete:
    def test_find_conversation_by_external_id(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        conn.commit()

        result = find_conversation_by_external_id(conn, h_id, "ext-1")
        assert result is not None
        assert result["id"] == conv_id

        assert find_conversation_by_external_id(conn, h_id, "nonexistent") is None
        conn.close()

    def test_get_harness_id_by_name(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "my_harness", source="test", log_format="jsonl")
        assert get_harness_id_by_name(conn, "my_harness") == h_id
        assert get_harness_id_by_name(conn, "nonexistent") is None
        conn.close()

    def test_delete_conversation_cascades(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        m_id = get_or_create_model(conn, "test-model")
        conv_id = insert_conversation(conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-01T00:00:00Z")
        insert_prompt_content(conn, prompt_id, 0, "text", '{"text": "hi"}')
        resp_id = insert_response(conn, conv_id, prompt_id, m_id, None, "r1", "2024-01-01T00:00:01Z")
        insert_response_content(conn, resp_id, 0, "text", '{"text": "hello"}')
        conn.commit()

        delete_conversation(conn, conv_id)
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM prompt_content").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM response_content").fetchone()[0] == 0
        conn.close()


# ---------------------------------------------------------------------------
# File deduplication
# ---------------------------------------------------------------------------


class TestFileDeduplication:
    def test_compute_file_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h = compute_file_hash(f)
        assert len(h) == 64  # SHA-256 hex

    def test_check_file_ingested(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        conn.commit()

        assert not check_file_ingested(conn, "/path/to/file.jsonl")
        record_ingested_file(conn, "/path/to/file.jsonl", "abc123", conv_id, commit=True)
        assert check_file_ingested(conn, "/path/to/file.jsonl")
        conn.close()

    def test_get_ingested_file_info(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        record_ingested_file(
            conn, "/path/to/file.jsonl", "abc123", conv_id,
            file_mtime=1000.0, file_size=500, commit=True,
        )

        info = get_ingested_file_info(conn, "/path/to/file.jsonl")
        assert info is not None
        assert info["file_hash"] == "abc123"
        assert info["conversation_id"] == conv_id
        assert info["file_mtime"] == 1000.0
        assert info["file_size"] == 500
        assert info["error"] is None

        assert get_ingested_file_info(conn, "/nonexistent") is None
        conn.close()

    def test_record_empty_file(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        rec_id = record_empty_file(conn, "/path/empty.jsonl", "hash1", h_id, commit=True)
        assert rec_id
        info = get_ingested_file_info(conn, "/path/empty.jsonl")
        assert info["conversation_id"] is None
        assert info["error"] is None
        conn.close()

    def test_record_failed_file(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        rec_id = record_failed_file(conn, "/path/bad.jsonl", "hash2", h_id, "parse error", commit=True)
        assert rec_id
        info = get_ingested_file_info(conn, "/path/bad.jsonl")
        assert info["conversation_id"] is None
        assert info["error"] == "parse error"
        conn.close()

    def test_clear_ingested_file_error(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        record_failed_file(conn, "/path/bad.jsonl", "hash2", h_id, "parse error", commit=True)
        clear_ingested_file_error(conn, "/path/bad.jsonl")
        conn.commit()
        assert not check_file_ingested(conn, "/path/bad.jsonl")
        conn.close()

    def test_update_file_stat(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        h_id = get_or_create_harness(conn, "test", source="test", log_format="jsonl")
        w_id = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        conv_id = insert_conversation(conn, "ext-1", h_id, w_id, "2024-01-01T00:00:00Z")
        record_ingested_file(conn, "/path/file.jsonl", "hash1", conv_id, commit=True)

        update_file_stat(conn, "/path/file.jsonl", 2000.0, 1024)
        conn.commit()

        info = get_ingested_file_info(conn, "/path/file.jsonl")
        assert info["file_mtime"] == 2000.0
        assert info["file_size"] == 1024
        conn.close()


# ---------------------------------------------------------------------------
# High-level store_conversation
# ---------------------------------------------------------------------------


class TestStoreConversation:
    def test_stores_full_conversation(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        conv = make_conversation(
            external_id="test-store-1",
            workspace_path="/test/project",
            prompt_text="What is Python?",
            response_text="A programming language.",
            input_tokens=100,
            output_tokens=50,
        )
        conv_id = store_conversation(conn, conv, commit=True)
        assert conv_id

        # Verify conversation
        row = conn.execute("SELECT external_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        assert row["external_id"] == "test-store-1"

        # Verify prompt content
        prompt_content = conn.execute(
            """SELECT pc.content FROM prompt_content pc
               JOIN prompts p ON p.id = pc.prompt_id
               WHERE p.conversation_id = ?""",
            (conv_id,),
        ).fetchone()
        assert "What is Python?" in prompt_content["content"]

        # Verify response content
        response_content = conn.execute(
            """SELECT rc.content FROM response_content rc
               JOIN responses r ON r.id = rc.response_id
               WHERE r.conversation_id = ?""",
            (conv_id,),
        ).fetchone()
        assert "programming language" in response_content["content"]

        # Verify token counts
        usage = conn.execute(
            "SELECT input_tokens, output_tokens FROM responses WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        conn.close()

    def test_stores_with_tool_calls(self, tmp_path):
        from siftd.domain.models import ToolCall

        conn = create_database(tmp_path / "test.db")
        tc = ToolCall(
            external_id="tc1",
            tool_name="shell.execute",
            input={"command": "ls"},
            result={"output": "ok"},
            status="success",
            timestamp="2024-01-01T10:00:01Z",
        )
        conv = make_conversation(tool_calls=[tc])
        conv_id = store_conversation(conn, conv, commit=True)

        tc_count = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE conversation_id = ?", (conv_id,),
        ).fetchone()[0]
        assert tc_count == 1
        conn.close()

    def test_commit_false_does_not_commit(self, tmp_path):
        conn = create_database(tmp_path / "test.db")
        conv = make_conversation(external_id="no-commit")
        store_conversation(conn, conv, commit=False)
        # Rollback and verify nothing persisted
        conn.rollback()
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        assert count == 0
        conn.close()
