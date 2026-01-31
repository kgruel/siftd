"""CLI smoke tests — verify commands parse and run without import errors."""


import pytest
from conftest import FIXTURES_DIR

from siftd.cli import main


def test_help_exits_zero():
    """siftd --help exits with code 0."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def test_status_with_db(test_db):
    """siftd --db <path> status runs successfully."""
    rc = main(["--db", str(test_db), "status"])
    assert rc == 0


def test_query_with_db(test_db):
    """siftd --db <path> query lists conversations."""
    rc = main(["--db", str(test_db), "query"])
    assert rc == 0


def test_unknown_subcommand():
    """Unknown subcommand prints help and exits non-zero."""
    with pytest.raises(SystemExit) as exc_info:
        main(["nonexistent-command"])
    assert exc_info.value.code != 0


def test_tag_bulk_apply(test_db, capsys):
    """siftd tag <id> tag1 tag2 tag3 applies all tags in one call."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    rc = main(["--db", str(test_db), "tag", conv_id, "alpha", "beta", "gamma"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Applied tag 'alpha'" in captured.out
    assert "Applied tag 'beta'" in captured.out
    assert "Applied tag 'gamma'" in captured.out

    # Verify all three tags are persisted
    conn = open_database(test_db)
    tags = conn.execute(
        """SELECT t.name FROM conversation_tags ct
           JOIN tags t ON t.id = ct.tag_id
           WHERE ct.conversation_id = ?
           ORDER BY t.name""",
        (conv_id,),
    ).fetchall()
    conn.close()
    assert [r["name"] for r in tags] == ["alpha", "beta", "gamma"]


