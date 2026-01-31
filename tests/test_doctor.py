"""Tests for the doctor module."""


import pytest

from siftd.api import (
    CheckInfo,
    Finding,
    list_checks,
    run_checks,
)
from siftd.doctor.checks import (
    CheckContext,
    DropInsValidCheck,
    EmbeddingsStaleCheck,
    FreelistCheck,
    IngestPendingCheck,
    OrphanedChunksCheck,
    PricingGapsCheck,
    SchemaCurrentCheck,
)


@pytest.fixture
def check_context(test_db, tmp_path):
    """Create a CheckContext for testing."""
    embed_db = tmp_path / "embeddings.db"
    adapters_dir = tmp_path / "adapters"
    formatters_dir = tmp_path / "formatters"
    queries_dir = tmp_path / "queries"

    adapters_dir.mkdir()
    formatters_dir.mkdir()
    queries_dir.mkdir()

    ctx = CheckContext(
        db_path=test_db,
        embed_db_path=embed_db,
        adapters_dir=adapters_dir,
        formatters_dir=formatters_dir,
        queries_dir=queries_dir,
    )
    yield ctx
    ctx.close()


class TestListChecks:
    """Tests for list_checks()."""

    def test_returns_check_info_list(self):
        """list_checks returns a list of CheckInfo."""
        checks = list_checks()
        assert len(checks) > 0
        assert all(isinstance(c, CheckInfo) for c in checks)

    def test_expected_checks_present(self):
        """All expected built-in checks are present."""
        checks = list_checks()
        names = {c.name for c in checks}
        assert "ingest-pending" in names
        assert "embeddings-stale" in names
        assert "orphaned-chunks" in names
        assert "pricing-gaps" in names
        assert "drop-ins-valid" in names
        assert "freelist" in names
        assert "schema-current" in names

    def test_has_fix_matches_class_attribute(self):
        """has_fix in CheckInfo matches the class attribute on each check."""
        checks = list_checks()
        by_name = {c.name: c.has_fix for c in checks}
        assert by_name["ingest-pending"] is True
        assert by_name["ingest-errors"] is False
        assert by_name["embeddings-stale"] is True
        assert by_name["orphaned-chunks"] is True
        assert by_name["pricing-gaps"] is False
        assert by_name["drop-ins-valid"] is False

    def test_check_info_has_required_fields(self):
        """CheckInfo has all required fields."""
        checks = list_checks()
        for check in checks:
            assert hasattr(check, "name")
            assert hasattr(check, "description")
            assert hasattr(check, "has_fix")
            assert hasattr(check, "requires_db")
            assert hasattr(check, "requires_embed_db")
            assert hasattr(check, "cost")
            assert isinstance(check.name, str)
            assert isinstance(check.description, str)
            assert isinstance(check.has_fix, bool)
            assert isinstance(check.requires_db, bool)
            assert isinstance(check.requires_embed_db, bool)
            assert check.cost in ("fast", "slow")

    def test_requires_db_attribute(self):
        """requires_db in CheckInfo matches expected values."""
        checks = list_checks()
        by_name = {c.name: c.requires_db for c in checks}
        # Checks that need the database
        assert by_name["ingest-pending"] is True
        assert by_name["ingest-errors"] is True
        assert by_name["embeddings-stale"] is True
        assert by_name["orphaned-chunks"] is True
        assert by_name["pricing-gaps"] is True
        assert by_name["freelist"] is True
        assert by_name["schema-current"] is True
        # Checks that don't need the database
        assert by_name["drop-ins-valid"] is False
        assert by_name["embeddings-available"] is False

    def test_requires_embed_db_attribute(self):
        """requires_embed_db in CheckInfo matches expected values."""
        checks = list_checks()
        by_name = {c.name: c.requires_embed_db for c in checks}
        # Checks that need the embeddings database
        assert by_name["embeddings-stale"] is True
        assert by_name["orphaned-chunks"] is True
        # Checks that don't need the embeddings database
        assert by_name["ingest-pending"] is False
        assert by_name["ingest-errors"] is False
        assert by_name["pricing-gaps"] is False
        assert by_name["drop-ins-valid"] is False
        assert by_name["embeddings-available"] is False
        assert by_name["freelist"] is False
        assert by_name["schema-current"] is False

    def test_cost_attribute(self):
        """cost in CheckInfo matches expected values."""
        checks = list_checks()
        by_name = {c.name: c.cost for c in checks}
        # Only ingest-pending is slow (runs discover())
        assert by_name["ingest-pending"] == "slow"
        # Everything else is fast
        assert by_name["ingest-errors"] == "fast"
        assert by_name["embeddings-stale"] == "fast"
        assert by_name["orphaned-chunks"] == "fast"
        assert by_name["pricing-gaps"] == "fast"
        assert by_name["drop-ins-valid"] == "fast"
        assert by_name["embeddings-available"] == "fast"
        assert by_name["freelist"] == "fast"
        assert by_name["schema-current"] == "fast"


