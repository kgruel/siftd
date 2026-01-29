"""Tests for the doctor module."""

from pathlib import Path

import pytest

from siftd.api import (
    CheckInfo,
    Finding,
    FixResult,
    list_checks,
    run_checks,
)
from siftd.doctor.checks import (
    CheckContext,
    DropInsValidCheck,
    EmbeddingsStaleCheck,
    IngestPendingCheck,
    OrphanedChunksCheck,
    PricingGapsCheck,
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
            assert isinstance(check.name, str)
            assert isinstance(check.description, str)
            assert isinstance(check.has_fix, bool)


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

    def test_skips_underscore_files(self, check_context):
        """Skips files starting with underscore."""
        adapter_file = check_context.adapters_dir / "_private.py"
        adapter_file.write_text("# Should be ignored\n")

        check = DropInsValidCheck()
        findings = check.run(check_context)
        assert findings == []


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
