"""Tests for tool-search API and CLI."""

import json
import sqlite3

import pytest

from siftd.api.database import create_database
from siftd.api.tool_search import group_tool_search_results, search_tool_calls
from siftd.cli import main
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


def _build_db(db_path):
    conn = create_database(db_path)
    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/work/siftd", "2024-01-01T00:00:00Z")
    model_id = get_or_create_model(conn, "claude-test")
    shell_tool_id = get_or_create_tool(conn, "shell.execute", description="Execute shell commands")
    read_tool_id = get_or_create_tool(conn, "file.read", description="Read file contents")

    conv_id = insert_conversation(conn, "conv-1", harness_id, workspace_id, started_at="2024-01-15T10:00:00Z")
    prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-15T10:00:00Z")
    response_id = insert_response(conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-15T10:00:01Z")

    insert_tool_call(
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
    insert_tool_call(
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
    conn.commit()
    conn.close()


def _build_raw_alias_db(db_path):
    conn = create_database(db_path)
    harness_id = get_or_create_harness(conn, "pi_agent", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/work/siftd", "2024-01-01T00:00:00Z")
    model_id = get_or_create_model(conn, "claude-test")
    raw_shell_tool_id = get_or_create_tool(conn, "bash", description="Raw shell tool")
    raw_read_tool_id = get_or_create_tool(conn, "read", description="Raw read tool")
    raw_experiment_tool_id = get_or_create_tool(conn, "run_experiment", description="Raw experiment shell tool")
    raw_log_tool_id = get_or_create_tool(conn, "log_experiment", description="Raw experiment logging tool")
    raw_init_tool_id = get_or_create_tool(conn, "init_experiment", description="Raw experiment init tool")
    raw_search_tool_id = get_or_create_tool(conn, "google_web_search", description="Raw web search tool")
    grep_tool_id = get_or_create_tool(conn, "search.grep", description="Canonical grep tool")

    conv_id = insert_conversation(conn, "conv-raw", harness_id, workspace_id, started_at="2024-01-15T10:00:00Z")
    prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-15T10:00:00Z")
    response_id = insert_response(conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-15T10:00:01Z")

    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_shell_tool_id,
        "tc-bash",
        '{"command": "git status"}',
        '{"stderr": "fatal: not a git repository"}',
        "error",
        "2024-01-15T10:00:02Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_read_tool_id,
        "tc-read",
        '{"file_path": "/work/siftd/pyproject.toml"}',
        '{"content": "[tool.ruff]"}',
        "success",
        "2024-01-15T10:00:03Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_experiment_tool_id,
        "tc-run-exp",
        '{"command": "./dev check"}',
        '{"stderr": "all good"}',
        "success",
        "2024-01-15T10:00:04Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_log_tool_id,
        "tc-log-exp",
        '{"metric": 123.4, "description": "benchmark run"}',
        '{"result": "kept"}',
        "success",
        "2024-01-15T10:00:05Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_init_tool_id,
        "tc-init-exp",
        '{"name": "bench run", "metric_name": "latency_ms"}',
        '{"result": "initialized"}',
        "success",
        "2024-01-15T10:00:06Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_search_tool_id,
        "tc-web-search",
        '{"query": "sqlite fts5 bm25"}',
        '{"result": "docs found"}',
        "success",
        "2024-01-15T10:00:07Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        grep_tool_id,
        "tc-grep",
        '{"pattern": "tool_name", "path": "/work/siftd/src/siftd/api/tool_search.py"}',
        '{"output": "tool_name match in api/tool_search.py"}',
        "success",
        "2024-01-15T10:00:07Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_experiment_tool_id,
        "tc-run-exp-dup",
        '{"command": "./dev check"}',
        '{"stderr": "all good"}',
        "success",
        "2024-01-15T10:00:08Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_experiment_tool_id,
        "tc-run-exp-2",
        '{"command": "pytest -k tool_search"}',
        '{"stderr": "1 passed"}',
        "success",
        "2024-01-15T10:00:09Z",
    )
    insert_tool_call(
        conn,
        response_id,
        conv_id,
        raw_experiment_tool_id,
        "tc-run-exp-3",
        '{"command": "rg src/siftd"}',
        '{"stderr": "src/siftd/api/tool_search.py"}',
        "success",
        "2024-01-15T10:00:10Z",
    )
    rebuild_tool_search_index(conn)
    conn.commit()
    conn.close()


class TestApiToolSearch:
    def test_group_tool_search_results_collapses_by_conversation(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        _, results = search_tool_calls("status:error git", db_path=db_path)
        groups = group_tool_search_results(results)

        assert len(groups) == 1
        assert groups[0].tool_call_count == 1
        assert groups[0].conversation_id == results[0].conversation_id
        assert groups[0].tool_names == ["shell.execute"]

    def test_search_tool_calls_with_fields_and_bare_terms(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        parsed, results = search_tool_calls(
            "tool:shell.execute status:error git",
            db_path=db_path,
        )

        assert parsed.fields == {
            "tool": ["shell.execute"],
            "status": ["error"],
        }
        assert parsed.bare_terms == ["git"]
        assert len(results) == 1
        assert results[0].tool_name == "shell.execute"
        assert results[0].command == "git status"

    def test_search_tool_calls_by_path_field_only(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        parsed, results = search_tool_calls(
            "tool:file.read path:pyproject.toml",
            db_path=db_path,
        )

        assert parsed.fields["path"] == ["pyproject.toml"]
        assert len(results) == 1
        assert results[0].basename == "pyproject.toml"

    def test_search_tool_calls_accepts_tool_aliases(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        parsed, results = search_tool_calls(
            "tool:bash status:error git",
            db_path=db_path,
        )

        assert parsed.fields["tool"] == ["shell.execute"]
        assert len(results) == 1
        assert results[0].tool_name == "shell.execute"

    def test_search_tool_calls_auto_builds_missing_projection(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        conn = create_database(db_path)
        conn.execute("DROP TABLE tool_search")
        conn.execute("DROP TABLE tool_search_fts")
        conn.commit()
        conn.close()

        _, results = search_tool_calls(
            "tool:file.read path:pyproject.toml",
            db_path=db_path,
        )

        assert len(results) == 1
        assert results[0].basename == "pyproject.toml"

    def test_search_tool_calls_quotes_shellish_bare_terms(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        _, results = search_tool_calls(
            "tool:shell.execute status:error ./dev check",
            db_path=db_path,
        )

        assert results == []

    def test_search_tool_calls_accepts_cli_style_filters(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        parsed, results = search_tool_calls(
            "git",
            db_path=db_path,
            workspace="siftd",
            model="claude",
            since="2024-01-01",
            before="2024-12-31",
            tool="bash",
        )

        assert parsed.fields["workspace"] == ["siftd"]
        assert parsed.fields["model"] == ["claude"]
        assert parsed.fields["since"] == ["2024-01-01"]
        assert parsed.fields["before"] == ["2024-12-31"]
        assert parsed.fields["tool"] == ["shell.execute"]
        assert len(results) == 1

    def test_search_tool_calls_accepts_inline_common_filters(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        parsed, results = search_tool_calls(
            "workspace:siftd model:claude since:2024-01-01 before:today tool:bash git",
            db_path=db_path,
        )

        assert parsed.fields["workspace"] == ["siftd"]
        assert parsed.fields["model"] == ["claude"]
        assert parsed.fields["tool"] == ["shell.execute"]
        assert len(parsed.fields["since"]) == 1
        assert len(parsed.fields["before"]) == 1
        assert len(results) == 1

    def test_search_tool_calls_mixes_cli_and_inline_filters_with_or_within_field(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        parsed, results = search_tool_calls(
            "workspace:other tool:bash git",
            db_path=db_path,
            workspace="siftd",
        )

        assert parsed.fields["workspace"] == ["other", "siftd"]
        assert parsed.fields["tool"] == ["shell.execute"]
        assert len(results) == 1

    def test_search_tool_calls_invalid_inline_date_raises(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        with pytest.raises(ValueError, match="invalid date format"):
            search_tool_calls("since:not-a-date git", db_path=db_path)

    def test_search_tool_calls_flag_only_and_inline_only_filters_are_equivalent(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        parsed_flags, results_flags = search_tool_calls(
            "git",
            db_path=db_path,
            workspace="siftd",
            model="claude",
            since="2024-01-01",
            before="2024-12-31",
            tool="bash",
        )
        parsed_inline, results_inline = search_tool_calls(
            "workspace:siftd model:claude since:2024-01-01 before:2024-12-31 tool:bash git",
            db_path=db_path,
        )

        assert parsed_flags.fields == parsed_inline.fields
        assert [r.tool_call_id for r in results_flags] == [r.tool_call_id for r in results_inline]

    def test_search_tool_calls_different_fields_are_anded(self, tmp_path):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        _, results = search_tool_calls(
            "tool:file.read status:error",
            db_path=db_path,
        )

        assert results == []

    def test_search_tool_calls_canonical_query_matches_raw_shell_aliases(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:shell.execute", db_path=db_path, n=10)

        assert {r.tool_name for r in results} >= {"bash", "run_experiment"}

    def test_search_tool_calls_canonical_query_matches_raw_file_aliases(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:file.read", db_path=db_path, n=10)

        assert [r.tool_name for r in results] == ["read"]

    def test_search_tool_calls_raw_alias_query_matches_canonical_and_raw_names(self, tmp_path):
        canonical_db = tmp_path / "tool_search_canonical.db"
        raw_db = tmp_path / "tool_search_raw.db"
        _build_db(canonical_db)
        _build_raw_alias_db(raw_db)

        _, canonical_results = search_tool_calls("tool:bash", db_path=canonical_db, n=10)
        _, raw_results = search_tool_calls("tool:bash", db_path=raw_db, n=10)

        assert [r.tool_name for r in canonical_results] == ["shell.execute"]
        assert {r.tool_name for r in raw_results} >= {"bash", "run_experiment"}

    def test_search_tool_calls_preserves_raw_noncanonical_tool_names(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:log_experiment", db_path=db_path, n=10)

        assert [r.tool_name for r in results] == ["log_experiment"]

    def test_search_tool_calls_canonical_shell_query_does_not_absorb_noncanonical_tools(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:shell.execute", db_path=db_path, n=10)

        names = {r.tool_name for r in results}
        assert "log_experiment" not in names
        assert "init_experiment" not in names

    def test_search_tool_calls_preserves_raw_init_experiment_name(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:init_experiment", db_path=db_path, n=10)

        assert [r.tool_name for r in results] == ["init_experiment"]

    def test_search_tool_calls_canonical_web_query_matches_raw_search_alias(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:search.web", db_path=db_path, n=10)

        assert [r.tool_name for r in results] == ["google_web_search"]

    @pytest.mark.parametrize(
        "query,expected_command,expected_path",
        [
            ("./dev", "./dev check", None),
            ("pyproject.toml", None, "/work/siftd/pyproject.toml"),
            ("git status", "git status", None),
            ("pytest -k tool_search", "pytest -k tool_search", None),
            ("rg src/siftd", "rg src/siftd", None),
        ],
    )
    def test_search_tool_calls_handles_punctuation_heavy_bare_terms(self, tmp_path, query, expected_command, expected_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls(query, db_path=db_path, n=10)

        assert results
        if expected_command is not None:
            assert results[0].command == expected_command
        if expected_path is not None:
            assert results[0].path == expected_path

    def test_search_tool_calls_prefers_structured_path_filter_for_path_like_terms(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:file.read path:pyproject.toml", db_path=db_path, n=10)

        assert len(results) == 1
        assert results[0].path == "/work/siftd/pyproject.toml"

    def test_search_tool_calls_command_like_bare_terms_rank_exact_command_first(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:shell.execute pytest -k tool_search", db_path=db_path, n=10)

        assert results
        assert results[0].command == "pytest -k tool_search"

    def test_search_tool_calls_ignores_punctuation_only_bare_terms_when_structured_filters_exist(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:file.read ... ()", db_path=db_path, n=10)

        assert len(results) == 1
        assert results[0].tool_name == "read"

    def test_search_tool_calls_punctuation_only_bare_query_returns_recent_rows_without_fts(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("... () :", db_path=db_path, n=3)

        assert len(results) == 3
        assert [r.tool_name for r in results] == ["run_experiment", "run_experiment", "run_experiment"]

    def test_search_tool_calls_basename_like_term_ranks_matching_path_first(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool_search.py", db_path=db_path, n=10)

        assert results
        assert results[0].path == "/work/siftd/src/siftd/api/tool_search.py"

    def test_search_tool_calls_grep_pattern_term_ranks_pattern_match_first(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool_name", db_path=db_path, n=10)

        assert results
        assert results[0].tool_name == "search.grep"
        assert results[0].pattern == "tool_name"

    def test_search_tool_calls_structured_pattern_filter_absorbs_pattern_intent(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:search.grep pattern:tool_name", db_path=db_path, n=10)

        assert len(results) == 1
        assert results[0].pattern == "tool_name"

    def test_search_tool_calls_mixed_structured_and_bare_query_keeps_expected_top_match(self, tmp_path):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        _, results = search_tool_calls("tool:shell.execute git", db_path=db_path, n=10)

        assert results
        assert results[0].command == "git status"


    def test_result_status_alias_works_as_status_filter(self, tmp_path):
        """result_status: is an alias for status: — both filter the same column."""
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        _, by_status = search_tool_calls("status:error", db_path=db_path)
        _, by_alias = search_tool_calls("result_status:error", db_path=db_path)

        assert len(by_status) > 0
        assert [r.tool_call_id for r in by_status] == [r.tool_call_id for r in by_alias]

    def test_auto_rebuild_on_missing_tables(self, tmp_path):
        """search_tool_calls auto-rebuilds when tool_search tables are missing."""
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        # Drop the projection tables to simulate a pre-existing DB
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE IF EXISTS tool_search_fts")
        conn.execute("DROP TABLE IF EXISTS tool_search")
        conn.commit()
        conn.close()

        _, results = search_tool_calls("tool:shell.execute", db_path=db_path)
        assert len(results) > 0

    def test_auto_rebuild_on_stale_index(self, tmp_path):
        """search_tool_calls auto-rebuilds when new tool_calls exist after ingest."""
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        # First search populates the projection
        _, results_before = search_tool_calls("tool:shell.execute", db_path=db_path)
        count_before = len(results_before)

        # Simulate a new ingest adding a tool call directly to tool_calls
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()[0]
        resp_id = conn.execute("SELECT id FROM responses LIMIT 1").fetchone()[0]
        tool_id = conn.execute("SELECT id FROM tools WHERE name = 'shell.execute' LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO tool_calls (id, conversation_id, response_id, tool_id, timestamp, status, input)"
            " VALUES ('new_tc_001', ?, ?, ?, '2024-06-15T12:00:00Z', 'success', '{\"command\": \"echo hello\"}')",
            (conv_id, resp_id, tool_id),
        )
        conn.commit()
        conn.close()

        # Second search should detect staleness and rebuild
        _, results_after = search_tool_calls("tool:shell.execute", db_path=db_path)
        assert len(results_after) > count_before


class TestCliToolSearch:
    def test_tool_search_text_output_is_grouped_by_default(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "tool:shell.execute",
            "status:error",
            "git",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        assert "01" in captured.out
        assert "1 match" in captured.out
        assert "shell.execute" in captured.out
        assert "git status" in captured.out
        assert "conversation " not in captured.out

    def test_tool_search_show_snippets_opt_in(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "tool:file.read",
            "path:pyproject.toml",
            "--show-snippets",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        assert "[tool.ruff]" in captured.out

    def test_grouped_output_shows_deeper_path_context(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "tool:file.read",
            "path:pyproject.toml",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        assert "work/siftd/pyproject.toml" in captured.out

    def test_tool_search_supports_shared_filter_flags(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "git",
            "-w", "siftd",
            "-m", "claude",
            "--since", "2024-01-01",
            "--before", "2024-12-31",
            "-t", "bash",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        assert "shell.execute" in captured.out
        assert "git status" in captured.out

    def test_tool_search_supports_inline_common_filters(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "workspace:siftd",
            "model:claude",
            "since:2024-01-01",
            "before:today",
            "tool:bash",
            "git",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        assert "shell.execute" in captured.out
        assert "git status" in captured.out

    def test_tool_search_json_output(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search.db"
        _build_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "tool:shell.execute",
            "status:error",
            "git",
            "--json",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["fields"] == {
            "tool": ["shell.execute"],
            "status": ["error"],
        }
        assert len(payload["results"]) == 1
        assert payload["results"][0]["command"] == "git status"
        assert len(payload["groups"]) == 1
        assert payload["groups"][0]["tool_call_count"] == 1

    def test_grouped_output_makes_grep_subject_more_interpretable(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "tool:search.grep",
            "pattern:tool_name",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        assert "grep tool_name" in captured.out
        assert "api/tool_search.py" in captured.out

    def test_grouped_output_collapses_duplicate_matches(self, tmp_path, capsys):
        db_path = tmp_path / "tool_search_raw.db"
        _build_raw_alias_db(db_path)

        rc = main([
            "--db", str(db_path),
            "tool-search",
            "tool:shell.execute",
            "./dev",
        ])
        captured = capsys.readouterr()

        assert rc == 0
        assert "./dev check" in captured.out
        assert "×2" in captured.out