def test_tag_bulk_remove(test_db, capsys):
    """siftd tag --remove <id> tag1 tag2 removes multiple tags."""
    from siftd.storage.sqlite import open_database

    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    conn.close()

    # Apply first
    main(["--db", str(test_db), "tag", conv_id, "alpha", "beta", "gamma"])
    # Remove two
    rc = main(["--db", str(test_db), "tag", "--remove", conv_id, "alpha", "gamma"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "Removed tag 'alpha'" in captured.out
    assert "Removed tag 'gamma'" in captured.out

    # Only beta should remain
    conn = open_database(test_db)
    tags = conn.execute(
        """SELECT t.name FROM conversation_tags ct
           JOIN tags t ON t.id = ct.tag_id
           WHERE ct.conversation_id = ?""",
        (conv_id,),
    ).fetchall()
    conn.close()
    assert [r["name"] for r in tags] == ["beta"]


class TestIngestCommand:
    """Smoke tests for siftd ingest command."""

    def test_ingest_creates_db(self, tmp_path, capsys):
        """siftd ingest creates database if it doesn't exist."""
        db_path = tmp_path / "new.db"
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        rc = main([
            "--db", str(db_path),
            "ingest",
            "--adapter", "claude_code",
            "--path", str(tmp_path / "projects"),
        ])

        assert rc == 0
        assert db_path.exists()
        captured = capsys.readouterr()
        assert "Creating database" in captured.out

    def test_ingest_with_existing_db(self, test_db, capsys):
        """siftd ingest works with existing database."""
        rc = main([
            "--db", str(test_db),
            "ingest",
            "--adapter", "claude_code",
            "--path", "/nonexistent/path",  # No files, but should still run
        ])

        assert rc == 0
        captured = capsys.readouterr()
        assert "Using database" in captured.out

    def test_ingest_verbose_flag(self, tmp_path, capsys):
        """siftd ingest --verbose shows skipped files."""
        db_path = tmp_path / "test.db"

        # First ingest
        fixture = FIXTURES_DIR / "claude_code_minimal.jsonl"
        dest = tmp_path / "projects" / "test-session" / "conversation.jsonl"
        dest.parent.mkdir(parents=True)
        dest.write_text(fixture.read_text())

        main([
            "--db", str(db_path),
            "ingest",
            "--adapter", "claude_code",
            "--path", str(tmp_path / "projects"),
        ])

        # Second ingest with verbose - should show skipped
        rc = main([
            "--db", str(db_path),
            "ingest",
            "--verbose",
            "--adapter", "claude_code",
            "--path", str(tmp_path / "projects"),
        ])

        assert rc == 0
        captured = capsys.readouterr()
        assert "[skipped]" in captured.out

    def test_ingest_unknown_adapter(self, tmp_path, capsys):
        """siftd ingest with unknown adapter returns error."""
        db_path = tmp_path / "test.db"

        rc = main([
            "--db", str(db_path),
            "ingest",
            "--adapter", "nonexistent_adapter",
        ])

        assert rc == 1
        captured = capsys.readouterr()
        assert "No adapters matched" in captured.out


class TestBackfillCommand:
    """Smoke tests for siftd backfill command."""

    def test_backfill_derivative_tags(self, test_db, capsys):
        """siftd backfill --derivative-tags runs successfully."""
        rc = main(["--db", str(test_db), "backfill", "--derivative-tags"])

        assert rc == 0
        captured = capsys.readouterr()
        # Should indicate completion (may find 0 or more)
        assert "derivative" in captured.out.lower() or "tagged" in captured.out.lower() or "No" in captured.out

    def test_backfill_shell_tags(self, test_db_with_tool_tags, capsys):
        """siftd backfill --shell-tags runs on database with tool calls."""
        rc = main(["--db", str(test_db_with_tool_tags), "backfill", "--shell-tags"])

        assert rc == 0
        # Should complete without error

    def test_backfill_missing_db(self, tmp_path, capsys):
        """siftd backfill with missing database returns error."""
        rc = main(["--db", str(tmp_path / "missing.db"), "backfill", "--derivative-tags"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower() or "Database" in captured.out


class TestQuerySqlCommand:
    """Smoke tests for siftd query sql (run-query) command."""

    def test_query_sql_list(self, test_db, tmp_path, monkeypatch, capsys):
        """siftd query sql (no args) lists available query files."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "count_convs.sql").write_text("SELECT COUNT(*) FROM conversations")
        (queries / "by_workspace.sql").write_text("SELECT * FROM conversations WHERE workspace_id = :ws")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "query", "sql"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "count_convs" in captured.out
        assert "by_workspace" in captured.out
        assert "(vars: ws)" in captured.out or "ws" in captured.out

    def test_query_sql_run(self, test_db, tmp_path, monkeypatch, capsys):
        """siftd query sql <name> runs the query."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "count.sql").write_text("SELECT COUNT(*) as n FROM conversations")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "query", "sql", "count"])

        assert rc == 0
        captured = capsys.readouterr()
        # Should show the count (2 from test_db)
        assert "2" in captured.out

    def test_query_sql_with_var(self, test_db, tmp_path, monkeypatch, capsys):
        """siftd query sql <name> --var key=value works."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "find.sql").write_text(
            "SELECT id FROM conversations WHERE external_id = :ext_id"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main([
            "--db", str(test_db),
            "query", "sql", "find",
            "--var", "ext_id=conv1",
        ])

        assert rc == 0
        captured = capsys.readouterr()
        # Should find the conversation
        assert "id" in captured.out.lower() or captured.out.strip()

    def test_query_sql_missing_var(self, test_db, tmp_path, monkeypatch, capsys):
        """siftd query sql with missing required var returns error."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "needs.sql").write_text("SELECT * FROM $table")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "query", "sql", "needs"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "table" in captured.out.lower()  # Should mention missing var

    def test_query_sql_not_found(self, test_db, tmp_path, monkeypatch, capsys):
        """siftd query sql with unknown query returns error."""
        queries = tmp_path / "queries"
        queries.mkdir()
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "query", "sql", "nonexistent"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    def test_query_sql_empty_queries_dir(self, test_db, tmp_path, monkeypatch, capsys):
        """siftd query sql with no query files shows message."""
        queries = tmp_path / "queries"
        queries.mkdir()
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "query", "sql"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "No queries found" in captured.out


class TestToolsCommand:
    """Tests for siftd tools command."""

    def test_tools_json_empty(self, test_db, capsys):
        """siftd tools --json outputs [] when no tool tags."""
        rc = main(["--db", str(test_db), "tools", "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "[]"

    def test_tools_json_with_data(self, test_db_with_tool_tags, capsys):
        """siftd tools --json outputs JSON array of tag info."""
        import json

        rc = main(["--db", str(test_db_with_tool_tags), "tools", "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) > 0
        # Each entry should have name, count, percentage
        for item in data:
            assert "name" in item
            assert "count" in item
            assert "percentage" in item

    def test_tools_json_by_workspace(self, test_db_with_tool_tags, capsys):
        """siftd tools --json --by-workspace outputs JSON array."""
        import json

        rc = main(["--db", str(test_db_with_tool_tags), "tools", "--json", "--by-workspace"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) > 0
        # Each entry should have workspace, total, tags
        for item in data:
            assert "workspace" in item
            assert "total" in item
            assert "tags" in item
            assert isinstance(item["tags"], list)

    def test_tools_json_missing_db(self, tmp_path, capsys):
        """siftd tools --json outputs [] when database doesn't exist."""
        rc = main(["--db", str(tmp_path / "missing.db"), "tools", "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "[]"


class TestAdaptersCommand:
    """Tests for siftd adapters command."""

    def test_adapters_json(self, capsys):
        """siftd adapters --json outputs JSON array of adapter info."""
        import json

        rc = main(["adapters", "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        # Should have at least built-in adapters
        assert len(data) > 0
        # Each entry should have name, origin, locations
        for item in data:
            assert "name" in item
            assert "origin" in item
            assert "locations" in item

    def test_adapters_json_includes_builtin(self, capsys):
        """siftd adapters --json includes built-in adapters."""
        import json

        rc = main(["adapters", "--json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        names = [a["name"] for a in data]
        # Should include at least claude_code
        assert "claude_code" in names


class TestFTS5ErrorHandling:
    """Tests for FTS5 query syntax error handling."""

    def test_query_no_fts_table_gives_helpful_error(self, test_db, capsys):
        """siftd query -s on DB without FTS table gives 'run ingest' hint."""
        # Drop the FTS table from the existing test database
        import sqlite3
        conn = sqlite3.connect(test_db)
        conn.execute("DROP TABLE IF EXISTS content_fts")
        conn.commit()
        conn.close()

        rc = main(["--db", str(test_db), "query", "-s", "test"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "FTS index not found" in captured.err
        assert "ingest" in captured.err.lower()

    def test_export_no_fts_table_gives_helpful_error(self, test_db, capsys):
        """siftd export -s on DB without FTS table gives 'run ingest' hint."""
        # Drop the FTS table from the existing test database
        import sqlite3
        conn = sqlite3.connect(test_db)
        conn.execute("DROP TABLE IF EXISTS content_fts")
        conn.commit()
        conn.close()

        rc = main(["--db", str(test_db), "export", "-s", "test"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "FTS index not found" in captured.err
        assert "ingest" in captured.err.lower()

    def test_query_malformed_fts5_incomplete_or(self, test_db, capsys):
        """siftd query -s 'foo OR' returns friendly error, exits 1."""
        rc = main(["--db", str(test_db), "query", "-s", "foo OR"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Invalid search query" in captured.err
        assert "syntax" in captured.err.lower()

    def test_query_malformed_fts5_incomplete_and(self, test_db, capsys):
        """siftd query -s 'foo AND' returns friendly error, exits 1."""
        rc = main(["--db", str(test_db), "query", "-s", "foo AND"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Invalid search query" in captured.err

    def test_query_malformed_fts5_unbalanced_parens(self, test_db, capsys):
        """siftd query -s 'foo (' returns friendly error, exits 1."""
        rc = main(["--db", str(test_db), "query", "-s", "foo ("])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Invalid search query" in captured.err

    def test_query_valid_fts5_still_works(self, test_db):
        """siftd query -s with valid FTS5 query still works."""
        rc = main(["--db", str(test_db), "query", "-s", "hello"])
        assert rc == 0

    def test_export_malformed_fts5_incomplete_or(self, test_db, capsys):
        """siftd export -s 'foo OR' returns friendly error, exits 1."""
        rc = main(["--db", str(test_db), "export", "-s", "foo OR"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Invalid search query" in captured.err

    def test_export_malformed_fts5_incomplete_and(self, test_db, capsys):
        """siftd export -s 'incomplete AND' returns friendly error, exits 1."""
        rc = main(["--db", str(test_db), "export", "-s", "incomplete AND"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "Invalid search query" in captured.err

    def test_export_valid_fts5_still_works(self, test_db):
        """siftd export -s with valid FTS5 query still works."""
        # Uses valid query; may find 0 results but shouldn't error
        rc = main(["--db", str(test_db), "export", "-s", "hello"])
        # rc could be 0 (found) or 1 (not found), but not a crash
        assert rc in (0, 1)
