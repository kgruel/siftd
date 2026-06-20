"""Tests for `siftd report` — the canonical named-SQL runner.

`report` was extracted from `query sql` (CLI UX audit, read-surface slice).
These cover the canonical verb; the deprecated `query sql` alias is covered in
test_cli.py::TestQuerySqlCommand. The final test pins the alias-first contract:
`query sql` keeps working but warns to stderr and steers to `report`.
"""

from siftd.cli import main


class TestReportCommand:
    def test_report_list(self, test_db, tmp_path, monkeypatch, capsys):
        """`siftd report` (no name) lists available reports."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "count_convs.sql").write_text("SELECT COUNT(*) FROM conversations")
        (queries / "by_workspace.sql").write_text("SELECT * FROM conversations WHERE workspace_id = :ws")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "report"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "count_convs" in out
        assert "by_workspace" in out

    def test_report_run(self, test_db, tmp_path, monkeypatch, capsys):
        """`siftd report <name>` runs the report."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "count.sql").write_text("SELECT COUNT(*) as n FROM conversations")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "report", "count"])

        assert rc == 0
        assert "2" in capsys.readouterr().out  # test_db has 2 conversations

    def test_report_with_var(self, test_db, tmp_path, monkeypatch, capsys):
        """`siftd report <name> --var key=value` substitutes parameters."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "find.sql").write_text("SELECT id FROM conversations WHERE external_id = :ext_id")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        import sqlite3

        expected_id = sqlite3.connect(str(test_db)).execute(
            "SELECT id FROM conversations WHERE external_id = 'conv1'"
        ).fetchone()[0]

        rc = main(["--db", str(test_db), "report", "find", "--var", "ext_id=conv1"])

        assert rc == 0
        assert expected_id[:12] in capsys.readouterr().out

    def test_report_missing_var(self, test_db, tmp_path, monkeypatch, capsys):
        """Missing required var returns error mentioning the variable."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "needs.sql").write_text("SELECT * FROM $table")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "report", "needs"])

        assert rc == 1
        assert "table" in capsys.readouterr().out.lower()

    def test_report_not_found(self, test_db, tmp_path, monkeypatch, capsys):
        """Unknown report name returns error."""
        queries = tmp_path / "queries"
        queries.mkdir()
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "report", "nonexistent"])

        assert rc == 1
        assert "not found" in capsys.readouterr().out.lower()

    def test_report_empty_user_dir_lists_builtins(self, test_db, tmp_path, monkeypatch, capsys):
        """An empty user dir still lists the built-in reports (always available)."""
        queries = tmp_path / "queries"
        queries.mkdir()
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        rc = main(["--db", str(test_db), "report"])

        assert rc == 0
        assert "cost" in capsys.readouterr().out  # a built-in report


def test_query_sql_alias_warns_and_runs(test_db, tmp_path, monkeypatch, capsys):
    """`query sql` keeps working (alias-first) but warns to stderr, not stdout."""
    queries = tmp_path / "queries"
    queries.mkdir()
    (queries / "count.sql").write_text("SELECT COUNT(*) as n FROM conversations")
    monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)
    # The notice fires once per process; clear so this test is order-independent.
    monkeypatch.setattr("siftd.cli._common._DEPRECATION_EMITTED", set())

    rc = main(["--db", str(test_db), "query", "sql", "count"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "2" in captured.out  # still produces the result
    assert "deprecated" in captured.err  # steer to `report`, on stderr
    assert "report" in captured.err
    assert "deprecated" not in captured.out  # never pollutes stdout/pipes
