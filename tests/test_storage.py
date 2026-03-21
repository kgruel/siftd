"""Tests for siftd storage layer coverage."""

import pytest

import siftd.storage.conversation_stats as cstats
import siftd.storage.fts as fts
import siftd.storage.queries as q
import siftd.storage.sessions as sess
import siftd.storage.sqlite as sq
import siftd.storage.tags as tags
import siftd.storage.tool_search as ts
from siftd.domain.models import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall, Usage
from siftd.storage import compute_content_hash, get_content, get_ref_count, release_content, store_content
from siftd.storage.filters import WhereBuilder, tag_condition
from siftd.storage.sql_helpers import batched_execute, batched_in_query, fetchall_dicts, in_clause, placeholders
from siftd.storage.sqlite import open_database


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "test.db")
    yield conn
    conn.close()


def _conv(**kw):
    """Build a Conversation with sensible defaults."""
    defaults = dict(
        external_id="conv-1", workspace_path="/test/project",
        started_at="2024-01-01T10:00:00Z", ended_at="2024-01-01T11:00:00Z",
        branch="main",
        harness=Harness(name="test_harness", source="test", log_format="jsonl", display_name="Test Harness"),
        prompts=[Prompt(
            external_id="p1", timestamp="2024-01-01T10:00:00Z",
            content=[ContentBlock(block_type="text", content={"text": "Write a Python function"})],
            responses=[Response(
                external_id="r1", timestamp="2024-01-01T10:00:01Z",
                model="claude-3-opus-20240229",
                usage=Usage(input_tokens=100, output_tokens=200),
                content=[ContentBlock(block_type="text", content={"text": "Here is a function"})],
                tool_calls=[
                    ToolCall(tool_name="file.write", external_id="tc1",
                             input={"file_path": "/test/file.py"},
                             result={"content": "def hello(): pass"},
                             status="success", timestamp="2024-01-01T10:00:02Z"),
                    ToolCall(tool_name="shell.execute", external_id="tc2",
                             input={"command": "pytest"},
                             result={"output": "1 passed"},
                             status="success", timestamp="2024-01-01T10:00:03Z"),
                ],
                attributes={"cache_read_input_tokens": "50"},
            )],
        )],
    )
    defaults.update(kw)
    return Conversation(**defaults)


@pytest.fixture
def populated_db(db):
    conv_id = sq.store_conversation(db, _conv(), commit=True)
    return db, conv_id


def _scaffold(conn):
    """Create minimal conversation scaffold, return (conv_id, response_id)."""
    h = sq.get_or_create_harness(conn, "test", source="test")
    w = sq.get_or_create_workspace(conn, "/test", "2024-01-01T10:00:00Z")
    c = sq.insert_conversation(conn, "c1", h, w, "2024-01-01T10:00:00Z")
    p = sq.insert_prompt(conn, c, "p1", "2024-01-01T10:00:00Z")
    r = sq.insert_response(conn, c, p, None, None, "r1", "2024-01-01T10:00:01Z")
    return c, r


# === Blob storage ===

class TestBlobStorage:
    def test_store_retrieve_hash(self, db):
        assert store_content(db, "Hello!", commit=True) == compute_content_hash("Hello!")
        h = store_content(db, "Test", commit=True)
        assert get_content(db, h) == "Test"
        assert get_content(db, "nonexistent") is None
        assert store_content(db, "det") == store_content(db, "det")
        assert store_content(db, "A") != store_content(db, "B")
        # Edge cases
        assert get_content(db, store_content(db, "", commit=True)) == ""
        s = "Hello 世界 🌍"
        assert get_content(db, store_content(db, s, commit=True)) == s

    def test_dedup_and_refcount(self, db):
        h = store_content(db, "D", commit=True)
        assert get_ref_count(db, h) == 1
        store_content(db, "D", commit=True)
        assert get_ref_count(db, h) == 2
        for s in ("X", "Y", "Z"):
            store_content(db, s)
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 4  # D,X,Y,Z
        # Release
        release_content(db, h, commit=True)
        assert get_ref_count(db, h) == 1
        release_content(db, h, commit=True)
        assert get_content(db, h) is None
        assert get_ref_count(db, "nope") == 0


