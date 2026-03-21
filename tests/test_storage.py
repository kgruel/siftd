"""Tests for siftd storage layer coverage."""

import pytest

from siftd.domain.models import ContentBlock, Conversation, Harness, Prompt, Response, ToolCall, Usage
from siftd.storage.conversation_stats import (
    ensure_conversation_stats_table,
    has_conversation_stats_table,
    rebuild_conversation_stats,
)
from siftd.storage.filters import WhereBuilder, tag_condition
from siftd.storage.fts import (
    ensure_fts_table,
    fts5_best_hit_for_conversation,
    fts5_recall_conversations,
    fts5_recall_details,
    rebuild_fts_index,
    search_content,
)
from siftd.storage.queries import (
    fetch_all_conversation_ids,
    fetch_conversation_by_id_or_prefix,
    fetch_conversation_exchanges,
    fetch_conversation_model,
    fetch_conversation_tags,
    fetch_conversation_time_window,
    fetch_conversation_timestamps,
    fetch_conversation_token_totals,
    fetch_exchanges,
    fetch_harness_conversation_counts,
    fetch_harnesses,
    fetch_model_names,
    fetch_prompt_response_texts,
    fetch_prompt_text_content,
    fetch_prompt_timestamps,
    fetch_prompts_for_conversation,
    fetch_response_content_blocks,
    fetch_response_text_content,
    fetch_response_token_coverage,
    fetch_responses_for_conversation,
    fetch_table_count,
    fetch_tags_for_conversations,
    fetch_token_coverage_by_harness,
    fetch_tool_calls_for_conversation,
    fetch_tool_tags_by_prefix,
    fetch_tool_tags_by_workspace,
    fetch_top_conversation_tags,
    fetch_top_tools,
    fetch_top_workspaces,
    has_pricing_table,
)
from siftd.storage.sessions import (
    cleanup_stale_sessions,
    consume_pending_tags,
    find_active_session,
    get_orphaned_pending_tags_count,
    get_pending_tags,
    get_session_info,
    get_stale_sessions_count,
    is_session_registered,
    queue_tag,
    register_session,
    unregister_session,
)
from siftd.storage.sql_helpers import batched_execute, batched_in_query, fetchall_dicts, in_clause, placeholders
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
    get_or_create_workspace,
    open_database,
    record_empty_file,
    record_failed_file,
    record_ingested_file,
    store_conversation,
    update_file_stat,
)
from siftd.storage.tags import (
    apply_tag,
    delete_tag,
    get_or_create_tag,
    get_tag_id,
    is_derivative_tool_call,
    list_tags,
    remove_tag,
    rename_tag,
    tag_shell_command,
)
from siftd.storage.tool_search import (
    _command_verb,
    _extract_arg,
    _extract_command,
    _extract_path,
    _extract_pattern,
    _extract_result_snippet,
    _loads_dict,
    _tool_family,
    rebuild_tool_search_index,
)


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
        harness=Harness(name="test_harness", source="test", log_format="jsonl"),
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
    conv_id = store_conversation(db, _conv(), commit=True)
    return db, conv_id


# === store_conversation ===

