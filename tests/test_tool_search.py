"""Tests for tool-search projection/index."""

from siftd.api.database import create_database
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


class TestToolSearchIndex:
    def test_rebuild_projects_tool_calls_into_search_rows(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        conn = create_database(db_path)

        harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
        workspace_id = get_or_create_workspace(conn, "/work/siftd", "2024-01-01T00:00:00Z")
        model_id = get_or_create_model(conn, "test-model")
        shell_tool_id = get_or_create_tool(conn, "shell.execute", description="Execute shell commands")
        read_tool_id = get_or_create_tool(conn, "file.read", description="Read file contents")

        conv_id = insert_conversation(
            conn,
            external_id="conv-1",
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at="2024-01-15T10:00:00Z",
        )
        prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-15T10:00:00Z")
        response_id = insert_response(
            conn,
            conv_id,
            prompt_id,
            model_id,
            None,
            "r1",
            "2024-01-15T10:00:01Z",
        )

        shell_tc_id = insert_tool_call(
            conn,
            response_id,
            conv_id,
            shell_tool_id,
            "tc-shell",
            '{"command": "git status"}',
            '{"stderr": "fatal: not a git repository"}',
            "error",
            "2024-01-15T10:00:02Z",
        )
        read_tc_id = insert_tool_call(
            conn,
            response_id,
            conv_id,
            read_tool_id,
            "tc-read",
            '{"file_path": "/work/siftd/pyproject.toml"}',
            '{"content": "[tool.ruff]"}',
            "success",
            "2024-01-15T10:00:03Z",
        )

        rebuild_tool_search_index(conn)

        shell_row = conn.execute(
            "SELECT * FROM tool_search WHERE tool_call_id = ?",
            (shell_tc_id,),
        ).fetchone()
        assert shell_row is not None
        assert shell_row["tool_name"] == "shell.execute"
        assert shell_row["tool_family"] == "shell"
        assert shell_row["status"] == "error"
        assert shell_row["command"] == "git status"
        assert shell_row["command_verb"] == "git"
        assert shell_row["result_snippet"] == "fatal: not a git repository"
        assert "/work/siftd" in shell_row["search_text"]

        read_row = conn.execute(
            "SELECT * FROM tool_search WHERE tool_call_id = ?",
            (read_tc_id,),
        ).fetchone()
        assert read_row is not None
        assert read_row["path"] == "/work/siftd/pyproject.toml"
        assert read_row["basename"] == "pyproject.toml"
        assert read_row["ext"] == "toml"
        assert read_row["result_snippet"] == "[tool.ruff]"

        fts_rows = conn.execute(
            """
            SELECT ts.tool_call_id
            FROM tool_search_fts fts
            JOIN tool_search ts ON ts.rowid = fts.rowid
            WHERE tool_search_fts MATCH ?
            ORDER BY bm25(tool_search_fts)
            """,
            ("pyproject",),
        ).fetchall()
        assert [row["tool_call_id"] for row in fts_rows] == [read_tc_id]

        conn.close()

    def test_table_exists_on_database_create(self, tmp_path):
        conn = create_database(tmp_path / "empty.db")

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        assert "tool_search" in tables
        assert "tool_search_fts" in tables
        conn.close()