class TestToolCallBlobs:
    def test_dedupe_and_cascade(self, db):
        c, r = _scaffold(db)
        sq.insert_tool_call(db, r, c, None, "tc1", '{}', '{"c":"f"}', "success", "2024-01-01T10:00:01Z")
        db.commit()
        row = db.execute("SELECT result, result_hash FROM tool_calls WHERE external_id='tc1'").fetchone()
        assert row["result"] is None and row["result_hash"] is not None
        # Delete cascades and releases blob
        sq.delete_conversation(db, c)
        db.commit()
        assert get_ref_count(db, row["result_hash"]) == 0

    def test_dedupe_disabled_and_null(self, db):
        c, r = _scaffold(db)
        sq.insert_tool_call(db, r, c, None, "tc1", '{}', '{"i":1}', "s", "2024-01-01T10:00:01Z", dedupe_result=False)
        sq.insert_tool_call(db, r, c, None, "tc2", '{}', None, "s", "2024-01-01T10:00:02Z")
        # Non-JSON result with filter_binary=True → hits except path
        sq.insert_tool_call(db, r, c, None, "tc3", '{}', 'not-json', "s", "2024-01-01T10:00:03Z")
        db.commit()
        row = db.execute("SELECT result, result_hash FROM tool_calls WHERE external_id='tc1'").fetchone()
        assert row["result"] == '{"i":1}' and row["result_hash"] is None
        # Non-JSON was still stored (filter_binary exception path leaves it as-is)
        assert db.execute("SELECT result_hash FROM tool_calls WHERE external_id='tc3'").fetchone()["result_hash"] is not None

    def test_shared_blob_cascade(self, db):
        h = sq.get_or_create_harness(db, "test", source="test")
        ws = sq.get_or_create_workspace(db, "/test", "2024-01-01T10:00:00Z")
        result = '{"s":1}'
        ids = []
        for i in range(2):
            c = sq.insert_conversation(db, f"c{i}", h, ws, f"2024-01-0{i+1}T10:00:00Z")
            p = sq.insert_prompt(db, c, f"p{i}", f"2024-01-0{i+1}T10:00:00Z")
            r = sq.insert_response(db, c, p, None, None, f"r{i}", f"2024-01-0{i+1}T10:00:01Z")
            sq.insert_tool_call(db, r, c, None, f"tc{i}", '{}', result, "s", f"2024-01-0{i+1}T10:00:01Z")
            ids.append(c)
        db.commit()
        assert get_ref_count(db, compute_content_hash(result)) == 2
        sq.delete_conversation(db, ids[0])
        db.commit()
        assert get_ref_count(db, compute_content_hash(result)) == 1


class TestBlobMigration:
    def test_migrate_lifecycle(self, db):
        from siftd.storage.migrate_blobs import count_pending_migrations, migrate_existing_results, verify_migration
        assert migrate_existing_results(db)["migrated"] == 0  # empty
        c, r = _scaffold(db)
        for ext, val in [("tc1", '{"a":1}'), ("tc2", '{"a":1}'), ("tc3", '{"b":2}')]:
            sq.insert_tool_call(db, r, c, None, ext, '{}', val, "s", None, dedupe_result=False)
        db.commit()
        assert count_pending_migrations(db)["total"] == 3
        s = migrate_existing_results(db)
        assert s["migrated"] == 3 and s["blobs_created"] == 2
        assert verify_migration(db)["pending"] == 0

    def test_migrate_preserves(self, db):
        from siftd.storage.migrate_blobs import migrate_existing_results
        c, r = _scaffold(db)
        sq.insert_tool_call(db, r, c, None, "new", '{}', '{"s":1}', "s", None, dedupe_result=True)
        sq.insert_tool_call(db, r, c, None, "old", '{}', '{"s":1}', "s", None, dedupe_result=False)
        db.commit()
        assert migrate_existing_results(db)["blobs_reused"] == 1


# === store_conversation ===

