"""Tests for the doctor module."""

import tempfile
from pathlib import Path

import pytest

from strata.api import (
    CheckInfo,
    Finding,
    FixResult,
    list_checks,
    run_checks,
)
from strata.doctor.checks import (
    CheckContext,
    DropInsValidCheck,
    EmbeddingsStaleCheck,
    IngestPendingCheck,
    PricingGapsCheck,
)
from strata.storage.sqlite import (
    create_database,
    get_or_create_harness,
    get_or_create_model,
    get_or_create_workspace,
    insert_conversation,
    insert_prompt,
    insert_response,
    record_ingested_file,
)


@pytest.fixture
def test_db(tmp_path):
    """Create a test database with sample data."""
    db_path = tmp_path / "test.db"
    conn = create_database(db_path)

    harness_id = get_or_create_harness(conn, "test_harness", source="test", log_format="jsonl")
    workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
    model_id = get_or_create_model(conn, "claude-3-opus-20240229")

    conv_id = insert_conversation(
        conn,
        external_id="conv1",
        harness_id=harness_id,
        workspace_id=workspace_id,
        started_at="2024-01-15T10:00:00Z",
    )

    prompt_id = insert_prompt(conn, conv_id, "p1", "2024-01-15T10:00:00Z")
    insert_response(
        conn, conv_id, prompt_id, model_id, None, "r1", "2024-01-15T10:00:01Z",
        input_tokens=100, output_tokens=50
    )

    conn.commit()
    conn.close()

    return db_path


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
        assert "pricing-gaps" in names
        assert "drop-ins-valid" in names

    def test_check_info_has_required_fields(self):
        """CheckInfo has all required fields."""
        checks = list_checks()
        for check in checks:
            assert hasattr(check, "name")
            assert hasattr(check, "description")
            assert hasattr(check, "has_fix")
            assert isinstance(check.name, str)
            assert isinstance(check.description, str)
            assert isinstance(check.has_fix, bool)


class TestRunChecks:
    """Tests for run_checks()."""

    def test_run_all_checks(self, test_db):
        """run_checks runs all checks when no filter specified."""
        findings = run_checks(db_path=test_db)
        # Should not raise, findings may be empty or have items
        assert isinstance(findings, list)
        assert all(isinstance(f, Finding) for f in findings)

    def test_run_specific_check(self, test_db):
        """run_checks can run a specific check by name."""
        findings = run_checks(checks=["drop-ins-valid"], db_path=test_db)
        # All findings should be from the specified check
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


class TestIngestPendingCheck:
    """Tests for the ingest-pending check."""

    def test_no_pending_files(self, check_context):
        """Returns empty findings when all discovered files are ingested."""
        # The test DB has no adapter-discovered files, so nothing should be pending
        # (adapters discover from real filesystem locations which won't exist in tests)
        check = IngestPendingCheck()
        findings = check.run(check_context)
        # With no real adapter locations, we expect no findings or adapter discover failures
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

    def test_no_embeddings_db(self, check_context):
        """Reports info when embeddings DB doesn't exist."""
        check = EmbeddingsStaleCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "not found" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "strata ask --index"

    def test_stale_conversations(self, check_context):
        """Reports stale conversations when embeddings DB exists but is empty."""
        from strata.storage.embeddings import open_embeddings_db

        # Create empty embeddings DB
        embed_conn = open_embeddings_db(check_context.embed_db_path)
        embed_conn.close()

        check = EmbeddingsStaleCheck()
        findings = check.run(check_context)

        # Should find stale conversations (main DB has 1 conv, embeddings has 0)
        assert len(findings) == 1
        assert findings[0].check == "embeddings-stale"
        assert "1 conversation" in findings[0].message
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
        # Pricing table is auto-created, so we may have findings for models without pricing
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
        # Create an invalid adapter file
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
NAME = "test_adapter"
DEFAULT_LOCATIONS = ["~/test"]
DEDUP_STRATEGY = "file"
HARNESS_SOURCE = "test"

def discover():
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

    def test_skips_underscore_files(self, check_context):
        """Skips files starting with underscore."""
        adapter_file = check_context.adapters_dir / "_private.py"
        adapter_file.write_text("# Should be ignored\n")

        check = DropInsValidCheck()
        findings = check.run(check_context)
        assert findings == []


class TestFindingDataclass:
    """Tests for Finding dataclass."""

    def test_required_fields(self):
        """Finding requires essential fields."""
        finding = Finding(
            check="test",
            severity="info",
            message="Test message",
            fix_available=False,
        )
        assert finding.check == "test"
        assert finding.severity == "info"
        assert finding.message == "Test message"
        assert finding.fix_available is False
        assert finding.fix_command is None
        assert finding.context is None

    def test_optional_fields(self):
        """Finding accepts optional fields."""
        finding = Finding(
            check="test",
            severity="warning",
            message="Test",
            fix_available=True,
            fix_command="strata fix",
            context={"count": 5},
        )
        assert finding.fix_command == "strata fix"
        assert finding.context == {"count": 5}


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
        # Connections should be None initially
        assert ctx._db_conn is None
        assert ctx._embed_conn is None

        # Access triggers loading
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
        # Should not raise
        ctx.close()