class TestRunChecks:
    """Tests for run_checks()."""

    def test_run_all_checks(self, test_db):
        """run_checks runs all checks when no filter specified."""
        findings = run_checks(db_path=test_db)
        assert isinstance(findings, list)
        assert all(isinstance(f, Finding) for f in findings)

    def test_run_specific_check(self, test_db):
        """run_checks can run a specific check by name."""
        findings = run_checks(checks=["drop-ins-valid"], db_path=test_db)
        for f in findings:
            assert f.check == "drop-ins-valid"

    def test_unknown_check_raises(self, test_db):
        """run_checks raises ValueError for unknown check names."""
        with pytest.raises(ValueError) as excinfo:
            run_checks(checks=["nonexistent-check"], db_path=test_db)
        assert "Unknown check" in str(excinfo.value)

    def test_missing_db_raises(self, tmp_path):
        """run_checks raises FileNotFoundError if database doesn't exist."""
        nonexistent = tmp_path / "nonexistent.db"
        with pytest.raises(FileNotFoundError):
            run_checks(db_path=nonexistent)

    def test_drop_ins_valid_without_db(self, tmp_path):
        """drop-ins-valid check runs without requiring the database to exist."""
        nonexistent = tmp_path / "nonexistent.db"
        # This should NOT raise FileNotFoundError since drop-ins-valid doesn't need DB
        findings = run_checks(checks=["drop-ins-valid"], db_path=nonexistent)
        assert isinstance(findings, list)
        for f in findings:
            assert f.check == "drop-ins-valid"

    def test_db_required_check_without_db_raises(self, tmp_path):
        """Checks that require DB still fail without it."""
        nonexistent = tmp_path / "nonexistent.db"
        with pytest.raises(FileNotFoundError):
            run_checks(checks=["ingest-pending"], db_path=nonexistent)


class TestIngestPendingCheck:
    """Tests for the ingest-pending check."""

    def test_no_pending_files(self, check_context):
        """Returns empty findings when all discovered files are ingested."""
        check = IngestPendingCheck()
        findings = check.run(check_context)
        assert isinstance(findings, list)

    def test_finding_structure(self, check_context):
        """Findings have correct structure."""
        check = IngestPendingCheck()
        findings = check.run(check_context)
        for f in findings:
            assert f.check == "ingest-pending"
            assert f.severity in ("info", "warning", "error")


class TestEmbeddingsStaleCheck:
    """Tests for the embeddings-stale check."""

    def test_no_embeddings_db(self, check_context, monkeypatch):
        """Reports info when embeddings DB doesn't exist."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", True)

        check = EmbeddingsStaleCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "not found" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd ask --index"

    def test_stale_conversations(self, check_context, monkeypatch):
        """Reports stale conversations when embeddings DB exists but is empty."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", True)

        from siftd.storage.embeddings import open_embeddings_db

        embed_conn = open_embeddings_db(check_context.embed_db_path)
        embed_conn.close()

        check = EmbeddingsStaleCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "embeddings-stale"
        assert "conversation" in findings[0].message
        assert "not indexed" in findings[0].message
        assert findings[0].fix_available is True