class TestStoreConversation:
    def test_stores_full_conversation(self, populated_db):
        conn, cid = populated_db
        assert conn.execute("SELECT external_id FROM conversations WHERE id=?", (cid,)).fetchone()[0] == "conv-1"
        assert conn.execute("SELECT COUNT(*) FROM prompts WHERE conversation_id=?", (cid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM responses WHERE conversation_id=?", (cid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE conversation_id=?", (cid,)).fetchone()[0] == 2
        # FTS indexed
        assert len(conn.execute("SELECT * FROM content_fts WHERE content_fts MATCH 'Python'").fetchall()) >= 1
        # Response attributes
        assert any(r["key"] == "cache_read_input_tokens" for r in conn.execute("SELECT * FROM response_attributes").fetchall())
        # Auto-tagged shell commands
        tags = conn.execute("SELECT t.name FROM tool_call_tags tct JOIN tags t ON t.id=tct.tag_id").fetchall()
        assert any("shell:" in t["name"] for t in tags)

    def test_workspace_cache(self, db):
        cache = {}
        for i in range(3):
            sq.store_conversation(db, _conv(external_id=f"c{i}", started_at=f"2024-0{i+1}-01T10:00:00Z"), _workspace_cache=cache)
        db.commit()
        assert "/test/project" in cache
        assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1

    def test_derivative_tagging(self, db):
        conv = _conv(
            external_id="deriv",
            prompts=[Prompt(external_id="p1", timestamp="2024-01-01T10:00:00Z",
                content=[ContentBlock(block_type="text", content={"text": "s"})],
                responses=[Response(external_id="r1", timestamp="2024-01-01T10:00:01Z",
                    model="test-model", content=[],
                    tool_calls=[ToolCall(tool_name="shell.execute", external_id="tc1",
                        input={"command": "siftd search foo"}, result={"output": "found"}, status="success")])])])
        cid = sq.store_conversation(db, conv, commit=True)
        tags = db.execute("SELECT t.name FROM conversation_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.conversation_id=?", (cid,)).fetchall()
        assert any("derivative" in t["name"] for t in tags)


# === Vocabulary ===

class TestVocabulary:
    def test_caching_and_aliases(self, db):
        assert sq.get_or_create_harness(db, "t", source="l") == sq.get_or_create_harness(db, "t")
        assert sq.get_or_create_provider(db, "a") == sq.get_or_create_provider(db, "a")
        mid = sq.get_or_create_model(db, "claude-3-opus-20240229")
        assert db.execute("SELECT raw_name FROM models WHERE id=?", (mid,)).fetchone()[0] == "claude-3-opus-20240229"
        h = sq.get_or_create_harness(db, "cc")
        tid = sq.get_or_create_tool_by_alias(db, "Read", h)
        assert tid == sq.get_or_create_tool_by_alias(db, "Read", h)
        sq.ensure_canonical_tools(db)
        sq.ensure_tool_aliases(db, h, {"Read": "file.read"})
        assert db.execute("SELECT tool_id FROM tool_aliases WHERE raw_name='Read' AND harness_id=?", (h,)).fetchone() is not None
        # Clear caches and re-lookup: hits the "found in DB, not in cache" paths
        sq.clear_vocabulary_caches()
        assert sq.get_or_create_harness(db, "t") == sq.get_or_create_harness(db, "t")
        assert sq.get_or_create_provider(db, "a") == sq.get_or_create_provider(db, "a")
        assert sq.get_or_create_model(db, "claude-3-opus-20240229") == mid
        assert sq.get_or_create_tool_by_alias(db, "Read", h) == tid
        # Tag cache: create, clear, re-lookup
        tag_id = tags.get_or_create_tag(db, "vocab_test")
        sq.clear_vocabulary_caches()
        assert tags.get_or_create_tag(db, "vocab_test") == tag_id


# === Conversation ops ===

class TestConversationOps:
    def test_find_and_delete(self, populated_db):
        conn, cid = populated_db
        h = sq.get_harness_id_by_name(conn, "test_harness")
        assert sq.find_conversation_by_external_id(conn, h, "conv-1")["id"] == cid
        assert sq.find_conversation_by_external_id(conn, h, "nope") is None
        assert sq.get_harness_id_by_name(conn, "nope") is None
        sq.delete_conversation(conn, cid)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


# === File dedup ===

class TestFileDedup:
    def test_ingested_lifecycle(self, populated_db, tmp_path):
        conn, cid = populated_db
        sq.record_ingested_file(conn, "/f.jsonl", "h1", cid, file_mtime=1.0, file_size=5, commit=True)
        assert sq.check_file_ingested(conn, "/f.jsonl")
        assert not sq.check_file_ingested(conn, "/other.jsonl")
        info = sq.get_ingested_file_info(conn, "/f.jsonl")
        assert info["file_hash"] == "h1" and info["file_mtime"] == 1.0
        assert sq.get_ingested_file_info(conn, "/nope") is None
        sq.update_file_stat(conn, "/f.jsonl", 2.0, 20)
        assert sq.get_ingested_file_info(conn, "/f.jsonl")["file_mtime"] == 2.0
        # File hash
        f = tmp_path / "t.txt"
        f.write_text("hello")
        assert sq.compute_file_hash(f) == sq.compute_file_hash(f) and len(sq.compute_file_hash(f)) == 64

    def test_empty_and_failed(self, db):
        h = sq.get_or_create_harness(db, "t")
        sq.record_empty_file(db, "/e.jsonl", "eh", h, commit=True)
        assert sq.check_file_ingested(db, "/e.jsonl")
        sq.record_failed_file(db, "/b.jsonl", "bh", h, "parse error", commit=True)
        assert sq.get_ingested_file_info(db, "/b.jsonl")["error"] == "parse error"
        sq.clear_ingested_file_error(db, "/b.jsonl")
        assert not sq.check_file_ingested(db, "/b.jsonl")
        # Record with nonexistent conversation
        with pytest.raises(ValueError, match="Conversation not found"):
            sq.record_ingested_file(db, "/x.jsonl", "h", "nonexistent", commit=True)


# === FTS ===

class TestFTS:
    def test_search_and_recall(self, populated_db):
        conn, cid = populated_db
        r = fts.search_content(conn, "Python")
        assert len(r) > 0 and "snippet" in r[0]
        assert fts.search_content(conn, "xyznonexistent") == []
        ids, mode = fts.fts5_recall_conversations(conn, "Python")
        assert cid in ids and mode in ("and", "or")
        assert fts.fts5_recall_details(conn, "Python function").fts_query is not None
        assert fts.fts5_best_hit_for_conversation(conn, "Python", conversation_id=cid) is not None
        assert fts.fts5_best_hit_for_conversation(conn, "xyznonexistent", conversation_id=cid) is None
        # OR fallback
        assert fts.fts5_recall_details(conn, "Python function", min_and_hits=999).mode in ("or", "none")
        # Short tokens → _fts5_or_rewrite returns None → mode="none"
        r_short = fts.fts5_recall_conversations(conn, "ab cd")
        assert r_short[1] == "none"
        # Malformed FTS → exception in AND phase → falls through to OR
        r_bad = fts.fts5_recall_conversations(conn, "NOT AND OR")
        assert r_bad[1] in ("or", "none")

    def test_recall_empty_db(self, db):
        ids, mode = fts.fts5_recall_conversations(db, "xyznonexistent")
        assert ids == set() and mode == "none"

    def test_rebuild_fts(self, populated_db):
        fts.rebuild_fts_index(populated_db[0])
        assert len(fts.search_content(populated_db[0], "Python")) > 0

    def test_ensure_fts(self, db, tmp_path):
        # Table exists with porter → no-op
        fts.ensure_fts_table(db)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='content_fts'").fetchone() is not None
        # Table missing → create from scratch
        db.execute("DROP TABLE IF EXISTS content_fts")
        fts.ensure_fts_table(db)
        assert "porter" in (db.execute("SELECT sql FROM sqlite_master WHERE name='content_fts'").fetchone()[0] or "").lower()
        # Recreate without porter
        conn = open_database(tmp_path / "t.db")
        conn.execute("DROP TABLE IF EXISTS content_fts")
        conn.execute("CREATE VIRTUAL TABLE content_fts USING fts5(text_content, content_id UNINDEXED, side UNINDEXED, conversation_id UNINDEXED)")
        conn.commit()
        fts.ensure_fts_table(conn)
        assert "porter" in (conn.execute("SELECT sql FROM sqlite_master WHERE name='content_fts'").fetchone()[0] or "").lower()
        conn.close()


# === Filters ===

class TestWhereBuilder:
    def test_filters(self):
        wb = WhereBuilder()
        wb.workspace("proj")
        assert "w.path LIKE" in wb.where_sql() and len(wb.params) == 2
        wb2 = WhereBuilder()
        wb2.model("claude")
        assert "models" in wb2.where_sql().lower() or "m.raw_name" in wb2.where_sql()
        wb3 = WhereBuilder()
        wb3.since("2024-01-01")
        wb3.before("2024-02-01")
        assert "c.started_at >=" in wb3.where_sql()

    def test_tags(self):
        wb = WhereBuilder()
        wb.tags_any(["bug", "feat"])
        assert "OR" in wb.where_sql()
        wb2 = WhereBuilder()
        wb2.tags_all(["a", "b"])
        assert wb2.where_sql().count("IN (SELECT") == 2
        wb3 = WhereBuilder()
        wb3.tags_none(["spam"])
        assert "NOT IN" in wb3.where_sql()
        sql, val = tag_condition("research:")
        assert "LIKE" in sql and val == "research:%"
        sql2, val2 = tag_condition("bugfix")
        assert "=" in sql2 and val2 == "bugfix"

    def test_joins_and_groupby(self):
        wb = WhereBuilder()
        wb.workspace("p")
        assert "workspaces" in wb.joins_sql()
        assert not wb.needs_group_by
        wb.require_join("r")
        assert wb.needs_group_by
        # Transitive deps: m → r
        wb2 = WhereBuilder()
        wb2.require_join("m")
        assert "responses" in wb2.joins_sql() and "models" in wb2.joins_sql()

    def test_empty_and_none(self):
        wb = WhereBuilder()
        assert wb.where_sql() == "" and wb.joins_sql() == ""
        wb.workspace(None)
        wb.model(None)
        wb.since(None)
        wb.before(None)
        wb.tags_any(None)
        wb.tags_all(None)
        wb.tags_none(None)
        assert wb.where_sql() == ""


# === SQL helpers ===

class TestSqlHelpers:
    def test_pure_helpers(self):
        assert placeholders(3) == "?, ?, ?" and placeholders(1) == "?"
        ph, vals = in_clause([1, 2, 3])
        assert ph == "?, ?, ?" and vals == [1, 2, 3]

    def test_db_helpers(self, populated_db):
        conn, cid = populated_db
        sq.get_or_create_harness(conn, "t2", source="l")
        conn.commit()
        rows = fetchall_dicts(conn, "SELECT name FROM harnesses WHERE name=?", ("t2",))
        assert len(rows) >= 1 and isinstance(rows[0], dict)
        assert len(batched_in_query(conn, "SELECT id FROM conversations WHERE id IN ({placeholders})", [cid])) == 1
        assert batched_in_query(conn, "SELECT 1 WHERE 1 IN ({placeholders})", []) == []
        tid = tags.get_or_create_tag(conn, "t")
        tags.apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert batched_execute(conn, "DELETE FROM conversation_tags WHERE conversation_id IN ({placeholders})", [cid]) >= 1
        assert batched_execute(conn, "DELETE FROM tags WHERE id IN ({placeholders})", []) == 0


# === Sessions ===

class TestSessions:
    def test_lifecycle(self, db):
        sess.register_session(db, "s1", "claude_code", "/test", commit=True)
        assert sess.is_session_registered(db, "s1")
        assert sess.find_active_session(db, "/test") == "s1"
        assert sess.get_session_info(db, "s1")["adapter_name"] == "claude_code"
        assert sess.unregister_session(db, "s1", commit=True)
        assert not sess.is_session_registered(db, "s1")
        assert not sess.unregister_session(db, "s1")
        assert sess.find_active_session(db, "/nope") is None
        assert sess.get_session_info(db, "nope") is None

    def test_tags(self, db):
        sess.register_session(db, "s1", "t", commit=True)
        assert sess.queue_tag(db, "s1", "imp", commit=True) is not None
        assert sess.queue_tag(db, "s1", "imp", commit=True) is None
        sess.queue_tag(db, "s1", "rev", entity_type="exchange", exchange_index=2, commit=True)
        tags = sess.get_pending_tags(db, "s1")
        assert len(tags) == 2
        assert tags[1].entity_type == "exchange" and tags[1].exchange_index == 2
        consumed = sess.consume_pending_tags(db, "s1", commit=True)
        assert len(consumed) == 2
        assert sess.consume_pending_tags(db, "s1") == []

    def test_stale_cleanup(self, db):
        sess.register_session(db, "s1", "t", commit=True)
        db.execute("UPDATE active_sessions SET started_at='2020-01-01T00:00:00', last_seen_at='2020-01-01T00:00:00'")
        db.commit()
        assert sess.get_stale_sessions_count(db, max_age_hours=1) == 1
        assert sess.get_orphaned_pending_tags_count(db) == 0
        s, _ = sess.cleanup_stale_sessions(db, max_age_hours=1, commit=True)
        assert s == 1


# === Tags ===

class TestTags:
    def test_crud(self, db):
        tid = tags.get_or_create_tag(db, "t", "desc")
        assert tid == tags.get_or_create_tag(db, "t")
        assert tags.get_tag_id(db, "t") == tid
        assert tags.get_tag_id(db, "nope") is None
        tags.get_or_create_tag(db, "old")
        assert tags.rename_tag(db, "old", "new", commit=True)
        assert not tags.rename_tag(db, "nope", "x")
        tags.get_or_create_tag(db, "a2")
        tags.get_or_create_tag(db, "b2")
        with pytest.raises(ValueError, match="already exists"):
            tags.rename_tag(db, "a2", "b2")

    def test_apply_remove_all_entities(self, populated_db):
        conn, cid = populated_db
        tid = tags.get_or_create_tag(conn, "r")
        assert tags.apply_tag(conn, "conversation", cid, tid, commit=True) is not None
        assert tags.apply_tag(conn, "conversation", cid, tid) is None
        assert tags.remove_tag(conn, "conversation", cid, tid, commit=True)
        assert not tags.remove_tag(conn, "conversation", cid, tid)
        # Workspace
        ws = sq.get_or_create_workspace(conn, "/t2", "2024-01-01T10:00:00Z")
        wt = tags.get_or_create_tag(conn, "wt")
        assert tags.apply_tag(conn, "workspace", ws, wt, commit=True) is not None
        assert tags.remove_tag(conn, "workspace", ws, wt, commit=True)
        # Tool call
        tc = conn.execute("SELECT id FROM tool_calls LIMIT 1").fetchone()["id"]
        tt = tags.get_or_create_tag(conn, "tt")
        assert tags.apply_tag(conn, "tool_call", tc, tt, commit=True) is not None
        assert tags.remove_tag(conn, "tool_call", tc, tt, commit=True)
        # Prompt
        pid = conn.execute("SELECT id FROM prompts LIMIT 1").fetchone()["id"]
        pt = tags.get_or_create_tag(conn, "pt")
        assert tags.apply_tag(conn, "prompt", pid, pt, commit=True) is not None
        assert tags.remove_tag(conn, "prompt", pid, pt, commit=True)

    def test_delete_and_list(self, populated_db):
        conn, cid = populated_db
        tid = tags.get_or_create_tag(conn, "del")
        tags.apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert tags.delete_tag(conn, "del", commit=True) >= 1
        assert tags.delete_tag(conn, "nope") == -1
        tid2 = tags.get_or_create_tag(conn, "listed")
        tags.apply_tag(conn, "conversation", cid, tid2)
        conn.commit()
        assert "listed" in [t["name"] for t in tags.list_tags(conn)]
        assert "listed" in [t["name"] for t in tags.list_tags(conn, since="2024-01-01", before="2025-01-01")]

    def test_unsupported_entity(self, db):
        tid = tags.get_or_create_tag(db, "x")
        with pytest.raises(ValueError):
            tags.apply_tag(db, "invalid", "id", tid)
        with pytest.raises(ValueError):
            tags.remove_tag(db, "invalid", "id", "tid")

    def test_is_derivative(self):
        assert tags.is_derivative_tool_call("shell.execute", {"command": "siftd query foo"})
        assert tags.is_derivative_tool_call("skill.invoke", {"skill": "siftd"})
        assert not tags.is_derivative_tool_call("shell.execute", {"command": "pytest"})
        assert not tags.is_derivative_tool_call("shell.execute", None)
        assert not tags.is_derivative_tool_call("file.read", {"path": "/t"})

    def test_tag_shell_command(self, populated_db):
        conn, _ = populated_db
        tc = conn.execute("SELECT id FROM tool_calls LIMIT 1").fetchone()["id"]
        assert tags.tag_shell_command(conn, tc, "shell.execute", {"command": "pytest"}) is not None
        assert tags.tag_shell_command(conn, tc, "file.read", {"path": "/t"}) is None
        assert tags.tag_shell_command(conn, tc, "shell.execute", None) is None
        assert tags.tag_shell_command(conn, tc, "shell.execute", {"command": ""}) is None


# === Queries ===

class TestQueries:
    def test_exchanges(self, populated_db):
        conn, cid = populated_db
        ex = q.fetch_exchanges(conn, conversation_id=cid)
        assert len(ex) == 1 and "Python" in ex[0].prompt_text
        assert q.fetch_exchanges(conn, exclude_conversation_ids={cid}) == []
        assert q.fetch_exchanges(conn, prompt_ids=[]) == []
        assert len(q.fetch_exchanges(conn)) >= 1  # no filters

    def test_conversation_detail(self, populated_db):
        conn, cid = populated_db
        assert q.fetch_conversation_by_id_or_prefix(conn, cid[:8])["id"] == cid
        assert q.fetch_conversation_model(conn, cid) is not None
        inp, out = q.fetch_conversation_token_totals(conn, cid)
        assert inp == 100 and out == 200
        ps = q.fetch_prompts_for_conversation(conn, cid)
        assert len(ps) == 1 and len(q.fetch_prompt_text_content(conn, ps[0]["id"])) >= 1
        rs = q.fetch_responses_for_conversation(conn, cid)
        assert len(rs) == 1 and len(q.fetch_response_text_content(conn, rs[0]["id"])) >= 1
        assert len(q.fetch_response_content_blocks(conn, [rs[0]["id"]])) >= 1
        assert len(q.fetch_tool_calls_for_conversation(conn, cid)) == 2
        assert len(q.fetch_tool_calls_for_conversation(conn, cid, include_content=True)) == 2

    def test_tags_and_exchanges(self, populated_db):
        conn, cid = populated_db
        tid = tags.get_or_create_tag(conn, "qt")
        tags.apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert "qt" in q.fetch_conversation_tags(conn, cid)
        assert cid in q.fetch_tags_for_conversations(conn, [cid])
        r = q.fetch_conversation_exchanges(conn, conversation_id=cid)
        assert cid in r and len(r[cid]) >= 1
        pids = [r["id"] for r in conn.execute("SELECT id FROM prompts WHERE conversation_id=?", (cid,)).fetchall()]
        assert len(q.fetch_prompt_response_texts(conn, pids)) >= 1

    def test_stats(self, populated_db):
        conn, cid = populated_db
        assert q.fetch_table_count(conn, "conversations") >= 1
        assert len(q.fetch_harnesses(conn)) >= 1
        assert len(q.fetch_top_workspaces(conn)) >= 1
        assert q.fetch_conversation_time_window(conn)[0] is not None
        assert len(q.fetch_harness_conversation_counts(conn)) >= 1
        assert len(q.fetch_model_names(conn)) >= 1
        assert len(q.fetch_top_tools(conn)) >= 1
        assert q.fetch_response_token_coverage(conn)[0] >= 1
        assert len(q.fetch_token_coverage_by_harness(conn)) >= 1
        assert q.has_pricing_table(conn)
        assert isinstance(q.fetch_top_conversation_tags(conn), list)
        assert isinstance(q.fetch_tool_tags_by_prefix(conn, "shell:"), list)
        assert isinstance(q.fetch_tool_tags_by_workspace(conn, "shell:"), list)

    def test_ids_and_timestamps(self, populated_db):
        conn, cid = populated_db
        assert cid in q.fetch_all_conversation_ids(conn)
        assert cid in q.fetch_conversation_timestamps(conn, [cid])
        pids = [r["id"] for r in conn.execute("SELECT id FROM prompts WHERE conversation_id=?", (cid,)).fetchall()]
        assert len(q.fetch_prompt_timestamps(conn, pids)) >= 1
        sq.record_ingested_file(conn, "/f.jsonl", "h", cid, commit=True)
        assert q.fetch_last_ingest_time(conn) is not None

    def test_empty_queries(self, db):
        assert q.fetch_response_content_blocks(db, []) == {}
        assert q.fetch_tags_for_conversations(db, []) == {}
        assert q.fetch_conversation_timestamps(db, []) == {}
        assert q.fetch_prompt_timestamps(db, []) == {}
        # Empty DB edge cases
        assert q.fetch_conversation_time_window(db) == (None, None)
        assert q.fetch_last_ingest_time(db) is None


# === Conversation stats ===

class TestConversationStats:
    def test_rebuild(self, populated_db):
        conn, cid = populated_db
        assert cstats.has_conversation_stats_table(conn)
        assert cstats.rebuild_conversation_stats(conn, commit=True) >= 1
        row = conn.execute("SELECT * FROM conversation_stats WHERE conversation_id=?", (cid,)).fetchone()
        assert row["prompt_count"] == 1 and row["total_tokens"] == 300

    def test_ensure_idempotent(self, db):
        cstats.ensure_conversation_stats_table(db, commit=True)
        cstats.ensure_conversation_stats_table(db, commit=True)
        assert cstats.has_conversation_stats_table(db)


# === Tool search ===

class TestToolSearch:
    def test_rebuild(self, populated_db):
        conn, _ = populated_db
        ts.rebuild_tool_search_index(conn, commit=True)
        assert len(conn.execute("SELECT * FROM tool_search").fetchall()) == 2
        assert len(conn.execute("SELECT * FROM tool_search_fts WHERE tool_search_fts MATCH 'pytest'").fetchall()) >= 1

    def test_helpers(self):
        assert ts._tool_family("file.read") == "file" and ts._tool_family(None) is None
        assert ts._extract_path({"file_path": "/t.py"}) == "/t.py" and ts._extract_path({}) is None
        assert ts._extract_command({"command": "ls"}) == "ls" and ts._extract_command({}) is None
        assert ts._command_verb("ls -la") == "ls" and ts._command_verb(None) is None
        assert ts._extract_pattern({"pattern": "*.py"}) == "*.py" and ts._extract_pattern({}) is None
        assert ts._extract_arg({"query": "q"}) == "q" and ts._extract_arg({}) is None
        assert ts._extract_result_snippet({"error": "e"}) == "e" and ts._extract_result_snippet({}) is None
        assert ts._loads_dict(None) == {} and ts._loads_dict("bad") == {} and ts._loads_dict('"s"') == {}
        assert ts._normalize_tool_tokens(None) is None and ts._normalize_tool_tokens("file.read") is not None


# === Database ops ===

class TestDatabaseOps:
    def test_open_and_backup(self, tmp_path):
        conn = open_database(tmp_path / "new.db")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "conversations" in tables and "prompts" in tables
        conn.close()
        # Read-only
        conn = open_database(tmp_path / "new.db", read_only=True)
        assert "conversations" in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        # Backup
        sq.backup_database(tmp_path / "new.db", tmp_path / "bak" / "d.db")
        assert (tmp_path / "bak" / "d.db").exists()
        # Empty
        sq.create_empty_database(tmp_path / "e.db")
        assert (tmp_path / "e.db").exists()

    def test_errors(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            open_database(tmp_path / "nope.db", read_only=True)
        with pytest.raises(FileNotFoundError):
            sq.backup_database(tmp_path / "missing.db", tmp_path / "d.db")
