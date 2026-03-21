"""Tests for siftd storage layer coverage.

Integration-style tests that exercise multiple storage modules through
their public APIs, maximizing coverage per test line.
"""


import pytest

from siftd.domain.models import (
    ContentBlock,
    Conversation,
    Harness,
    Prompt,
    Response,
    ToolCall,
    Usage,
)
from siftd.storage.sqlite import (
    backup_database,
    check_file_ingested,
    clear_ingested_file_error,
    compute_file_hash,
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
    get_or_create_tool_by_alias,
    open_database,
    record_empty_file,
    record_failed_file,
    record_ingested_file,
    store_conversation,
    update_file_stat,
)


@pytest.fixture
def db(tmp_path):
    """Fresh database connection."""
    conn = open_database(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def populated_db(db):
    """Database with a stored conversation including tool calls."""
    conv = Conversation(
        external_id="conv-1",
        workspace_path="/test/project",
        started_at="2024-01-01T10:00:00Z",
        ended_at="2024-01-01T11:00:00Z",
        branch="main",
        harness=Harness(name="test_harness", source="test", log_format="jsonl"),
        prompts=[
            Prompt(
                external_id="p1",
                timestamp="2024-01-01T10:00:00Z",
                content=[ContentBlock(block_type="text", content={"text": "Write a Python function"})],
                responses=[
                    Response(
                        external_id="r1",
                        timestamp="2024-01-01T10:00:01Z",
                        model="claude-3-opus-20240229",
                        usage=Usage(input_tokens=100, output_tokens=200),
                        content=[ContentBlock(block_type="text", content={"text": "Here is a function"})],
                        tool_calls=[
                            ToolCall(
                                tool_name="file.write",
                                external_id="tc1",
                                input={"file_path": "/test/file.py"},
                                result={"content": "def hello(): pass"},
                                status="success",
                                timestamp="2024-01-01T10:00:02Z",
                            ),
                            ToolCall(
                                tool_name="shell.execute",
                                external_id="tc2",
                                input={"command": "pytest"},
                                result={"output": "1 passed"},
                                status="success",
                                timestamp="2024-01-01T10:00:03Z",
                            ),
                        ],
                        attributes={"cache_read_input_tokens": "50"},
                    ),
                ],
            ),
        ],
    )
    conv_id = store_conversation(db, conv, commit=True)
    return db, conv_id


# =============================================================================
# store_conversation integration
# =============================================================================


class TestStoreConversation:
    """Tests for the high-level store_conversation function."""

    def test_stores_full_conversation(self, populated_db):
        """store_conversation persists all nested objects."""
        conn, conv_id = populated_db
        # Verify conversation
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        assert row["external_id"] == "conv-1"
        assert row["branch"] == "main"
        # Verify prompt
        prompts = conn.execute("SELECT * FROM prompts WHERE conversation_id = ?", (conv_id,)).fetchall()
        assert len(prompts) == 1
        # Verify response
        responses = conn.execute("SELECT * FROM responses WHERE conversation_id = ?", (conv_id,)).fetchall()
        assert len(responses) == 1
        assert responses[0]["input_tokens"] == 100
        # Verify tool calls
        tcs = conn.execute("SELECT * FROM tool_calls WHERE conversation_id = ?", (conv_id,)).fetchall()
        assert len(tcs) == 2

    def test_stores_fts_content(self, populated_db):
        """store_conversation indexes text in FTS."""
        conn, conv_id = populated_db
        rows = conn.execute(
            "SELECT * FROM content_fts WHERE content_fts MATCH 'Python'",
        ).fetchall()
        assert len(rows) >= 1

    def test_stores_response_attributes(self, populated_db):
        """store_conversation stores response attributes."""
        conn, conv_id = populated_db
        rows = conn.execute("SELECT * FROM response_attributes").fetchall()
        assert any(r["key"] == "cache_read_input_tokens" for r in rows)

    def test_auto_tags_shell_commands(self, populated_db):
        """store_conversation auto-tags shell.execute tool calls."""
        conn, conv_id = populated_db
        tags = conn.execute(
            "SELECT t.name FROM tool_call_tags tct JOIN tags t ON t.id = tct.tag_id"
        ).fetchall()
        assert any("shell:" in t["name"] for t in tags)

    def test_workspace_cache(self, db):
        """store_conversation uses workspace cache to avoid repeated lookups."""
        cache = {}
        for i in range(3):
            conv = Conversation(
                external_id=f"conv-{i}",
                workspace_path="/test/project",
                started_at=f"2024-01-0{i+1}T10:00:00Z",
                harness=Harness(name="test_harness", source="test"),
                prompts=[],
            )
            store_conversation(db, conv, _workspace_cache=cache)
        db.commit()
        assert "/test/project" in cache
        ws_count = db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        assert ws_count == 1

    def test_derivative_tagging(self, db):
        """store_conversation tags derivative conversations."""
        conv = Conversation(
            external_id="deriv-conv",
            workspace_path="/test/project",
            started_at="2024-01-01T10:00:00Z",
            harness=Harness(name="test_harness", source="test"),
            prompts=[
                Prompt(
                    external_id="p1",
                    timestamp="2024-01-01T10:00:00Z",
                    content=[ContentBlock(block_type="text", content={"text": "search"})],
                    responses=[
                        Response(
                            external_id="r1",
                            timestamp="2024-01-01T10:00:01Z",
                            model="test-model",
                            content=[],
                            tool_calls=[
                                ToolCall(
                                    tool_name="shell.execute",
                                    external_id="tc1",
                                    input={"command": "siftd search foo"},
                                    result={"output": "found"},
                                    status="success",
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        conv_id = store_conversation(db, conv, commit=True)
        tags = db.execute(
            "SELECT t.name FROM conversation_tags ct JOIN tags t ON t.id = ct.tag_id WHERE ct.conversation_id = ?",
            (conv_id,),
        ).fetchall()
        assert any("derivative" in t["name"] for t in tags)


# =============================================================================
# Vocabulary entities
# =============================================================================


class TestVocabulary:
    """Tests for get-or-create vocabulary functions."""

    def test_harness_caching(self, db):
        """get_or_create_harness returns same ID for same name."""
        id1 = get_or_create_harness(db, "test", source="local")
        id2 = get_or_create_harness(db, "test", source="local")
        assert id1 == id2

    def test_model_parsing(self, db):
        """get_or_create_model parses raw model name."""
        model_id = get_or_create_model(db, "claude-3-opus-20240229")
        row = db.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        assert row["raw_name"] == "claude-3-opus-20240229"
        assert row["name"] is not None

    def test_provider_caching(self, db):
        """get_or_create_provider returns same ID for same name."""
        id1 = get_or_create_provider(db, "anthropic")
        id2 = get_or_create_provider(db, "anthropic")
        assert id1 == id2

    def test_tool_by_alias(self, db):
        """get_or_create_tool_by_alias creates tool and alias."""
        h_id = get_or_create_harness(db, "claude_code")
        tool_id = get_or_create_tool_by_alias(db, "Read", h_id)
        assert tool_id is not None
        # Same alias returns same ID
        tool_id2 = get_or_create_tool_by_alias(db, "Read", h_id)
        assert tool_id == tool_id2

    def test_ensure_tool_aliases(self, db):
        """ensure_tool_aliases maps raw names to canonical tools."""
        ensure_canonical_tools(db)
        h_id = get_or_create_harness(db, "claude_code")
        ensure_tool_aliases(db, h_id, {"Read": "file.read", "Write": "file.write"})
        # Verify alias was created
        row = db.execute(
            "SELECT tool_id FROM tool_aliases WHERE raw_name = 'Read' AND harness_id = ?",
            (h_id,),
        ).fetchone()
        assert row is not None


# =============================================================================
# Conversation lookup and deletion
# =============================================================================


class TestConversationOps:
    """Tests for conversation lookup and deletion."""

    def test_find_by_external_id(self, populated_db):
        """find_conversation_by_external_id locates stored conversation."""
        conn, conv_id = populated_db
        h_id = get_harness_id_by_name(conn, "test_harness")
        result = find_conversation_by_external_id(conn, h_id, "conv-1")
        assert result is not None
        assert result["id"] == conv_id

    def test_find_by_external_id_missing(self, populated_db):
        """find_conversation_by_external_id returns None for missing."""
        conn, _ = populated_db
        h_id = get_harness_id_by_name(conn, "test_harness")
        assert find_conversation_by_external_id(conn, h_id, "nonexistent") is None

    def test_get_harness_id_missing(self, db):
        """get_harness_id_by_name returns None for unknown harness."""
        assert get_harness_id_by_name(db, "nonexistent") is None

    def test_delete_conversation(self, populated_db):
        """delete_conversation removes conversation and cascaded data."""
        conn, conv_id = populated_db
        delete_conversation(conn, conv_id)
        conn.commit()
        row = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
        assert row[0] == 0
        # Cascaded deletes
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0


# =============================================================================
# File deduplication
# =============================================================================


class TestFileDedup:
    """Tests for file ingestion tracking."""

    def test_record_and_check_ingested(self, populated_db):
        """record_ingested_file + check_file_ingested round-trip."""
        conn, conv_id = populated_db
        record_ingested_file(conn, "/test/file.jsonl", "abc123", conv_id,
                             file_mtime=1234.5, file_size=999, commit=True)
        assert check_file_ingested(conn, "/test/file.jsonl")
        assert not check_file_ingested(conn, "/other/file.jsonl")

    def test_get_ingested_file_info(self, populated_db):
        """get_ingested_file_info returns stored metadata."""
        conn, conv_id = populated_db
        record_ingested_file(conn, "/test/f.jsonl", "hash1", conv_id,
                             file_mtime=100.0, file_size=500, commit=True)
        info = get_ingested_file_info(conn, "/test/f.jsonl")
        assert info["file_hash"] == "hash1"
        assert info["file_mtime"] == 100.0
        assert info["file_size"] == 500

    def test_get_ingested_file_info_missing(self, db):
        """get_ingested_file_info returns None for unknown path."""
        assert get_ingested_file_info(db, "/nonexistent") is None

    def test_record_empty_file(self, db):
        """record_empty_file tracks file without conversation."""
        h_id = get_or_create_harness(db, "test")
        record_empty_file(db, "/empty.jsonl", "emptyhash", h_id, commit=True)
        assert check_file_ingested(db, "/empty.jsonl")

    def test_record_failed_file(self, db):
        """record_failed_file tracks file with error."""
        h_id = get_or_create_harness(db, "test")
        record_failed_file(db, "/bad.jsonl", "badhash", h_id, "parse error", commit=True)
        info = get_ingested_file_info(db, "/bad.jsonl")
        assert info["error"] == "parse error"

    def test_clear_ingested_file_error(self, db):
        """clear_ingested_file_error removes the record."""
        h_id = get_or_create_harness(db, "test")
        record_failed_file(db, "/bad.jsonl", "badhash", h_id, "err", commit=True)
        clear_ingested_file_error(db, "/bad.jsonl")
        assert not check_file_ingested(db, "/bad.jsonl")

    def test_update_file_stat(self, populated_db):
        """update_file_stat updates mtime and size."""
        conn, conv_id = populated_db
        record_ingested_file(conn, "/f.jsonl", "h1", conv_id, file_mtime=1.0, file_size=10, commit=True)
        update_file_stat(conn, "/f.jsonl", 2.0, 20)
        info = get_ingested_file_info(conn, "/f.jsonl")
        assert info["file_mtime"] == 2.0
        assert info["file_size"] == 20

    def test_compute_file_hash(self, tmp_path):
        """compute_file_hash returns consistent SHA-256."""
        f = tmp_path / "test.txt"
        f.write_text("hello")
        h1 = compute_file_hash(f)
        h2 = compute_file_hash(f)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex


# =============================================================================
# FTS5 search
# =============================================================================


class TestFTS:
    """Tests for FTS5 full-text search."""

    def test_search_content(self, populated_db):
        """search_content finds indexed text."""
        from siftd.storage.fts import search_content
        conn, _ = populated_db
        results = search_content(conn, "Python")
        assert len(results) > 0
        assert "conversation_id" in results[0]
        assert "snippet" in results[0]

    def test_search_no_results(self, populated_db):
        """search_content returns empty for no match."""
        from siftd.storage.fts import search_content
        conn, _ = populated_db
        results = search_content(conn, "xyznonexistent")
        assert results == []

    def test_fts5_recall(self, populated_db):
        """fts5_recall_conversations returns matching conversation IDs."""
        from siftd.storage.fts import fts5_recall_conversations
        conn, conv_id = populated_db
        ids, mode = fts5_recall_conversations(conn, "Python")
        assert conv_id in ids
        assert mode in ("and", "or")

    def test_fts5_recall_no_match(self, db):
        """fts5_recall_conversations returns empty set for no match."""
        from siftd.storage.fts import fts5_recall_conversations
        ids, mode = fts5_recall_conversations(db, "xyznonexistent")
        assert ids == set()
        assert mode == "none"

    def test_fts5_recall_details(self, populated_db):
        """fts5_recall_details returns Fts5Recall with query info."""
        from siftd.storage.fts import fts5_recall_details
        conn, _ = populated_db
        recall = fts5_recall_details(conn, "Python function")
        assert recall.fts_query is not None
        assert recall.mode in ("and", "or", "none")

    def test_fts5_best_hit(self, populated_db):
        """fts5_best_hit_for_conversation returns snippet."""
        from siftd.storage.fts import fts5_best_hit_for_conversation
        conn, conv_id = populated_db
        hit = fts5_best_hit_for_conversation(conn, "Python", conversation_id=conv_id)
        assert hit is not None
        assert "snippet" in hit

    def test_rebuild_fts_index(self, populated_db):
        """rebuild_fts_index repopulates from stored content."""
        from siftd.storage.fts import rebuild_fts_index, search_content
        conn, _ = populated_db
        rebuild_fts_index(conn)
        results = search_content(conn, "Python")
        assert len(results) > 0

    def test_ensure_fts_table_idempotent(self, db):
        """ensure_fts_table is safe to call multiple times."""
        from siftd.storage.fts import ensure_fts_table
        ensure_fts_table(db)
        ensure_fts_table(db)  # Should not raise
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='content_fts'"
        ).fetchone()
        assert row is not None


# =============================================================================
# Filters (WhereBuilder)
# =============================================================================


class TestWhereBuilder:
    """Tests for the WhereBuilder dynamic SQL filter."""

    def test_workspace_filter(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.workspace("myproject")
        assert "w.path LIKE" in wb.where_sql()
        assert len(wb.params) == 2

    def test_model_filter(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.model("claude")
        sql = wb.where_sql()
        assert "models" in sql.lower() or "m.raw_name" in sql

    def test_date_filters(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.since("2024-01-01")
        wb.before("2024-02-01")
        sql = wb.where_sql()
        assert "c.started_at >=" in sql
        assert "c.started_at <" in sql

    def test_tags_any(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.tags_any(["bug", "feature"])
        assert "OR" in wb.where_sql()
        assert len(wb.params) == 2

    def test_tags_all(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.tags_all(["bug", "feature"])
        sql = wb.where_sql()
        assert sql.count("IN (SELECT") == 2

    def test_tags_none(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.tags_none(["spam"])
        assert "NOT IN" in wb.where_sql()

    def test_tag_prefix_match(self):
        from siftd.storage.filters import tag_condition
        sql, val = tag_condition("research:")
        assert "LIKE" in sql
        assert val == "research:%"

    def test_tag_exact_match(self):
        from siftd.storage.filters import tag_condition
        sql, val = tag_condition("bugfix")
        assert "=" in sql
        assert val == "bugfix"

    def test_joins_sql(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.workspace("proj")
        joins = wb.joins_sql()
        assert "workspaces" in joins

    def test_needs_group_by(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        assert not wb.needs_group_by
        wb.require_join("r")
        assert wb.needs_group_by

    def test_empty_filters(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        assert wb.where_sql() == ""
        assert wb.joins_sql() == ""

    def test_none_values_skip(self):
        from siftd.storage.filters import WhereBuilder
        wb = WhereBuilder()
        wb.workspace(None)
        wb.model(None)
        wb.since(None)
        wb.before(None)
        wb.tags_any(None)
        wb.tags_all(None)
        wb.tags_none(None)
        assert wb.where_sql() == ""


# =============================================================================
# SQL helpers
# =============================================================================


class TestSqlHelpers:
    """Tests for SQL helper utilities."""

    def test_placeholders(self):
        from siftd.storage.sql_helpers import placeholders
        assert placeholders(3) == "?, ?, ?"
        assert placeholders(1) == "?"

    def test_in_clause(self):
        from siftd.storage.sql_helpers import in_clause
        ph, vals = in_clause([1, 2, 3])
        assert ph == "?, ?, ?"
        assert vals == [1, 2, 3]

    def test_fetchall_dicts(self, db):
        from siftd.storage.sql_helpers import fetchall_dicts
        get_or_create_harness(db, "test", source="local")
        db.commit()
        rows = fetchall_dicts(db, "SELECT name FROM harnesses WHERE name = ?", ("test",))
        assert len(rows) >= 1
        assert isinstance(rows[0], dict)
        assert rows[0]["name"] == "test"

    def test_batched_in_query(self, populated_db):
        from siftd.storage.sql_helpers import batched_in_query
        conn, conv_id = populated_db
        rows = batched_in_query(
            conn,
            "SELECT id FROM conversations WHERE id IN ({placeholders})",
            [conv_id],
        )
        assert len(rows) == 1

    def test_batched_in_query_empty(self, db):
        from siftd.storage.sql_helpers import batched_in_query
        rows = batched_in_query(db, "SELECT 1 WHERE 1 IN ({placeholders})", [])
        assert rows == []

    def test_batched_execute(self, populated_db):
        from siftd.storage.sql_helpers import batched_execute
        conn, conv_id = populated_db
        # Insert some tags
        from siftd.storage.tags import apply_tag, get_or_create_tag
        tag_id = get_or_create_tag(conn, "test-tag")
        apply_tag(conn, "conversation", conv_id, tag_id)
        conn.commit()
        affected = batched_execute(
            conn,
            "DELETE FROM conversation_tags WHERE conversation_id IN ({placeholders})",
            [conv_id],
        )
        assert affected >= 1

    def test_batched_execute_empty(self, db):
        from siftd.storage.sql_helpers import batched_execute
        affected = batched_execute(db, "DELETE FROM tags WHERE id IN ({placeholders})", [])
        assert affected == 0


# =============================================================================
# Sessions
# =============================================================================


class TestSessions:
    """Tests for session tracking and pending tags."""

    def test_register_and_find_session(self, db):
        from siftd.storage.sessions import (
            find_active_session,
            get_session_info,
            is_session_registered,
            register_session,
        )
        sid = register_session(db, "sess-1", "claude_code", "/test/project", commit=True)
        assert sid == "sess-1"
        assert is_session_registered(db, "sess-1")
        assert find_active_session(db, "/test/project") == "sess-1"
        info = get_session_info(db, "sess-1")
        assert info["adapter_name"] == "claude_code"

    def test_unregister_session(self, db):
        from siftd.storage.sessions import is_session_registered, register_session, unregister_session
        register_session(db, "sess-1", "test", commit=True)
        assert unregister_session(db, "sess-1", commit=True)
        assert not is_session_registered(db, "sess-1")
        assert not unregister_session(db, "sess-1")  # Already gone

    def test_queue_and_consume_tags(self, db):
        from siftd.storage.sessions import consume_pending_tags, queue_tag, register_session
        register_session(db, "sess-1", "test", commit=True)
        tag_id = queue_tag(db, "sess-1", "important", commit=True)
        assert tag_id is not None
        # Duplicate returns None
        assert queue_tag(db, "sess-1", "important", commit=True) is None
        tags = consume_pending_tags(db, "sess-1", commit=True)
        assert len(tags) == 1
        assert tags[0].tag_name == "important"
        # After consume, no more tags
        assert consume_pending_tags(db, "sess-1") == []

    def test_queue_exchange_tag(self, db):
        from siftd.storage.sessions import get_pending_tags, queue_tag, register_session
        register_session(db, "sess-1", "test", commit=True)
        queue_tag(db, "sess-1", "reviewed", entity_type="exchange", exchange_index=2, commit=True)
        tags = get_pending_tags(db, "sess-1")
        assert len(tags) == 1
        assert tags[0].entity_type == "exchange"
        assert tags[0].exchange_index == 2

    def test_cleanup_stale_sessions(self, db):
        from siftd.storage.sessions import cleanup_stale_sessions, register_session
        register_session(db, "sess-old", "test", commit=True)
        # Make it stale by backdating
        db.execute(
            "UPDATE active_sessions SET started_at = '2020-01-01T00:00:00', last_seen_at = '2020-01-01T00:00:00' WHERE harness_session_id = 'sess-old'"
        )
        db.commit()
        sessions_del, tags_del = cleanup_stale_sessions(db, max_age_hours=1, commit=True)
        assert sessions_del == 1

    def test_session_not_found(self, db):
        from siftd.storage.sessions import find_active_session, get_session_info
        assert find_active_session(db, "/nonexistent") is None
        assert get_session_info(db, "nonexistent") is None

    def test_stale_count_and_orphan_count(self, db):
        from siftd.storage.sessions import (
            get_orphaned_pending_tags_count,
            get_stale_sessions_count,
            register_session,
        )
        register_session(db, "sess-1", "test", commit=True)
        db.execute(
            "UPDATE active_sessions SET last_seen_at = '2020-01-01T00:00:00' WHERE harness_session_id = 'sess-1'"
        )
        db.commit()
        assert get_stale_sessions_count(db, max_age_hours=1) == 1
        assert get_orphaned_pending_tags_count(db) == 0


# =============================================================================
# Tags
# =============================================================================


class TestTags:
    """Tests for tag CRUD operations."""

    def test_get_or_create_tag(self, db):
        from siftd.storage.tags import get_or_create_tag, get_tag_id
        tag_id = get_or_create_tag(db, "test-tag", "A test tag")
        assert tag_id is not None
        # Same name returns same ID
        assert get_or_create_tag(db, "test-tag") == tag_id
        assert get_tag_id(db, "test-tag") == tag_id
        assert get_tag_id(db, "nonexistent") is None

    def test_apply_and_remove_tag(self, populated_db):
        conn, conv_id = populated_db
        from siftd.storage.tags import apply_tag, get_or_create_tag, remove_tag
        tag_id = get_or_create_tag(conn, "review")
        result = apply_tag(conn, "conversation", conv_id, tag_id, commit=True)
        assert result is not None
        # Duplicate returns None
        assert apply_tag(conn, "conversation", conv_id, tag_id) is None
        # Remove
        assert remove_tag(conn, "conversation", conv_id, tag_id, commit=True)
        assert not remove_tag(conn, "conversation", conv_id, tag_id)

    def test_rename_tag(self, db):
        from siftd.storage.tags import get_or_create_tag, rename_tag
        get_or_create_tag(db, "old-name")
        assert rename_tag(db, "old-name", "new-name", commit=True)
        assert not rename_tag(db, "nonexistent", "whatever")

    def test_rename_tag_conflict(self, db):
        from siftd.storage.tags import get_or_create_tag, rename_tag
        get_or_create_tag(db, "tag-a")
        get_or_create_tag(db, "tag-b")
        with pytest.raises(ValueError, match="already exists"):
            rename_tag(db, "tag-a", "tag-b")

    def test_delete_tag(self, populated_db):
        conn, conv_id = populated_db
        from siftd.storage.tags import apply_tag, delete_tag, get_or_create_tag
        tag_id = get_or_create_tag(conn, "to-delete")
        apply_tag(conn, "conversation", conv_id, tag_id)
        conn.commit()
        removed = delete_tag(conn, "to-delete", commit=True)
        assert removed >= 1
        assert delete_tag(conn, "nonexistent") == -1

    def test_list_tags(self, populated_db):
        conn, conv_id = populated_db
        from siftd.storage.tags import apply_tag, get_or_create_tag, list_tags
        tag_id = get_or_create_tag(conn, "listed-tag")
        apply_tag(conn, "conversation", conv_id, tag_id)
        conn.commit()
        tags = list_tags(conn)
        names = [t["name"] for t in tags]
        assert "listed-tag" in names

    def test_list_tags_with_time_filter(self, populated_db):
        conn, conv_id = populated_db
        from siftd.storage.tags import apply_tag, get_or_create_tag, list_tags
        tag_id = get_or_create_tag(conn, "time-tag")
        apply_tag(conn, "conversation", conv_id, tag_id)
        conn.commit()
        tags = list_tags(conn, since="2024-01-01", before="2025-01-01")
        names = [t["name"] for t in tags]
        assert "time-tag" in names

    def test_apply_tag_unsupported_entity(self, db):
        from siftd.storage.tags import apply_tag, get_or_create_tag
        tag_id = get_or_create_tag(db, "t")
        with pytest.raises(ValueError, match="Unsupported"):
            apply_tag(db, "invalid_entity", "some-id", tag_id)

    def test_remove_tag_unsupported_entity(self, db):
        from siftd.storage.tags import remove_tag
        with pytest.raises(ValueError, match="Unsupported"):
            remove_tag(db, "invalid_entity", "some-id", "tag-id")

    def test_is_derivative_tool_call(self):
        from siftd.storage.tags import is_derivative_tool_call
        assert is_derivative_tool_call("shell.execute", {"command": "siftd query foo"})
        assert is_derivative_tool_call("skill.invoke", {"skill": "siftd"})
        assert not is_derivative_tool_call("shell.execute", {"command": "pytest"})
        assert not is_derivative_tool_call("shell.execute", None)
        assert not is_derivative_tool_call("file.read", {"path": "/test"})


# =============================================================================
# Queries
# =============================================================================


class TestQueries:
    """Tests for read queries."""

    def test_fetch_exchanges(self, populated_db):
        from siftd.storage.queries import fetch_exchanges
        conn, conv_id = populated_db
        exchanges = fetch_exchanges(conn, conversation_id=conv_id)
        assert len(exchanges) == 1
        assert "Python" in exchanges[0].prompt_text

    def test_fetch_exchanges_empty(self, db):
        from siftd.storage.queries import fetch_exchanges
        assert fetch_exchanges(db, prompt_ids=[]) == []

    def test_fetch_conversation_by_prefix(self, populated_db):
        from siftd.storage.queries import fetch_conversation_by_id_or_prefix
        conn, conv_id = populated_db
        result = fetch_conversation_by_id_or_prefix(conn, conv_id[:8])
        assert result is not None
        assert result["id"] == conv_id

    def test_fetch_conversation_model(self, populated_db):
        from siftd.storage.queries import fetch_conversation_model
        conn, conv_id = populated_db
        model = fetch_conversation_model(conn, conv_id)
        assert model is not None

    def test_fetch_token_totals(self, populated_db):
        from siftd.storage.queries import fetch_conversation_token_totals
        conn, conv_id = populated_db
        inp, out = fetch_conversation_token_totals(conn, conv_id)
        assert inp == 100
        assert out == 200

    def test_fetch_prompts_and_content(self, populated_db):
        from siftd.storage.queries import fetch_prompt_text_content, fetch_prompts_for_conversation
        conn, conv_id = populated_db
        prompts = fetch_prompts_for_conversation(conn, conv_id)
        assert len(prompts) == 1
        content = fetch_prompt_text_content(conn, prompts[0]["id"])
        assert len(content) >= 1

    def test_fetch_responses_and_content(self, populated_db):
        from siftd.storage.queries import (
            fetch_response_content_blocks,
            fetch_response_text_content,
            fetch_responses_for_conversation,
        )
        conn, conv_id = populated_db
        responses = fetch_responses_for_conversation(conn, conv_id)
        assert len(responses) == 1
        content = fetch_response_text_content(conn, responses[0]["id"])
        assert len(content) >= 1
        blocks = fetch_response_content_blocks(conn, [responses[0]["id"]])
        assert len(blocks) >= 1

    def test_fetch_tool_calls(self, populated_db):
        from siftd.storage.queries import fetch_tool_calls_for_conversation
        conn, conv_id = populated_db
        tcs = fetch_tool_calls_for_conversation(conn, conv_id)
        assert len(tcs) == 2
        tcs_with_content = fetch_tool_calls_for_conversation(conn, conv_id, include_content=True)
        assert len(tcs_with_content) == 2

    def test_fetch_conversation_tags(self, populated_db):
        from siftd.storage.queries import fetch_conversation_tags, fetch_tags_for_conversations
        from siftd.storage.tags import apply_tag, get_or_create_tag
        conn, conv_id = populated_db
        tag_id = get_or_create_tag(conn, "q-tag")
        apply_tag(conn, "conversation", conv_id, tag_id)
        conn.commit()
        tags = fetch_conversation_tags(conn, conv_id)
        assert "q-tag" in tags
        bulk = fetch_tags_for_conversations(conn, [conv_id])
        assert conv_id in bulk

    def test_fetch_stats(self, populated_db):
        from siftd.storage.queries import (
            fetch_conversation_time_window,
            fetch_harness_conversation_counts,
            fetch_harnesses,
            fetch_model_names,
            fetch_response_token_coverage,
            fetch_table_count,
            fetch_token_coverage_by_harness,
            fetch_top_tools,
            fetch_top_workspaces,
        )
        conn, _ = populated_db
        assert fetch_table_count(conn, "conversations") >= 1
        assert len(fetch_harnesses(conn)) >= 1
        assert len(fetch_top_workspaces(conn)) >= 1
        earliest, latest = fetch_conversation_time_window(conn)
        assert earliest is not None
        assert len(fetch_harness_conversation_counts(conn)) >= 1
        assert len(fetch_model_names(conn)) >= 1
        assert len(fetch_top_tools(conn)) >= 1
        total, with_tok = fetch_response_token_coverage(conn)
        assert total >= 1
        assert len(fetch_token_coverage_by_harness(conn)) >= 1

    def test_fetch_conversation_exchanges(self, populated_db):
        from siftd.storage.queries import fetch_conversation_exchanges
        conn, conv_id = populated_db
        result = fetch_conversation_exchanges(conn, conversation_id=conv_id)
        assert conv_id in result
        assert len(result[conv_id]) >= 1

    def test_fetch_prompt_response_texts(self, populated_db):
        from siftd.storage.queries import fetch_prompt_response_texts
        conn, conv_id = populated_db
        prompt_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM prompts WHERE conversation_id = ?", (conv_id,)
        ).fetchall()]
        results = fetch_prompt_response_texts(conn, prompt_ids)
        assert len(results) >= 1

    def test_fetch_all_conversation_ids(self, populated_db):
        from siftd.storage.queries import fetch_all_conversation_ids
        conn, conv_id = populated_db
        ids = fetch_all_conversation_ids(conn)
        assert conv_id in ids

    def test_fetch_conversation_timestamps(self, populated_db):
        from siftd.storage.queries import fetch_conversation_timestamps
        conn, conv_id = populated_db
        ts = fetch_conversation_timestamps(conn, [conv_id])
        assert conv_id in ts

    def test_fetch_prompt_timestamps(self, populated_db):
        from siftd.storage.queries import fetch_prompt_timestamps
        conn, conv_id = populated_db
        pids = [r["id"] for r in conn.execute(
            "SELECT id FROM prompts WHERE conversation_id = ?", (conv_id,)
        ).fetchall()]
        ts = fetch_prompt_timestamps(conn, pids)
        assert len(ts) >= 1


# =============================================================================
# Conversation stats
# =============================================================================


class TestConversationStats:
    """Tests for materialized conversation stats."""

    def test_rebuild_stats(self, populated_db):
        from siftd.storage.conversation_stats import (
            has_conversation_stats_table,
            rebuild_conversation_stats,
        )
        conn, conv_id = populated_db
        assert has_conversation_stats_table(conn)
        count = rebuild_conversation_stats(conn, commit=True)
        assert count >= 1
        row = conn.execute(
            "SELECT * FROM conversation_stats WHERE conversation_id = ?", (conv_id,)
        ).fetchone()
        assert row["prompt_count"] == 1
        assert row["response_count"] == 1
        assert row["total_tokens"] == 300  # 100 + 200


# =============================================================================
# Tool search
# =============================================================================


class TestToolSearch:
    """Tests for tool search projection."""

    def test_rebuild_tool_search(self, populated_db):
        from siftd.storage.tool_search import rebuild_tool_search_index
        conn, conv_id = populated_db
        rebuild_tool_search_index(conn, commit=True)
        rows = conn.execute("SELECT * FROM tool_search").fetchall()
        assert len(rows) == 2
        # Verify FTS was populated
        fts_rows = conn.execute(
            "SELECT * FROM tool_search_fts WHERE tool_search_fts MATCH 'pytest'"
        ).fetchall()
        assert len(fts_rows) >= 1


# =============================================================================
# Database operations
# =============================================================================


class TestDatabaseOps:
    """Tests for database creation and backup."""

    def test_open_database_creates_schema(self, tmp_path):
        conn = open_database(tmp_path / "new.db")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "conversations" in tables
        assert "prompts" in tables
        conn.close()

    def test_open_database_read_only(self, tmp_path):
        # Create first
        conn = open_database(tmp_path / "ro.db")
        conn.close()
        # Open read-only
        conn = open_database(tmp_path / "ro.db", read_only=True)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "conversations" in tables
        conn.close()

    def test_open_database_read_only_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            open_database(tmp_path / "nonexistent.db", read_only=True)

    def test_create_empty_database(self, tmp_path):
        create_empty_database(tmp_path / "empty.db")
        assert (tmp_path / "empty.db").exists()

    def test_backup_database(self, tmp_path):
        src = tmp_path / "src.db"
        conn = open_database(src)
        conn.close()
        dest = tmp_path / "backup" / "dest.db"
        backup_database(src, dest)
        assert dest.exists()

    def test_backup_missing_source(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            backup_database(tmp_path / "missing.db", tmp_path / "dest.db")