class TestPricingGapsCheck:
    """Tests for the pricing-gaps check."""

    def test_returns_list(self, check_context):
        """Returns a list of findings (may be empty or have items)."""
        check = PricingGapsCheck()
        findings = check.run(check_context)
        assert isinstance(findings, list)
        assert all(isinstance(f, Finding) for f in findings)

    def test_finding_structure(self, check_context):
        """Findings have correct structure when there are gaps."""
        check = PricingGapsCheck()
        findings = check.run(check_context)
        for f in findings:
            assert f.check == "pricing-gaps"
            assert f.severity == "warning"
            assert f.fix_available is False


class TestDropInsValidCheck:
    """Tests for the drop-ins-valid check."""

    def test_empty_directories(self, check_context):
        """Returns no findings when drop-in directories are empty."""
        check = DropInsValidCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_invalid_adapter(self, check_context):
        """Reports error for invalid adapter file."""
        adapter_file = check_context.adapters_dir / "bad_adapter.py"
        adapter_file.write_text("# Missing required attributes\nx = 1\n")

        check = DropInsValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "drop-ins-valid"
        assert findings[0].severity == "error"
        assert "bad_adapter.py" in findings[0].message
        assert "missing" in findings[0].message

    def test_valid_adapter_no_findings(self, check_context):
        """No findings for valid adapter file."""
        adapter_file = check_context.adapters_dir / "good_adapter.py"
        adapter_file.write_text("""
ADAPTER_INTERFACE_VERSION = 1
NAME = "test_adapter"
DEFAULT_LOCATIONS = ["~/test"]
DEDUP_STRATEGY = "file"
HARNESS_SOURCE = "test"

def discover(locations=None):
    return []

def can_handle(source):
    return False

def parse(source):
    return []
""")

        check = DropInsValidCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_invalid_formatter(self, check_context):
        """Reports error for invalid formatter file."""
        formatter_file = check_context.formatters_dir / "bad_formatter.py"
        formatter_file.write_text("# Missing NAME\ndef create_formatter(): pass\n")

        check = DropInsValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "drop-ins-valid"
        assert "bad_formatter.py" in findings[0].message

    def test_empty_query_file(self, check_context):
        """Reports warning for empty query file."""
        query_file = check_context.queries_dir / "empty.sql"
        query_file.write_text("")

        check = DropInsValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "drop-ins-valid"
        assert findings[0].severity == "warning"
        assert "empty" in findings[0].message

    def test_invalid_sql_syntax(self, check_context):
        """Reports error for invalid SQL syntax via EXPLAIN."""
        query_file = check_context.queries_dir / "bad_syntax.sql"
        query_file.write_text("SELECT * FROM WHERE")  # Invalid SQL

        check = DropInsValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "drop-ins-valid"
        assert findings[0].severity == "error"
        assert "bad_syntax.sql" in findings[0].message

    def test_valid_sql_with_placeholders(self, check_context):
        """Valid SQL with $var placeholders should pass."""
        query_file = check_context.queries_dir / "valid.sql"
        query_file.write_text("SELECT * FROM foo WHERE id = $id AND name = $name")

        check = DropInsValidCheck()
        findings = check.run(check_context)

        # No findings for valid SQL (table doesn't exist but syntax is valid)
        assert findings == []

    def test_skips_underscore_files(self, check_context):
        """Skips files starting with underscore."""
        adapter_file = check_context.adapters_dir / "_private.py"
        adapter_file.write_text("# Should be ignored\n")

        check = DropInsValidCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_adapter_syntax_error(self, check_context):
        """Reports error for adapter with Python syntax error."""
        adapter_file = check_context.adapters_dir / "syntax_error.py"
        adapter_file.write_text("def broken(\n")  # Invalid Python syntax

        check = DropInsValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "drop-ins-valid"
        assert findings[0].severity == "error"
        assert "syntax_error.py" in findings[0].message
        assert "syntax error" in findings[0].message

    def test_formatter_syntax_error(self, check_context):
        """Reports error for formatter with Python syntax error."""
        formatter_file = check_context.formatters_dir / "bad_syntax.py"
        formatter_file.write_text("class Broken(:\n")  # Invalid Python syntax

        check = DropInsValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "drop-ins-valid"
        assert "syntax error" in findings[0].message