class TestStoreConversation:
    def test_stores_full_conversation(self, populated_db):
        conn, cid = populated_db
        assert conn.execute("SELECT external_id FROM conversations WHERE id=?", (cid,)).fetchone()[0] == "conv-1"
        assert conn.execute("SELECT COUNT(*) FROM prompts WHERE conversation_id=?", (cid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM responses WHERE conversation_id=?", (cid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE conversation_id=?", (cid,)).fetchone()[0] == 2

    def test_fts_indexed(self, populated_db):
        conn, _ = populated_db
        assert len(conn.execute("SELECT * FROM content_fts WHERE content_fts MATCH 'Python'").fetchall()) >= 1

    def test_response_attributes(self, populated_db):
        conn, _ = populated_db
        assert any(r["key"] == "cache_read_input_tokens" for r in conn.execute("SELECT * FROM response_attributes").fetchall())

    def test_auto_tags_shell(self, populated_db):
        conn, _ = populated_db
        tags = conn.execute("SELECT t.name FROM tool_call_tags tct JOIN tags t ON t.id=tct.tag_id").fetchall()
        assert any("shell:" in t["name"] for t in tags)

    def test_workspace_cache(self, db):
        cache = {}
        for i in range(3):
            store_conversation(db, _conv(external_id=f"c{i}", started_at=f"2024-0{i+1}-01T10:00:00Z"), _workspace_cache=cache)
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
        cid = store_conversation(db, conv, commit=True)
        tags = db.execute("SELECT t.name FROM conversation_tags ct JOIN tags t ON t.id=ct.tag_id WHERE ct.conversation_id=?", (cid,)).fetchall()
        assert any("derivative" in t["name"] for t in tags)


# === Vocabulary ===

class TestVocabulary:
    def test_harness_cache(self, db):
        assert get_or_create_harness(db, "t", source="l") == get_or_create_harness(db, "t")

    def test_model_parsing(self, db):
        mid = get_or_create_model(db, "claude-3-opus-20240229")
        assert db.execute("SELECT raw_name FROM models WHERE id=?", (mid,)).fetchone()[0] == "claude-3-opus-20240229"

    def test_provider_cache(self, db):
        assert get_or_create_provider(db, "a") == get_or_create_provider(db, "a")

    def test_tool_by_alias(self, db):
        h = get_or_create_harness(db, "cc")
        tid = get_or_create_tool_by_alias(db, "Read", h)
        assert tid == get_or_create_tool_by_alias(db, "Read", h)

    def test_ensure_tool_aliases(self, db):
        ensure_canonical_tools(db)
        h = get_or_create_harness(db, "cc")
        ensure_tool_aliases(db, h, {"Read": "file.read"})
        assert db.execute("SELECT tool_id FROM tool_aliases WHERE raw_name='Read' AND harness_id=?", (h,)).fetchone() is not None


# === Conversation ops ===

class TestConversationOps:
    def test_find_by_external_id(self, populated_db):
        conn, cid = populated_db
        h = get_harness_id_by_name(conn, "test_harness")
        assert find_conversation_by_external_id(conn, h, "conv-1")["id"] == cid

    def test_find_missing(self, populated_db):
        conn, _ = populated_db
        h = get_harness_id_by_name(conn, "test_harness")
        assert find_conversation_by_external_id(conn, h, "nope") is None

    def test_harness_id_missing(self, db):
        assert get_harness_id_by_name(db, "nope") is None

    def test_delete_cascades(self, populated_db):
        conn, cid = populated_db
        delete_conversation(conn, cid)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0


# === File dedup ===

class TestFileDedup:
    def test_record_and_check(self, populated_db):
        conn, cid = populated_db
        record_ingested_file(conn, "/f.jsonl", "abc", cid, file_mtime=1.0, file_size=9, commit=True)
        assert check_file_ingested(conn, "/f.jsonl")
        assert not check_file_ingested(conn, "/other.jsonl")

    def test_get_info(self, populated_db):
        conn, cid = populated_db
        record_ingested_file(conn, "/f.jsonl", "h1", cid, file_mtime=1.0, file_size=5, commit=True)
        info = get_ingested_file_info(conn, "/f.jsonl")
        assert info["file_hash"] == "h1" and info["file_mtime"] == 1.0

    def test_info_missing(self, db):
        assert get_ingested_file_info(db, "/nope") is None

    def test_empty_file(self, db):
        h = get_or_create_harness(db, "t")
        record_empty_file(db, "/e.jsonl", "eh", h, commit=True)
        assert check_file_ingested(db, "/e.jsonl")

    def test_failed_file(self, db):
        h = get_or_create_harness(db, "t")
        record_failed_file(db, "/b.jsonl", "bh", h, "parse error", commit=True)
        assert get_ingested_file_info(db, "/b.jsonl")["error"] == "parse error"

    def test_clear_error(self, db):
        h = get_or_create_harness(db, "t")
        record_failed_file(db, "/b.jsonl", "bh", h, "err", commit=True)
        clear_ingested_file_error(db, "/b.jsonl")
        assert not check_file_ingested(db, "/b.jsonl")

    def test_update_stat(self, populated_db):
        conn, cid = populated_db
        record_ingested_file(conn, "/f.jsonl", "h1", cid, file_mtime=1.0, file_size=10, commit=True)
        update_file_stat(conn, "/f.jsonl", 2.0, 20)
        assert get_ingested_file_info(conn, "/f.jsonl")["file_mtime"] == 2.0

    def test_compute_hash(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("hello")
        h = compute_file_hash(f)
        assert h == compute_file_hash(f) and len(h) == 64


# === FTS ===

class TestFTS:
    def test_search(self, populated_db):
        conn, _ = populated_db
        r = search_content(conn, "Python")
        assert len(r) > 0 and "snippet" in r[0]

    def test_search_empty(self, populated_db):
        assert search_content(populated_db[0], "xyznonexistent") == []

    def test_recall(self, populated_db):
        conn, cid = populated_db
        ids, mode = fts5_recall_conversations(conn, "Python")
        assert cid in ids and mode in ("and", "or")

    def test_recall_empty(self, db):
        ids, mode = fts5_recall_conversations(db, "xyznonexistent")
        assert ids == set() and mode == "none"

    def test_recall_details(self, populated_db):
        r = fts5_recall_details(populated_db[0], "Python function")
        assert r.fts_query is not None

    def test_best_hit(self, populated_db):
        conn, cid = populated_db
        assert fts5_best_hit_for_conversation(conn, "Python", conversation_id=cid) is not None

    def test_rebuild_fts(self, populated_db):
        conn, _ = populated_db
        rebuild_fts_index(conn)
        assert len(search_content(conn, "Python")) > 0

    def test_ensure_fts_idempotent(self, db):
        ensure_fts_table(db)
        ensure_fts_table(db)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='content_fts'").fetchone() is not None

    def test_recall_or_fallback(self, populated_db):
        r = fts5_recall_details(populated_db[0], "Python function", min_and_hits=999)
        assert r.mode in ("or", "none")

    def test_best_hit_no_match(self, populated_db):
        conn, cid = populated_db
        assert fts5_best_hit_for_conversation(conn, "xyznonexistent", conversation_id=cid) is None

    def test_ensure_fts_recreates_without_porter(self, tmp_path):
        conn = open_database(tmp_path / "t.db")
        conn.execute("DROP TABLE IF EXISTS content_fts")
        conn.execute("CREATE VIRTUAL TABLE content_fts USING fts5(text_content, content_id UNINDEXED, side UNINDEXED, conversation_id UNINDEXED)")
        conn.commit()
        ensure_fts_table(conn)
        assert "porter" in (conn.execute("SELECT sql FROM sqlite_master WHERE name='content_fts'").fetchone()[0] or "").lower()
        conn.close()


# === Filters ===

class TestWhereBuilder:
    def test_workspace(self):
        wb = WhereBuilder()
        wb.workspace("proj")
        assert "w.path LIKE" in wb.where_sql() and len(wb.params) == 2

    def test_model(self):
        wb = WhereBuilder()
        wb.model("claude")
        assert "m.raw_name" in wb.where_sql() or "models" in wb.where_sql().lower()

    def test_dates(self):
        wb = WhereBuilder()
        wb.since("2024-01-01")
        wb.before("2024-02-01")
        assert "c.started_at >=" in wb.where_sql() and "c.started_at <" in wb.where_sql()

    def test_tags_any(self):
        wb = WhereBuilder()
        wb.tags_any(["bug", "feat"])
        assert "OR" in wb.where_sql()

    def test_tags_all(self):
        wb = WhereBuilder()
        wb.tags_all(["a", "b"])
        assert wb.where_sql().count("IN (SELECT") == 2

    def test_tags_none(self):
        wb = WhereBuilder()
        wb.tags_none(["spam"])
        assert "NOT IN" in wb.where_sql()

    def test_tag_prefix(self):
        sql, val = tag_condition("research:")
        assert "LIKE" in sql and val == "research:%"

    def test_tag_exact(self):
        sql, val = tag_condition("bugfix")
        assert "=" in sql and val == "bugfix"

    def test_joins(self):
        wb = WhereBuilder()
        wb.workspace("p")
        assert "workspaces" in wb.joins_sql()

    def test_group_by(self):
        wb = WhereBuilder()
        assert not wb.needs_group_by
        wb.require_join("r")
        assert wb.needs_group_by

    def test_empty(self):
        wb = WhereBuilder()
        assert wb.where_sql() == "" and wb.joins_sql() == ""

    def test_none_skips(self):
        wb = WhereBuilder()
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
    def test_placeholders(self):
        assert placeholders(3) == "?, ?, ?" and placeholders(1) == "?"

    def test_in_clause(self):
        ph, vals = in_clause([1, 2, 3])
        assert ph == "?, ?, ?" and vals == [1, 2, 3]

    def test_fetchall_dicts(self, db):
        get_or_create_harness(db, "t", source="l")
        db.commit()
        rows = fetchall_dicts(db, "SELECT name FROM harnesses WHERE name=?", ("t",))
        assert len(rows) >= 1 and isinstance(rows[0], dict)

    def test_batched_in_query(self, populated_db):
        conn, cid = populated_db
        assert len(batched_in_query(conn, "SELECT id FROM conversations WHERE id IN ({placeholders})", [cid])) == 1

    def test_batched_in_empty(self, db):
        assert batched_in_query(db, "SELECT 1 WHERE 1 IN ({placeholders})", []) == []

    def test_batched_execute(self, populated_db):
        conn, cid = populated_db
        tid = get_or_create_tag(conn, "t")
        apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert batched_execute(conn, "DELETE FROM conversation_tags WHERE conversation_id IN ({placeholders})", [cid]) >= 1

    def test_batched_execute_empty(self, db):
        assert batched_execute(db, "DELETE FROM tags WHERE id IN ({placeholders})", []) == 0


# === Sessions ===

class TestSessions:
    def test_register_and_find(self, db):
        register_session(db, "s1", "claude_code", "/test", commit=True)
        assert is_session_registered(db, "s1")
        assert find_active_session(db, "/test") == "s1"
        assert get_session_info(db, "s1")["adapter_name"] == "claude_code"

    def test_unregister(self, db):
        register_session(db, "s1", "t", commit=True)
        assert unregister_session(db, "s1", commit=True)
        assert not is_session_registered(db, "s1")
        assert not unregister_session(db, "s1")

    def test_queue_consume_tags(self, db):
        register_session(db, "s1", "t", commit=True)
        assert queue_tag(db, "s1", "imp", commit=True) is not None
        assert queue_tag(db, "s1", "imp", commit=True) is None  # dup
        tags = consume_pending_tags(db, "s1", commit=True)
        assert len(tags) == 1 and tags[0].tag_name == "imp"
        assert consume_pending_tags(db, "s1") == []

    def test_exchange_tag(self, db):
        register_session(db, "s1", "t", commit=True)
        queue_tag(db, "s1", "rev", entity_type="exchange", exchange_index=2, commit=True)
        tags = get_pending_tags(db, "s1")
        assert tags[0].entity_type == "exchange" and tags[0].exchange_index == 2

    def test_cleanup_stale(self, db):
        register_session(db, "s1", "t", commit=True)
        db.execute("UPDATE active_sessions SET started_at='2020-01-01T00:00:00', last_seen_at='2020-01-01T00:00:00'")
        db.commit()
        s, _ = cleanup_stale_sessions(db, max_age_hours=1, commit=True)
        assert s == 1

    def test_not_found(self, db):
        assert find_active_session(db, "/nope") is None
        assert get_session_info(db, "nope") is None

    def test_stale_and_orphan_counts(self, db):
        register_session(db, "s1", "t", commit=True)
        db.execute("UPDATE active_sessions SET last_seen_at='2020-01-01T00:00:00'")
        db.commit()
        assert get_stale_sessions_count(db, max_age_hours=1) == 1
        assert get_orphaned_pending_tags_count(db) == 0


# === Tags ===

class TestTags:
    def test_get_or_create(self, db):
        tid = get_or_create_tag(db, "t", "desc")
        assert tid == get_or_create_tag(db, "t")
        assert get_tag_id(db, "t") == tid
        assert get_tag_id(db, "nope") is None

    def test_apply_remove(self, populated_db):
        conn, cid = populated_db
        tid = get_or_create_tag(conn, "r")
        assert apply_tag(conn, "conversation", cid, tid, commit=True) is not None
        assert apply_tag(conn, "conversation", cid, tid) is None
        assert remove_tag(conn, "conversation", cid, tid, commit=True)
        assert not remove_tag(conn, "conversation", cid, tid)

    def test_workspace_tag(self, db):
        ws = get_or_create_workspace(db, "/t", "2024-01-01T10:00:00Z")
        tid = get_or_create_tag(db, "wt")
        assert apply_tag(db, "workspace", ws, tid, commit=True) is not None
        assert remove_tag(db, "workspace", ws, tid, commit=True)

    def test_tool_call_tag(self, populated_db):
        conn, _ = populated_db
        tc = conn.execute("SELECT id FROM tool_calls LIMIT 1").fetchone()["id"]
        tid = get_or_create_tag(conn, "tt")
        assert apply_tag(conn, "tool_call", tc, tid, commit=True) is not None
        assert remove_tag(conn, "tool_call", tc, tid, commit=True)

    def test_prompt_tag(self, populated_db):
        conn, _ = populated_db
        pid = conn.execute("SELECT id FROM prompts LIMIT 1").fetchone()["id"]
        tid = get_or_create_tag(conn, "pt")
        assert apply_tag(conn, "prompt", pid, tid, commit=True) is not None
        assert remove_tag(conn, "prompt", pid, tid, commit=True)

    def test_rename(self, db):
        get_or_create_tag(db, "old")
        assert rename_tag(db, "old", "new", commit=True)
        assert not rename_tag(db, "nope", "x")

    def test_rename_conflict(self, db):
        get_or_create_tag(db, "a")
        get_or_create_tag(db, "b")
        with pytest.raises(ValueError, match="already exists"):
            rename_tag(db, "a", "b")

    def test_delete(self, populated_db):
        conn, cid = populated_db
        tid = get_or_create_tag(conn, "del")
        apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert delete_tag(conn, "del", commit=True) >= 1
        assert delete_tag(conn, "nope") == -1

    def test_list(self, populated_db):
        conn, cid = populated_db
        tid = get_or_create_tag(conn, "listed")
        apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert "listed" in [t["name"] for t in list_tags(conn)]

    def test_list_time_filter(self, populated_db):
        conn, cid = populated_db
        tid = get_or_create_tag(conn, "tf")
        apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert "tf" in [t["name"] for t in list_tags(conn, since="2024-01-01", before="2025-01-01")]

    def test_unsupported_entity(self, db):
        tid = get_or_create_tag(db, "x")
        with pytest.raises(ValueError):
            apply_tag(db, "invalid", "id", tid)
        with pytest.raises(ValueError):
            remove_tag(db, "invalid", "id", "tid")

    def test_is_derivative(self):
        assert is_derivative_tool_call("shell.execute", {"command": "siftd query foo"})
        assert is_derivative_tool_call("skill.invoke", {"skill": "siftd"})
        assert not is_derivative_tool_call("shell.execute", {"command": "pytest"})
        assert not is_derivative_tool_call("shell.execute", None)
        assert not is_derivative_tool_call("file.read", {"path": "/t"})

    def test_tag_shell_command(self, populated_db):
        conn, _ = populated_db
        tc = conn.execute("SELECT id FROM tool_calls LIMIT 1").fetchone()["id"]
        assert tag_shell_command(conn, tc, "shell.execute", {"command": "pytest"}) is not None
        assert tag_shell_command(conn, tc, "file.read", {"path": "/t"}) is None
        assert tag_shell_command(conn, tc, "shell.execute", None) is None
        assert tag_shell_command(conn, tc, "shell.execute", {"command": ""}) is None


# === Queries ===

class TestQueries:
    def test_exchanges(self, populated_db):
        conn, cid = populated_db
        ex = fetch_exchanges(conn, conversation_id=cid)
        assert len(ex) == 1 and "Python" in ex[0].prompt_text

    def test_exchanges_empty(self, db):
        assert fetch_exchanges(db, prompt_ids=[]) == []

    def test_exchanges_exclude(self, populated_db):
        conn, cid = populated_db
        assert fetch_exchanges(conn, exclude_conversation_ids={cid}) == []

    def test_conversation_by_prefix(self, populated_db):
        conn, cid = populated_db
        assert fetch_conversation_by_id_or_prefix(conn, cid[:8])["id"] == cid

    def test_model(self, populated_db):
        assert fetch_conversation_model(populated_db[0], populated_db[1]) is not None

    def test_tokens(self, populated_db):
        inp, out = fetch_conversation_token_totals(populated_db[0], populated_db[1])
        assert inp == 100 and out == 200

    def test_prompts_content(self, populated_db):
        conn, cid = populated_db
        ps = fetch_prompts_for_conversation(conn, cid)
        assert len(ps) == 1 and len(fetch_prompt_text_content(conn, ps[0]["id"])) >= 1

    def test_responses_content(self, populated_db):
        conn, cid = populated_db
        rs = fetch_responses_for_conversation(conn, cid)
        assert len(rs) == 1
        assert len(fetch_response_text_content(conn, rs[0]["id"])) >= 1
        assert len(fetch_response_content_blocks(conn, [rs[0]["id"]])) >= 1

    def test_tool_calls(self, populated_db):
        conn, cid = populated_db
        assert len(fetch_tool_calls_for_conversation(conn, cid)) == 2
        assert len(fetch_tool_calls_for_conversation(conn, cid, include_content=True)) == 2

    def test_tags(self, populated_db):
        conn, cid = populated_db
        tid = get_or_create_tag(conn, "qt")
        apply_tag(conn, "conversation", cid, tid)
        conn.commit()
        assert "qt" in fetch_conversation_tags(conn, cid)
        assert cid in fetch_tags_for_conversations(conn, [cid])

    def test_stats(self, populated_db):
        conn, _ = populated_db
        assert fetch_table_count(conn, "conversations") >= 1
        assert len(fetch_harnesses(conn)) >= 1
        assert len(fetch_top_workspaces(conn)) >= 1
        assert fetch_conversation_time_window(conn)[0] is not None
        assert len(fetch_harness_conversation_counts(conn)) >= 1
        assert len(fetch_model_names(conn)) >= 1
        assert len(fetch_top_tools(conn)) >= 1
        assert fetch_response_token_coverage(conn)[0] >= 1
        assert len(fetch_token_coverage_by_harness(conn)) >= 1

    def test_conversation_exchanges(self, populated_db):
        conn, cid = populated_db
        r = fetch_conversation_exchanges(conn, conversation_id=cid)
        assert cid in r and len(r[cid]) >= 1

    def test_prompt_response_texts(self, populated_db):
        conn, cid = populated_db
        pids = [r["id"] for r in conn.execute("SELECT id FROM prompts WHERE conversation_id=?", (cid,)).fetchall()]
        assert len(fetch_prompt_response_texts(conn, pids)) >= 1

    def test_all_ids(self, populated_db):
        conn, cid = populated_db
        assert cid in fetch_all_conversation_ids(conn)

    def test_timestamps(self, populated_db):
        conn, cid = populated_db
        assert cid in fetch_conversation_timestamps(conn, [cid])
        pids = [r["id"] for r in conn.execute("SELECT id FROM prompts WHERE conversation_id=?", (cid,)).fetchall()]
        assert len(fetch_prompt_timestamps(conn, pids)) >= 1

    def test_pricing_table(self, db):
        assert has_pricing_table(db)

    def test_top_conversation_tags(self, populated_db):
        assert isinstance(fetch_top_conversation_tags(populated_db[0]), list)

    def test_tool_tags(self, populated_db):
        conn, _ = populated_db
        assert isinstance(fetch_tool_tags_by_prefix(conn, "shell:"), list)
        assert isinstance(fetch_tool_tags_by_workspace(conn, "shell:"), list)

    def test_last_ingest_time(self, populated_db):
        from siftd.storage.queries import fetch_last_ingest_time
        conn, cid = populated_db
        record_ingested_file(conn, "/f.jsonl", "h", cid, commit=True)
        assert fetch_last_ingest_time(conn) is not None

    def test_exchanges_no_filters(self, populated_db):
        """fetch_exchanges with no filters returns all."""
        conn, cid = populated_db
        ex = fetch_exchanges(conn)
        assert len(ex) >= 1

    def test_empty_queries(self, db):
        assert fetch_response_content_blocks(db, []) == {}
        assert fetch_tags_for_conversations(db, []) == {}
        assert fetch_conversation_timestamps(db, []) == {}
        assert fetch_prompt_timestamps(db, []) == {}


# === Conversation stats ===

class TestConversationStats:
    def test_rebuild(self, populated_db):
        conn, cid = populated_db
        assert has_conversation_stats_table(conn)
        assert rebuild_conversation_stats(conn, commit=True) >= 1
        row = conn.execute("SELECT * FROM conversation_stats WHERE conversation_id=?", (cid,)).fetchone()
        assert row["prompt_count"] == 1 and row["total_tokens"] == 300

    def test_ensure_idempotent(self, db):
        ensure_conversation_stats_table(db, commit=True)
        ensure_conversation_stats_table(db, commit=True)
        assert has_conversation_stats_table(db)


# === Tool search ===

class TestToolSearch:
    def test_rebuild(self, populated_db):
        conn, _ = populated_db
        rebuild_tool_search_index(conn, commit=True)
        assert len(conn.execute("SELECT * FROM tool_search").fetchall()) == 2
        assert len(conn.execute("SELECT * FROM tool_search_fts WHERE tool_search_fts MATCH 'pytest'").fetchall()) >= 1

    def test_helpers(self):
        assert _tool_family("file.read") == "file" and _tool_family(None) is None
        assert _extract_path({"file_path": "/t.py"}) == "/t.py" and _extract_path({}) is None
        assert _extract_command({"command": "ls"}) == "ls" and _extract_command({}) is None
        assert _command_verb("ls -la") == "ls" and _command_verb(None) is None
        assert _extract_pattern({"pattern": "*.py"}) == "*.py" and _extract_pattern({}) is None
        assert _extract_arg({"query": "q"}) == "q" and _extract_arg({}) is None
        assert _extract_result_snippet({"error": "e"}) == "e" and _extract_result_snippet({}) is None
        assert _loads_dict(None) == {} and _loads_dict("bad") == {} and _loads_dict('"s"') == {}


# === Database ops ===

class TestDatabaseOps:
    def test_creates_schema(self, tmp_path):
        conn = open_database(tmp_path / "new.db")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "conversations" in tables and "prompts" in tables
        conn.close()

    def test_read_only(self, tmp_path):
        open_database(tmp_path / "ro.db").close()
        conn = open_database(tmp_path / "ro.db", read_only=True)
        assert "conversations" in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()

    def test_read_only_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            open_database(tmp_path / "nope.db", read_only=True)

    def test_create_empty(self, tmp_path):
        create_empty_database(tmp_path / "e.db")
        assert (tmp_path / "e.db").exists()

    def test_backup(self, tmp_path):
        open_database(tmp_path / "src.db").close()
        backup_database(tmp_path / "src.db", tmp_path / "bak" / "d.db")
        assert (tmp_path / "bak" / "d.db").exists()

    def test_backup_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            backup_database(tmp_path / "missing.db", tmp_path / "d.db")
