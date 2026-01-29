"""Tests for user-defined SQL query files with dual variable syntax."""

import pytest

from siftd.api.conversations import (
    QueryError,
    QueryFile,
    list_query_files,
    run_query_file,
)


class TestQueryFile:
    """Tests for QueryFile dataclass."""

    def test_variables_property_combines_both(self):
        """variables property returns sorted union of template and param vars."""
        qf = QueryFile(
            name="test",
            path=None,
            template_vars=["table", "column"],
            param_vars=["value", "limit"],
        )
        assert qf.variables == ["column", "limit", "table", "value"]

    def test_variables_dedupes_overlap(self):
        """If same name appears in both, it's only listed once."""
        qf = QueryFile(
            name="test",
            path=None,
            template_vars=["ws"],
            param_vars=["ws", "other"],
        )
        assert qf.variables == ["other", "ws"]


class TestListQueryFiles:
    """Tests for list_query_files variable detection."""

    def test_detects_template_vars(self, tmp_path, monkeypatch):
        """Detects $var and ${var} syntax."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "test.sql").write_text(
            "SELECT * FROM $table WHERE col = ${value}"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = list_query_files()
        assert len(result) == 1
        assert result[0].template_vars == ["table", "value"]
        assert result[0].param_vars == []

    def test_detects_param_vars(self, tmp_path, monkeypatch):
        """Detects :var syntax for parameterized queries."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "test.sql").write_text(
            "SELECT * FROM t WHERE name = :name AND age > :age"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = list_query_files()
        assert len(result) == 1
        assert result[0].template_vars == []
        assert result[0].param_vars == ["age", "name"]

    def test_detects_both_syntaxes(self, tmp_path, monkeypatch):
        """Detects mixed $var and :var syntax."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "mixed.sql").write_text(
            "SELECT * FROM $table WHERE ws LIKE '%' || :ws || '%'"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = list_query_files()
        assert len(result) == 1
        assert result[0].template_vars == ["table"]
        assert result[0].param_vars == ["ws"]

    def test_ignores_postgres_cast(self, tmp_path, monkeypatch):
        """Does not match ::type (Postgres cast) as :type."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "cast.sql").write_text(
            "SELECT col::text, :actual_param FROM t"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = list_query_files()
        assert result[0].param_vars == ["actual_param"]

    def test_empty_dir(self, tmp_path, monkeypatch):
        """Returns empty list for empty queries dir."""
        queries = tmp_path / "queries"
        queries.mkdir()
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        assert list_query_files() == []

    def test_missing_dir(self, tmp_path, monkeypatch):
        """Returns empty list if queries dir doesn't exist."""
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: tmp_path / "nope")

        assert list_query_files() == []


class TestRunQueryFile:
    """Tests for run_query_file execution."""

    def test_template_var_substitution(self, test_db, tmp_path, monkeypatch):
        """$var syntax does text substitution."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "count.sql").write_text(
            "SELECT COUNT(*) as n FROM $table"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = run_query_file("count", {"table": "conversations"}, db_path=test_db)
        assert result.columns == ["n"]
        assert result.rows[0][0] == 2  # test_db has 2 conversations

    def test_param_var_substitution(self, test_db, tmp_path, monkeypatch):
        """:var syntax uses parameterized query (safe quoting)."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "find.sql").write_text(
            "SELECT id FROM conversations WHERE external_id = :ext_id"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = run_query_file("find", {"ext_id": "conv1"}, db_path=test_db)
        assert result.columns == ["id"]
        assert len(result.rows) == 1

    def test_param_handles_quotes(self, test_db, tmp_path, monkeypatch):
        """Parameterized :var handles values with quotes safely."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "find.sql").write_text(
            "SELECT id FROM conversations WHERE external_id = :ext_id"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        # This would break text substitution but works with parameterization
        result = run_query_file("find", {"ext_id": "doesn't exist"}, db_path=test_db)
        assert result.rows == []  # No match, but no error

    def test_mixed_syntax(self, test_db, tmp_path, monkeypatch):
        """Both $var and :var work together."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "mixed.sql").write_text(
            "SELECT $col FROM conversations WHERE external_id = :ext_id"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = run_query_file(
            "mixed",
            {"col": "started_at", "ext_id": "conv1"},
            db_path=test_db,
        )
        assert result.columns == ["started_at"]
        assert len(result.rows) == 1

    def test_missing_template_var_raises(self, test_db, tmp_path, monkeypatch):
        """Missing $var raises QueryError with clear message."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "needs.sql").write_text("SELECT * FROM $table")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        with pytest.raises(QueryError, match="Missing template variables: table"):
            run_query_file("needs", {}, db_path=test_db)

    def test_missing_param_var_raises(self, test_db, tmp_path, monkeypatch):
        """Missing :var raises QueryError with clear message."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "needs.sql").write_text(
            "SELECT * FROM conversations WHERE id = :conv_id"
        )
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        with pytest.raises(QueryError, match="Missing parameter variables: conv_id"):
            run_query_file("needs", {}, db_path=test_db)

    def test_file_not_found(self, test_db, tmp_path, monkeypatch):
        """Missing query file raises FileNotFoundError."""
        queries = tmp_path / "queries"
        queries.mkdir()
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        with pytest.raises(FileNotFoundError, match="Query file not found"):
            run_query_file("nope", {}, db_path=test_db)

    def test_db_not_found(self, tmp_path, monkeypatch):
        """Missing database raises FileNotFoundError."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "test.sql").write_text("SELECT 1")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        with pytest.raises(FileNotFoundError, match="Database not found"):
            run_query_file("test", {}, db_path=tmp_path / "nope.db")

    def test_sql_error_raises(self, test_db, tmp_path, monkeypatch):
        """SQL syntax error raises QueryError."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "bad.sql").write_text("SLECT * FORM nothing")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        with pytest.raises(QueryError, match="SQL error"):
            run_query_file("bad", {}, db_path=test_db)

    def test_no_variables_needed(self, test_db, tmp_path, monkeypatch):
        """Query with no variables works."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "simple.sql").write_text("SELECT COUNT(*) as n FROM conversations")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        result = run_query_file("simple", db_path=test_db)
        assert result.rows[0][0] == 2

    def test_extra_variables_ignored(self, test_db, tmp_path, monkeypatch):
        """Extra variables in dict are silently ignored."""
        queries = tmp_path / "queries"
        queries.mkdir()
        (queries / "simple.sql").write_text("SELECT COUNT(*) as n FROM conversations")
        monkeypatch.setattr("siftd.paths.queries_dir", lambda: queries)

        # Extra vars don't cause error
        result = run_query_file("simple", {"unused": "value"}, db_path=test_db)
        assert result.rows[0][0] == 2