class TestFindingDataclass:
    """Finding defaults that callers depend on."""

    def test_defaults(self):
        finding = Finding(
            check="test",
            severity="info",
            message="Test message",
            fix_available=False,
        )
        assert finding.fix_command is None
        assert finding.context is None


class TestCheckContext:
    """Tests for CheckContext."""

    def test_lazy_connection_loading(self, test_db, tmp_path):
        """Connections are not opened until accessed."""
        ctx = CheckContext(
            db_path=test_db,
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        assert ctx._db_conn is None
        assert ctx._embed_conn is None

        conn = ctx.get_db_conn()
        assert conn is not None
        assert ctx._db_conn is not None

        ctx.close()

    def test_close_handles_unopened(self, test_db, tmp_path):
        """close() works even if connections were never opened."""
        ctx = CheckContext(
            db_path=test_db,
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path,
            formatters_dir=tmp_path,
            queries_dir=tmp_path,
        )
        ctx.close()


class TestOrphanedChunksCheck:
    """Tests for the orphaned-chunks check."""

    def test_no_embeddings_db(self, check_context, monkeypatch):
        """Returns no findings when embeddings DB doesn't exist."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", True)

        check = OrphanedChunksCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_no_orphans(self, check_context, monkeypatch):
        """Returns no findings when all chunks match conversations."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", True)

        from siftd.storage.embeddings import open_embeddings_db, store_chunk

        embed_conn = open_embeddings_db(check_context.embed_db_path)

        # Get a real conversation ID from the test DB
        main_conn = check_context.get_db_conn()
        conv_ids = [
            row[0] for row in main_conn.execute("SELECT id FROM conversations").fetchall()
        ]
        assert len(conv_ids) > 0

        store_chunk(
            embed_conn, conv_ids[0], "exchange", "text",
            [1.0, 0.0], token_count=1, commit=True,
        )
        embed_conn.close()

        # Re-open via context so the check uses the populated DB
        check_context._embed_conn = None
        check = OrphanedChunksCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_detects_orphans(self, check_context, monkeypatch):
        """Reports orphaned chunks for conversations not in main DB."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "_EMBEDDINGS_AVAILABLE", True)

        from siftd.storage.embeddings import open_embeddings_db, store_chunk

        embed_conn = open_embeddings_db(check_context.embed_db_path)
        store_chunk(
            embed_conn, "nonexistent-conv", "exchange", "orphan",
            [1.0, 0.0], token_count=1, commit=True,
        )
        embed_conn.close()

        check_context._embed_conn = None
        check = OrphanedChunksCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "orphaned-chunks"
        assert findings[0].severity == "warning"
        assert findings[0].fix_available is True
        assert findings[0].context["chunk_count"] == 1
        assert findings[0].context["conversation_count"] == 1


class TestFreelistCheck:
    """Tests for the freelist check."""

    def test_no_freelist_pages(self, check_context):
        """Returns no findings when freelist is empty."""
        check = FreelistCheck()
        findings = check.run(check_context)
        # Fresh DB typically has no freelist pages
        assert isinstance(findings, list)
        # Either empty (no freelist) or has the expected structure
        for f in findings:
            assert f.check == "freelist"

    def test_freelist_with_pages(self, check_context):
        """Reports freelist pages when present."""
        # Create freelist pages by inserting then deleting data
        conn = check_context.get_db_conn()

        # We need a writable connection for this test
        import sqlite3
        write_conn = sqlite3.connect(check_context.db_path)
        write_conn.row_factory = sqlite3.Row

        # Insert a bunch of data to expand the DB
        write_conn.execute("""
            CREATE TABLE IF NOT EXISTS _test_temp (
                id INTEGER PRIMARY KEY,
                data TEXT
            )
        """)
        for i in range(1000):
            write_conn.execute(
                "INSERT INTO _test_temp (data) VALUES (?)",
                ("x" * 1000,)
            )
        write_conn.commit()

        # Delete everything to create freelist pages
        write_conn.execute("DELETE FROM _test_temp")
        write_conn.execute("DROP TABLE _test_temp")
        write_conn.commit()
        write_conn.close()

        # Close and reopen context connection to see changes
        check_context.close()

        check = FreelistCheck()
        findings = check.run(check_context)

        # Should have freelist pages now
        assert len(findings) == 1
        f = findings[0]
        assert f.check == "freelist"
        assert f.severity == "info"
        assert f.fix_available is False
        assert "free page" in f.message
        assert "reclaimed" in f.message
        assert f.context["freelist_count"] > 0
        assert "tip" in f.context
        assert "VACUUM" in f.context["tip"]

    def test_finding_structure(self, check_context):
        """Findings have correct structure."""
        check = FreelistCheck()
        findings = check.run(check_context)
        for f in findings:
            assert f.check == "freelist"
            assert f.severity == "info"
            assert f.fix_available is False
            assert "freelist_count" in f.context
            assert "page_count" in f.context
            assert "page_size" in f.context
            assert "wasted_bytes" in f.context
            assert "tip" in f.context


class TestSchemaCurrentCheck:
    """Tests for the schema-current check."""

    def test_fully_migrated_db_no_findings(self, check_context):
        """Returns no findings when database is fully migrated."""
        # The test_db fixture creates a fully migrated DB
        check = SchemaCurrentCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_detects_missing_error_column(self, tmp_path):
        """Reports finding when error column is missing from ingested_files."""
        import sqlite3

        # Create a minimal DB without the error column
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                file_hash TEXT NOT NULL,
                harness_id TEXT NOT NULL,
                conversation_id TEXT,
                ingested_at TEXT NOT NULL
            )
        """)
        # Create prompts table without CASCADE to trigger that check too
        conn.execute("""
            CREATE TABLE prompts (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                external_id TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        ctx = CheckContext(
            db_path=db_path,
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        try:
            check = SchemaCurrentCheck()
            findings = check.run(ctx)

            assert len(findings) == 1
            assert findings[0].check == "schema-current"
            assert findings[0].severity == "warning"
            assert "pending" in findings[0].message
            assert findings[0].fix_available is True
            assert findings[0].fix_command == "siftd ingest"
            assert "pending" in findings[0].context
            assert len(findings[0].context["pending"]) > 0
        finally:
            ctx.close()

    def test_finding_structure(self, tmp_path):
        """Findings have correct structure when migrations are pending."""
        import sqlite3

        # Create a bare-bones DB
        db_path = tmp_path / "minimal.db"
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE prompts (
                id TEXT PRIMARY KEY,
                conversation_id TEXT
            )
        """)
        conn.commit()
        conn.close()

        ctx = CheckContext(
            db_path=db_path,
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path,
            formatters_dir=tmp_path,
            queries_dir=tmp_path,
        )
        try:
            check = SchemaCurrentCheck()
            findings = check.run(ctx)

            assert len(findings) == 1
            f = findings[0]
            assert f.check == "schema-current"
            assert f.severity == "warning"
            assert f.fix_available is True
            assert f.fix_command == "siftd ingest"
            assert isinstance(f.context["pending"], list)
        finally:
            ctx.close()
