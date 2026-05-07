"""Tests for tool-search projection/index."""

import sqlite3

from siftd.api.database import create_database
from siftd.api.tool_search import (
    _add_conversation_tags_all,
    _add_conversation_tags_any,
    _add_conversation_tags_none,
    _add_owner_clause,
    _add_tool_call_tags,
    _search_tool_calls_impl,
    search_tool_calls,
)
from siftd.storage.sqlite import (
    get_or_create_harness,
    get_or_create_model,
    get_or_create_tool,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_response,
    insert_tool_call,
)
from siftd.storage.tool_search import rebuild_tool_search_index
from siftd.tool_query import ToolQuery


def _seed_db(tmp_path, name="test.db"):
    """Create a minimal DB with one shell.execute tool call."""
    db_path = tmp_path / name
    conn = create_database(db_path)
    h = get_or_create_harness(conn, "h", source="test", log_format="jsonl")
    w = get_or_create_workspace(conn, "/work", "2024-01-01T00:00:00Z")
    m = get_or_create_model(conn, "model")
    t = get_or_create_tool(conn, "shell.execute", description="Execute commands")
    c = insert_conversation(conn, "c1", h, w, "2024-01-15T10:00:00Z")
    p = insert_prompt(conn, c, "p1", "2024-01-15T10:00:00Z")
    r = insert_response(conn, c, p, m, None, "r1", "2024-01-15T10:00:01Z")
    return db_path, conn, t, c, r


class TestToolSearchIndex:
    def test_rebuild_projects_tool_calls(self, tmp_path):
        db_path, conn, shell_t, c, r = _seed_db(tmp_path)
        read_t = get_or_create_tool(conn, "file.read", description="Read files")

        stc = insert_tool_call(conn, r, c, shell_t, "tc-s",
                               '{"command": "git status"}',
                               '{"stderr": "fatal: not a git repository"}',
                               "error", "2024-01-15T10:00:02Z")
        rtc = insert_tool_call(conn, r, c, read_t, "tc-r",
                               '{"file_path": "/work/siftd/pyproject.toml"}',
                               '{"content": "[tool.ruff]"}',
                               "success", "2024-01-15T10:00:03Z")
        rebuild_tool_search_index(conn)

        s = conn.execute("SELECT * FROM tool_search WHERE tool_call_id=?", (stc,)).fetchone()
        assert s["tool_name"] == "shell.execute"
        assert s["tool_family"] == "shell"
        assert s["status"] == "error"
        assert s["command"] == "git status"
        assert s["command_verb"] == "git"

        rd = conn.execute("SELECT * FROM tool_search WHERE tool_call_id=?", (rtc,)).fetchone()
        assert rd["path"] == "/work/siftd/pyproject.toml"
        assert rd["basename"] == "pyproject.toml"
        assert rd["ext"] == "toml"

        fts = conn.execute(
            "SELECT ts.tool_call_id FROM tool_search_fts fts"
            " JOIN tool_search ts ON ts.rowid = fts.rowid"
            " WHERE tool_search_fts MATCH ? ORDER BY bm25(tool_search_fts)",
            ("pyproject",),
        ).fetchall()
        assert [row["tool_call_id"] for row in fts] == [rtc]
        conn.close()

    def test_tables_not_created_eagerly(self, tmp_path):
        conn = create_database(tmp_path / "empty.db")
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()}
        assert "tool_search" not in tables
        conn.close()


class TestSQLBuilders:
    """Tests for SQL WHERE clause builder functions."""

    def test_tags_any(self):
        # Default kinds=None matches all conversation-bearing kinds (5 total).
        w, p = [], []
        _add_conversation_tags_any(w, p, ["bug", "feat"])
        assert len(w) == 1 and "OR" in w[0]
        # 5 kind placeholders + 2 tag values
        assert p == ["conversation", "prompt", "response", "tool_call", "exchange", "bug", "feat"]

    def test_tags_any_scoped(self):
        w, p = [], []
        _add_conversation_tags_any(w, p, ["bug"], ["conversation"])
        assert p == ["conversation", "bug"]

    def test_tags_any_none(self):
        w, p = [], []
        _add_conversation_tags_any(w, p, None)
        assert w == []

    def test_tags_all(self):
        w, p = [], []
        _add_conversation_tags_all(w, p, ["bug", "fix"])
        assert len(w) == 2
        # Two subqueries × (5 kind placeholders + 1 value) = 12
        assert len(p) == 12

    def test_tags_all_scoped(self):
        w, p = [], []
        _add_conversation_tags_all(w, p, ["bug", "fix"], ["response"])
        assert p == ["response", "bug", "response", "fix"]

    def test_tags_all_none(self):
        w, p = [], []
        _add_conversation_tags_all(w, p, None)
        assert w == []

    def test_tags_none(self):
        w, p = [], []
        _add_conversation_tags_none(w, p, ["wip"])
        assert len(w) == 1 and "NOT IN" in w[0]
        assert p == ["conversation", "prompt", "response", "tool_call", "exchange", "wip"]

    def test_tags_none_empty(self):
        w, p = [], []
        _add_conversation_tags_none(w, p, None)
        assert w == []

    def test_owner(self):
        w, p = [], []
        _add_owner_clause(w, p, "alice")
        assert len(w) == 1 and p == ["alice"]

    def test_owner_none(self):
        w, p = [], []
        _add_owner_clause(w, p, None)
        assert w == []

    def test_tool_tags(self):
        w, p = [], []
        _add_tool_call_tags(w, p, ["shell:test"])
        assert len(w) == 1 and "tag_assignments" in w[0]

    def test_tool_tags_none(self):
        w, p = [], []
        _add_tool_call_tags(w, p, None)
        assert w == []


class TestSearchToolCallsIntegration:
    def test_rebuild_index(self, tmp_path):
        """L96-102: search with rebuild_index=True."""
        db_path, conn, t, c, r = _seed_db(tmp_path)
        insert_tool_call(conn, r, c, t, "tc1",
                         '{"command": "ls"}', '"files"', "success", "2024-01-01T00:00:02Z")
        conn.commit()
        conn.close()
        parsed, results = search_tool_calls("ls", db_path=db_path, rebuild_index=True)
        assert parsed is not None

    def test_owner_guard_no_table(self, tmp_path):
        """L150: owner filter with no conversation_owners table."""
        conn = sqlite3.connect(str(tmp_path / "bare.db"))
        conn.row_factory = sqlite3.Row
        for t in ("tool_search", "conversations", "responses", "models", "providers", "harnesses"):
            conn.execute(f"CREATE TABLE {t} (id TEXT PRIMARY KEY)")
        parsed = ToolQuery(raw="t", terms=[], fields={}, bare_terms=[], unknown_fields=[])
        assert _search_tool_calls_impl(conn, parsed, limit=10, owner="alice") == []
        conn.close()
