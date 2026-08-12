"""Tests for the doctor module."""

import os
import sqlite3
import stat
import threading
from contextlib import closing

import pytest

from siftd.api import (
    CheckInfo,
    Finding,
    list_checks,
    run_checks,
)
from siftd.doctor.checks import (
    AdapterStaleCheck,
    CheckContext,
    ConfigValidCheck,
    CostCoverageCheck,
    DbBlobOrphansCheck,
    DbBlobRefcountDriftCheck,
    DbFkIntegrityCheck,
    DbTriggerPresenceCheck,
    DropInsValidCheck,
    EmbedConfigCheck,
    EmbeddingsStaleCheck,
    FreelistCheck,
    FtsIntegrityCheck,
    FtsStaleCheck,
    IngestPendingCheck,
    OrphanedChunksCheck,
    SchemaCurrentCheck,
    WorkspaceIdentityCheck,
    _connect_read_only,
)
from conftest import skip_if_root


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
        assert "drop-ins-valid" in names
        # pricing-gaps was dissolved into the caveats producer registry
        # (siftd.api.caveats._pricing_caveats); verify the check is GONE so
        # the check list and caveats layer don't both surface the same fact.
        assert "pricing-gaps" not in names
        assert "freelist" in names
        assert "schema-current" in names
        # New P1 checks
        assert "fts-stale" in names
        assert "fts-integrity" in names
        assert "config-valid" in names
        assert "embed-config" in names
        # embeddings-compat is from main (replaces embeddings-dimension-mismatch)
        assert "embeddings-compat" in names
        assert "workspace-identity" in names
        assert "adapter-stale" in names
        # Deep checks
        assert "db-fk-integrity" in names
        assert "db-blob-refcount-drift" in names
        assert "db-blob-orphans" in names
        assert "db-trigger-presence" in names

    def test_has_fix_matches_class_attribute(self):
        """has_fix in CheckInfo matches the class attribute on each check."""
        checks = list_checks()
        by_name = {c.name: c.has_fix for c in checks}
        assert by_name["ingest-pending"] is True
        assert by_name["ingest-errors"] is False
        assert by_name["adapter-stale"] is True
        assert by_name["embeddings-stale"] is True
        assert by_name["orphaned-chunks"] is True
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
            assert check.cost in ("fast", "slow", "deep")

    def test_requires_db_attribute(self):
        """requires_db in CheckInfo matches expected values."""
        checks = list_checks()
        by_name = {c.name: c.requires_db for c in checks}
        # Checks that need the database
        assert by_name["ingest-pending"] is True
        assert by_name["ingest-errors"] is True
        assert by_name["embeddings-stale"] is True
        assert by_name["orphaned-chunks"] is True
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
        assert by_name["drop-ins-valid"] is False
        assert by_name["embeddings-available"] is False
        assert by_name["freelist"] is False
        assert by_name["schema-current"] is False

    def test_cost_attribute(self):
        """cost in CheckInfo matches expected values."""
        checks = list_checks()
        by_name = {c.name: c.cost for c in checks}
        # ingest-pending and adapter-stale are slow (run discover())
        assert by_name["ingest-pending"] == "slow"
        assert by_name["adapter-stale"] == "slow"
        # Everything else is fast
        assert by_name["ingest-errors"] == "fast"
        assert by_name["embeddings-stale"] == "fast"
        assert by_name["orphaned-chunks"] == "fast"
        assert by_name["drop-ins-valid"] == "fast"
        assert by_name["embeddings-available"] == "fast"
        assert by_name["freelist"] == "fast"
        assert by_name["schema-current"] == "fast"
        # Deep checks
        assert by_name["db-fk-integrity"] == "deep"
        assert by_name["db-blob-refcount-drift"] == "deep"
        assert by_name["db-blob-orphans"] == "deep"
        assert by_name["db-trigger-presence"] == "deep"


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

    def test_callback_exception_does_not_abort_run(self, test_db, caplog):
        """A failing on_check_done callback doesn't abort the doctor run."""
        import logging

        calls = []

        def bad_callback(name, results):
            calls.append(name)
            raise RuntimeError("renderer crashed")

        with caplog.at_level(logging.WARNING, logger="siftd.doctor.runner"):
            findings = run_checks(
                checks=["drop-ins-valid", "config-valid"],
                db_path=test_db,
                on_check_done=bad_callback,
            )

        assert isinstance(findings, list)
        assert len(calls) == 2
        assert any("on_check_done" in r.message for r in caplog.records)

    def test_callback_none_does_not_raise(self, test_db):
        """on_check_done=None runs without error."""
        findings = run_checks(checks=["drop-ins-valid"], db_path=test_db, on_check_done=None)
        assert isinstance(findings, list)

    def test_fast_flag_filters_to_fast_checks(self, test_db):
        """run_checks with fast=True runs only fast checks, skipping slow and deep."""
        from siftd.api import list_checks

        findings = run_checks(db_path=test_db, fast=True)
        assert isinstance(findings, list)

        checks = list_checks()
        fast_check_names = {c.name for c in checks if c.cost == "fast"}
        slow_check_names = {c.name for c in checks if c.cost == "slow"}
        deep_check_names = {c.name for c in checks if c.cost == "deep"}

        # All findings should be from fast checks
        finding_check_names = {f.check for f in findings}
        assert finding_check_names.issubset(fast_check_names), \
            f"Found non-fast checks: {finding_check_names - fast_check_names}"

        # Verify that slow and deep checks are not included
        assert not finding_check_names.intersection(slow_check_names), \
            f"Found slow checks when using fast=True: {finding_check_names & slow_check_names}"
        assert not finding_check_names.intersection(deep_check_names), \
            f"Found deep checks when using fast=True: {finding_check_names & deep_check_names}"


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


class TestAdapterStaleCheck:
    """Tests for the adapter-stale check."""

    INGESTED_MTIME = 1_700_000_000.0

    @pytest.fixture
    def stale_db(self, tmp_path):
        """DB with one harness whose newest ingested mtime is INGESTED_MTIME."""
        from siftd.storage.sqlite import (
            create_database,
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            record_ingested_file,
        )

        db_path = tmp_path / "stale.db"
        conn = create_database(db_path)
        harness_id = get_or_create_harness(conn, "fake_adapter", source="test", log_format="jsonl")
        workspace_id = get_or_create_workspace(conn, "/test/project", "2024-01-01T10:00:00Z")
        conv_id = insert_conversation(
            conn,
            external_id="c1",
            harness_id=harness_id,
            workspace_id=workspace_id,
            started_at="2024-01-15T10:00:00Z",
        )
        record_ingested_file(
            conn, "/logs/session.jsonl", "hash1", conv_id, file_mtime=self.INGESTED_MTIME
        )
        conn.commit()
        conn.close()
        return db_path

    def _ctx(self, db_path, tmp_path):
        return CheckContext(
            db_path=db_path,
            embed_db_path=tmp_path / "embeddings.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

    def _fake_plugins(self, monkeypatch, name, discover_fn):
        import types

        from siftd.plugin_discovery import PluginInfo

        module = types.SimpleNamespace(discover=discover_fn)
        plugins = [PluginInfo(name=name, origin="builtin", module=module)]
        import siftd.adapters.registry as registry

        monkeypatch.setattr(registry, "load_all_adapters", lambda **kw: plugins)

    def _log_file(self, tmp_path, mtime):
        import os

        from siftd.domain.source import Source

        log = tmp_path / "session.jsonl"
        log.write_text("{}\n")
        os.utime(log, (mtime, mtime))
        return Source(kind="file", location=log)

    def _set_ingested_path(self, db_path, path):
        from siftd.storage.sqlite import open_database

        conn = open_database(db_path)
        conn.execute(
            "UPDATE ingested_files SET path = ? WHERE path = '/logs/session.jsonl'",
            (str(path),),
        )
        conn.commit()
        conn.close()

    def test_stale_adapter_warns(self, tmp_path, stale_db, monkeypatch):
        """Disk file newer than last ingest produces a warning with fix."""
        source = self._log_file(tmp_path, self.INGESTED_MTIME + 3600)
        self._set_ingested_path(stale_db, source.location)
        self._fake_plugins(monkeypatch, "fake_adapter", lambda: [source])

        ctx = self._ctx(stale_db, tmp_path)
        try:
            findings = AdapterStaleCheck().run(ctx)
        finally:
            ctx.close()

        assert len(findings) == 1
        f = findings[0]
        assert f.check == "adapter-stale"
        assert f.severity == "warning"
        assert f.fix_available is True
        assert f.fix_command == "siftd ingest"
        assert f.context["adapter"] == "fake_adapter"
        assert f.context["gap_seconds"] == pytest.approx(3600, abs=2)

    def test_fresh_adapter_silent(self, tmp_path, stale_db, monkeypatch):
        """Disk mtime equal to ingested mtime produces no findings."""
        source = self._log_file(tmp_path, self.INGESTED_MTIME)
        self._set_ingested_path(stale_db, source.location)
        self._fake_plugins(monkeypatch, "fake_adapter", lambda: [source])

        ctx = self._ctx(stale_db, tmp_path)
        try:
            findings = AdapterStaleCheck().run(ctx)
        finally:
            ctx.close()

        assert findings == []

    def test_older_file_change_not_hidden_by_newer_ingested_file(
        self, tmp_path, stale_db, monkeypatch
    ):
        """Each path is compared independently, not through adapter-wide maxima."""
        import os

        from siftd.domain.source import Source
        from siftd.storage.sqlite import get_or_create_harness, open_database, record_failed_file

        older = tmp_path / "session.jsonl"
        older.write_text("changed\n")
        os.utime(older, (self.INGESTED_MTIME + 100, self.INGESTED_MTIME + 100))

        newer = tmp_path / "newer.jsonl"
        newer.write_text("{}\n")
        os.utime(newer, (self.INGESTED_MTIME + 200, self.INGESTED_MTIME + 200))

        conn = open_database(stale_db)
        harness_id = get_or_create_harness(conn, "fake_adapter")
        record_failed_file(
            conn,
            str(older),
            "hash-older",
            harness_id,
            "fixture",
            file_mtime=self.INGESTED_MTIME,
        )
        record_failed_file(
            conn,
            str(newer),
            "hash2",
            harness_id,
            "fixture",
            file_mtime=self.INGESTED_MTIME + 200,
        )
        conn.commit()
        conn.close()

        self._fake_plugins(
            monkeypatch,
            "fake_adapter",
            lambda: [
                Source(kind="file", location=older),
                Source(kind="file", location=newer),
            ],
        )
        ctx = self._ctx(stale_db, tmp_path)
        try:
            findings = AdapterStaleCheck().run(ctx)
        finally:
            ctx.close()

        assert len(findings) == 1
        assert findings[0].context["path"] == str(older)
        assert findings[0].context["stale_file_count"] == 1

    def test_adapter_without_db_presence_skipped(self, tmp_path, stale_db, monkeypatch):
        """Adapters with no ingested rows are skipped — discover() never runs."""
        def _boom():
            raise AssertionError("discover() must not be called")

        self._fake_plugins(monkeypatch, "other_adapter", _boom)

        ctx = self._ctx(stale_db, tmp_path)
        try:
            findings = AdapterStaleCheck().run(ctx)
        finally:
            ctx.close()

        assert findings == []

    def test_discover_failure_silent(self, tmp_path, stale_db, monkeypatch):
        """discover() failures are skipped (ingest-pending reports them)."""
        def _boom():
            raise RuntimeError("no home dir")

        self._fake_plugins(monkeypatch, "fake_adapter", _boom)

        ctx = self._ctx(stale_db, tmp_path)
        try:
            findings = AdapterStaleCheck().run(ctx)
        finally:
            ctx.close()

        assert findings == []

    def test_missing_paths_tolerated(self, tmp_path, stale_db, monkeypatch):
        """Sources whose path no longer exists are ignored."""
        from siftd.domain.source import Source

        missing = Source(kind="file", location=tmp_path / "gone.jsonl")
        self._fake_plugins(monkeypatch, "fake_adapter", lambda: [missing])

        ctx = self._ctx(stale_db, tmp_path)
        try:
            findings = AdapterStaleCheck().run(ctx)
        finally:
            ctx.close()

        assert findings == []

    def test_discovery_shared_across_checks(self, tmp_path, stale_db, monkeypatch):
        """Both slow-lane checks reuse one discover() pass via the context."""
        source = self._log_file(tmp_path, self.INGESTED_MTIME)
        self._set_ingested_path(stale_db, source.location)
        calls = []

        def _discover():
            calls.append(1)
            return [source]

        self._fake_plugins(monkeypatch, "fake_adapter", _discover)

        ctx = self._ctx(stale_db, tmp_path)
        try:
            AdapterStaleCheck().run(ctx)
            IngestPendingCheck().run(ctx)
        finally:
            ctx.close()

        assert len(calls) == 1

    def test_discovery_failure_shared_across_checks(self, tmp_path, stale_db, monkeypatch):
        """A cached discover() failure reaches both checks with their own policies."""
        def _boom():
            raise RuntimeError("no home dir")

        self._fake_plugins(monkeypatch, "fake_adapter", _boom)

        ctx = self._ctx(stale_db, tmp_path)
        try:
            stale_findings = AdapterStaleCheck().run(ctx)
            pending_findings = IngestPendingCheck().run(ctx)
        finally:
            ctx.close()

        assert stale_findings == []  # adapter-stale stays silent on failures
        assert len(pending_findings) == 1  # ingest-pending reports them
        assert "discover() failed" in pending_findings[0].message


@pytest.mark.embeddings
class TestEmbeddingsStaleCheck:
    """Tests for the embeddings-stale check."""

    def test_no_embeddings_db(self, check_context, monkeypatch):
        """Reports info when embeddings DB doesn't exist."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus("fastembed", True, "ok"))

        check = EmbeddingsStaleCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "not found" in findings[0].message
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd embed"

    def test_stale_conversations(self, check_context, monkeypatch):
        """Reports stale conversations when embeddings DB exists but is empty."""
        pytest.importorskip("numpy")
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus("fastembed", True, "ok"))

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


class TestEmbedConfigCheck:
    """Tests for the embed-config check."""

    def test_finding_structure(self):
        """Check has correct attributes."""
        check = EmbedConfigCheck()
        assert check.name == "embed-config"
        assert check.has_fix is True
        assert check.requires_db is False
        assert check.requires_embed_db is False
        assert check.cost == "fast"

    def test_nothing_configured_no_db_is_info(self, check_context, monkeypatch, tmp_path):
        """No embed.backend set and no embeddings DB yet — informational, not an error."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("")
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "no embedding backend configured")
        )

        findings = EmbedConfigCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "not set up" in findings[0].message
        assert findings[0].fix_available is True

    def test_nothing_configured_but_db_exists_is_silent(self, check_context, monkeypatch, tmp_path):
        """No backend configured, but an embeddings DB already exists — the missing-extra
        case belongs to embeddings-available, not this check; avoid duplicate noise."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("")
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)
        check_context.embed_db_path.touch()

        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "no embedding backend configured")
        )

        assert EmbedConfigCheck().run(check_context) == []

    def test_configured_backend_unusable_is_warning(self, check_context, monkeypatch, tmp_path):
        """embed.backend set but not usable (e.g. missing extra) — surfaced regardless
        of whether an embeddings DB has ever been built."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "fastembed"\n')
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail,
            "embedding_status",
            lambda: avail.EmbedStatus(
                None,
                False,
                'embed.backend = "fastembed" but the [embed] extra is not installed; '
                "run `siftd install embed`",
            ),
        )

        findings = EmbedConfigCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "unusable" in findings[0].message
        assert findings[0].fix_command == "siftd install embed"

    def test_off_backend_is_silent(self, check_context, monkeypatch, tmp_path):
        """embed.backend = 'off' is an explicit opt-out, not a misconfiguration."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "off"\n')
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus(None, False, 'embeddings disabled (embed.backend = "off")')
        )

        assert EmbedConfigCheck().run(check_context) == []

    def test_remote_backend_missing_api_key_is_warning(self, check_context, monkeypatch, tmp_path):
        """A cloud preset with no api_key set will 401 at request time — flagged early."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "voyage"\n')
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.egress as egress_mod
        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus("remote:voyage", True, "remote backend (voyage-4-lite)")
        )
        monkeypatch.setattr(egress_mod, "egress_notice_pending", lambda embed_db_path=None: None)

        findings = EmbedConfigCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "api_key" in findings[0].message
        assert findings[0].context["backend"] == "voyage"

    def test_remote_backend_with_api_key_no_finding(self, check_context, monkeypatch, tmp_path):
        """A cloud preset with an api_key set draws no missing-key warning."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "voyage"\napi_key = "sk-literal"\n')
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.egress as egress_mod
        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus("remote:voyage", True, "remote backend (voyage-4-lite)")
        )
        monkeypatch.setattr(egress_mod, "egress_notice_pending", lambda embed_db_path=None: None)

        assert EmbedConfigCheck().run(check_context) == []

    def test_self_hosted_preset_no_key_required(self, check_context, monkeypatch, tmp_path):
        """ollama/custom presets are self-hosted by design — no key expected."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "ollama"\nmodel = "nomic-embed-text"\n')
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.egress as egress_mod
        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus("remote:ollama", True, "remote backend (nomic-embed-text)")
        )
        monkeypatch.setattr(egress_mod, "egress_notice_pending", lambda embed_db_path=None: None)

        assert EmbedConfigCheck().run(check_context) == []

    def test_egress_notice_pending_is_info(self, check_context, monkeypatch, tmp_path):
        """A pending first-egress disclosure surfaces as an actionable info finding."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "voyage"\napi_key = "sk-literal"\n')
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus("remote:voyage", True, "remote backend (voyage-4-lite)")
        )

        import siftd.embeddings.egress as egress_mod

        monkeypatch.setattr(
            egress_mod,
            "egress_notice_pending",
            lambda embed_db_path=None: "semantic indexing sends conversation content to voyage",
        )

        findings = EmbedConfigCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "Pending first-egress disclosure" in findings[0].message
        assert findings[0].fix_command == "siftd embed"

    def test_no_egress_notice_no_finding(self, check_context, monkeypatch, tmp_path):
        """A usable, already-notified (or local) backend draws no findings."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "fastembed"\n')
        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        monkeypatch.setattr("siftd.config.config_file", lambda: config_path)

        import siftd.embeddings.availability as avail

        monkeypatch.setattr(
            avail, "embedding_status", lambda: avail.EmbedStatus("fastembed", True, "local fastembed backend installed")
        )

        import siftd.embeddings.egress as egress_mod

        monkeypatch.setattr(egress_mod, "egress_notice_pending", lambda embed_db_path=None: None)

        assert EmbedConfigCheck().run(check_context) == []


class TestCostCoverageCheck:
    """Tests for the cost-coverage check."""

    def test_no_stats_table_returns_empty(self, tmp_path):
        """Returns no findings when conversation_stats table does not exist."""
        from siftd.storage.sqlite import create_database

        db_path = tmp_path / "bare.db"
        conn = create_database(db_path)
        # Drop the stats table so the check has nothing to query
        conn.execute("DROP TABLE IF EXISTS conversation_stats")
        conn.commit()
        conn.close()

        embed_db = tmp_path / "embeddings.db"
        ctx = CheckContext(
            db_path=db_path,
            embed_db_path=embed_db,
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        for d in [tmp_path / "adapters", tmp_path / "formatters", tmp_path / "queries"]:
            d.mkdir()

        check = CostCoverageCheck()
        findings = check.run(ctx)
        ctx.close()
        assert findings == []

    def _make_ctx_with_stats(self, tmp_path, rows):
        """Create a CheckContext pre-populated with given conversation_stats rows."""
        from siftd.storage.sqlite import create_database

        db_path = tmp_path / "cost_test.db"
        conn = create_database(db_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executemany(
            "INSERT INTO conversation_stats "
            "(conversation_id, prompt_count, response_count, total_tokens, cost) "
            "VALUES (?, 1, 1, 100, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        for d in [tmp_path / "adapters", tmp_path / "formatters", tmp_path / "queries"]:
            d.mkdir(exist_ok=True)
        return CheckContext(
            db_path=db_path,
            embed_db_path=tmp_path / "embeddings.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

    def test_no_findings_when_coverage_adequate(self, tmp_path):
        """No findings when cost coverage is above threshold."""
        ctx = self._make_ctx_with_stats(
            tmp_path, [("c1", 0.5), ("c2", 1.0), ("c3", 1.5)]
        )
        check = CostCoverageCheck()
        findings = check.run(ctx)
        ctx.close()
        assert findings == []

    def test_warning_when_coverage_low(self, tmp_path):
        """Warning finding when fewer than 25% of conversations have cost."""
        # 1 with cost, 9 without (10% coverage — below 25% threshold)
        rows = [("c1", 0.5)] + [(f"c{i}", None) for i in range(2, 11)]
        ctx = self._make_ctx_with_stats(tmp_path, rows)
        check = CostCoverageCheck()
        findings = check.run(ctx)
        ctx.close()

        assert len(findings) == 1
        f = findings[0]
        assert f.check == "cost-coverage"
        assert f.severity == "warning"
        assert f.fix_available is False
        assert "10%" in f.message or "10" in f.message

    def test_registered_in_builtin_checks(self):
        """CostCoverageCheck is in the BUILTIN_CHECKS registry."""
        checks = list_checks()
        names = {c.name for c in checks}
        assert "cost-coverage" in names


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
        assert ctx._conns == {}

        conn = ctx.get_db_conn()
        assert conn is not None
        assert list(ctx._conns.values()) == [conn]

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

    def _assert_one_conn_per_thread(self, getter, threads=8):
        """Assert ``getter`` returns one connection per thread, reused within it.

        Dedicated threads rather than a pool: a pool is free to run several
        tasks on one worker, and two tasks sharing a thread legitimately share
        that thread's connection — which would make a distinctness assertion
        depend on the pool's scheduling rather than on the invariant.

        All ``threads`` threads are held alive at the barrier while they call,
        because the invariant is about *concurrently live* threads. Letting
        them run to completion one after another would let a finished thread's
        identity be recycled, and the assertion would then be measuring how
        fast the machine is rather than what the cache keys on.

        Returns the per-thread connections so callers can assert on them.
        """
        barrier = threading.Barrier(threads)
        pairs: list[tuple] = []
        lock = threading.Lock()

        def two_calls():
            barrier.wait()
            pair = (getter(), getter())
            with lock:
                pairs.append(pair)
            barrier.wait()

        workers = [threading.Thread(target=two_calls) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        for first, second in pairs:
            assert first is second, "repeat calls on one thread should reuse its connection"
        per_thread = [first for first, _ in pairs]
        assert len(set(per_thread)) == len(per_thread) == threads, (
            "each concurrently live thread must get its own connection"
        )
        return per_thread

    def test_db_conn_is_one_per_thread(self, test_db, tmp_path):
        """Each thread gets its own connection, and close() releases them all.

        A single sqlite3.Connection shared across the runner's thread pool
        produced silently wrong query results — see
        test_concurrent_run_reports_fts_drift_every_time.
        """
        ctx = CheckContext(
            db_path=test_db,
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        per_thread = self._assert_one_conn_per_thread(ctx.get_db_conn)
        assert set(ctx._conns.values()) == set(per_thread)

        ctx.close()
        assert ctx._conns == {}
        for conn in per_thread:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_embed_conn_is_one_per_thread(self, test_db, tmp_path):
        """get_embed_conn() follows the same per-thread rule as get_db_conn()."""
        # Create the embeddings DB first (get_embed_conn opens in read-only mode)
        embed_db = tmp_path / "embed.db"
        seed = sqlite3.connect(embed_db)
        seed.close()

        ctx = CheckContext(
            db_path=test_db,
            embed_db_path=embed_db,
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        self._assert_one_conn_per_thread(ctx.get_embed_conn)
        ctx.close()

    def test_sequential_threads_do_not_inherit_a_dead_thread_conn(self, test_db, tmp_path):
        """A finished thread's connection is never handed to its successor.

        CPython recycles thread idents once a thread dies, so keying the cache
        on ``threading.get_ident()`` gave a new thread whatever its dead
        predecessor had opened. Harmless in itself — sequential use — but it
        made "one connection per thread" true only by accident, and how often
        it happened depended on how fast threads finished. Keyed on the Thread
        object instead, so this holds by construction.
        """
        ctx = CheckContext(
            db_path=test_db,
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        seen = []

        def once():
            seen.append(ctx.get_db_conn())

        # Strictly sequential: each thread is dead before the next one starts,
        # which is exactly when ident recycling happens.
        for _ in range(8):
            w = threading.Thread(target=once)
            w.start()
            w.join()

        assert len(set(seen)) == 8, "a new thread inherited a dead thread's connection"

        ctx.close()

    def test_two_databases_get_separate_conns_on_one_thread(self, test_db, tmp_path):
        """The per-thread cache keys on the database too, not just the thread."""
        embed_db = tmp_path / "embed.db"
        sqlite3.connect(embed_db).close()
        ctx = CheckContext(
            db_path=test_db,
            embed_db_path=embed_db,
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )

        assert ctx.get_db_conn() is not ctx.get_embed_conn()
        assert len(ctx._conns) == 2

        ctx.close()

    def test_read_conn_sees_commits_a_writer_has_not_checkpointed(self, check_context, test_db):
        """Doctor's reads are not pinned to the last-checkpointed snapshot.

        An immutable connection ignores the ``-wal`` file outright, so against a
        database a live ``serve`` or concurrent ``ingest`` has committed to but
        not checkpointed, checks answered from a stale snapshot and reported it
        as current. Deterministic, not racy: no checkpoint, no visibility, ever.
        """
        with closing(sqlite3.connect(test_db)) as writer:
            writer.execute("PRAGMA journal_mode = WAL")
            writer.execute("CREATE TABLE wal_probe (x INTEGER)")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            conn = check_context.get_db_conn()
            assert conn.execute("SELECT count(*) FROM wal_probe").fetchone()[0] == 0

            writer.execute("INSERT INTO wal_probe VALUES (1)")
            writer.commit()

            # Guard against the test going vacuous: it only means anything
            # while the commit is still in the WAL and nowhere else.
            wal = test_db.parent / f"{test_db.name}-wal"
            assert wal.exists() and wal.stat().st_size > 0, "commit was checkpointed away"

            assert conn.execute("SELECT count(*) FROM wal_probe").fetchone()[0] == 1

    @skip_if_root
    def test_read_conn_falls_back_to_immutable_on_read_only_media(self, tmp_path):
        """Immutability is derived from the medium, not asserted by the caller.

        Simulated by making the database's *directory* unwritable, which is what
        stops the ``-shm`` sidecar from being created — read-only media in the
        only form the filesystem lets a test reproduce.
        """
        media = tmp_path / "media"
        media.mkdir()
        db = media / "frozen.db"
        seed = sqlite3.connect(db)
        seed.execute("PRAGMA journal_mode = WAL")
        seed.execute("CREATE TABLE t (x INTEGER)")
        seed.execute("INSERT INTO t VALUES (1)")
        seed.commit()
        seed.close()

        os.chmod(db, stat.S_IRUSR)
        os.chmod(media, stat.S_IRUSR | stat.S_IXUSR)
        try:
            # Positive control: without the fallback there is nothing to read.
            plain = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            with pytest.raises(sqlite3.OperationalError):
                plain.execute("SELECT count(*) FROM t")
            plain.close()

            conn = _connect_read_only(db)
            try:
                assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
            finally:
                conn.close()
            assert not (media / "frozen.db-shm").exists(), "probe left a sidecar behind"
        finally:
            os.chmod(media, stat.S_IRWXU)
            os.chmod(db, stat.S_IRUSR | stat.S_IWUSR)

    def test_concurrent_run_reports_fts_drift_every_time(self, test_db):
        """A full concurrent doctor run reports the same FTS drift on every pass.

        Regression for the shared-connection race: the runner fans checks out
        over a thread pool, and fts-integrity opens its own *write* connection
        to the same file. With one shared read connection the fts-stale query
        intermittently returned 0 missing rows instead of 4 — no error, just a
        wrong answer, so `siftd doctor` silently reported a healthy index. The
        loop is the instrument: a single pass passed even with the bug (the
        observed miss rate was roughly one run in seven).
        """
        from siftd.api import run_checks

        for i in range(30):
            findings = run_checks(db_path=test_db)
            missing = [f for f in findings if f.check == "fts-stale"]
            assert missing, f"fts-stale finding vanished on pass {i}"
            assert missing[0].context["missing_count"] == 4


@pytest.mark.embeddings
class TestOrphanedChunksCheck:
    """Tests for the orphaned-chunks check."""

    def test_no_embeddings_db(self, check_context, monkeypatch):
        """Returns no findings when embeddings DB doesn't exist."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus("fastembed", True, "ok"))

        check = OrphanedChunksCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_no_orphans(self, check_context, monkeypatch):
        """Returns no findings when all chunks match conversations."""
        pytest.importorskip("numpy")
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus("fastembed", True, "ok"))

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
        check_context.close()
        check = OrphanedChunksCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_detects_orphans(self, check_context, monkeypatch):
        """Reports orphaned chunks for conversations not in main DB."""
        pytest.importorskip("numpy")
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus("fastembed", True, "ok"))

        from siftd.storage.embeddings import open_embeddings_db, store_chunk

        embed_conn = open_embeddings_db(check_context.embed_db_path)
        store_chunk(
            embed_conn, "nonexistent-conv", "exchange", "orphan",
            [1.0, 0.0], token_count=1, commit=True,
        )
        embed_conn.close()

        check_context.close()
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


class TestFtsStaleCheck:
    """Tests for the fts-stale check."""

    def test_no_fts_table(self, tmp_path):
        """Returns no findings when FTS table doesn't exist yet."""
        import sqlite3

        # Create a minimal DB without FTS table
        db_path = tmp_path / "no_fts.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE prompt_content (id TEXT PRIMARY KEY, block_type TEXT)")
        conn.execute("CREATE TABLE response_content (id TEXT PRIMARY KEY, block_type TEXT)")
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
            check = FtsStaleCheck()
            findings = check.run(ctx)
            # schema-current check will catch missing FTS, so this returns empty
            assert findings == []
        finally:
            ctx.close()

    def test_fts_in_sync(self, check_context):
        """Returns no findings when FTS is in sync with content."""
        from siftd.storage.fts import ensure_fts_table, rebuild_fts_index

        conn = check_context.get_db_conn()

        # Need a writable connection
        import sqlite3
        write_conn = sqlite3.connect(check_context.db_path)
        write_conn.row_factory = sqlite3.Row
        ensure_fts_table(write_conn)
        rebuild_fts_index(write_conn, commit=True)
        write_conn.close()

        # Close and reopen to see changes
        check_context.close()

        check = FtsStaleCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_detects_orphaned_fts_entries(self, check_context):
        """Reports findings when FTS has entries not in content tables."""
        from siftd.storage.fts import ensure_fts_table

        import sqlite3
        write_conn = sqlite3.connect(check_context.db_path)
        write_conn.row_factory = sqlite3.Row
        ensure_fts_table(write_conn)

        # Insert orphaned FTS entry (event_content_id doesn't exist)
        write_conn.execute("""
            INSERT INTO content_fts (text_content, event_content_id, event_id, conversation_id)
            VALUES ('orphan text', 'nonexistent-ec-id', 'nonexistent-e-id', 'some-conv')
        """)
        write_conn.commit()
        write_conn.close()

        check_context.close()

        check = FtsStaleCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "fts-stale"
        assert findings[0].severity == "warning"
        assert findings[0].fix_available is True
        assert findings[0].fix_command == "siftd ingest --rebuild-fts"
        assert findings[0].context["orphaned_count"] == 1

    def test_detects_missing_fts_entries(self, check_context):
        """Reports findings when content exists but not in FTS."""
        from siftd.storage.fts import ensure_fts_table

        import sqlite3
        write_conn = sqlite3.connect(check_context.db_path)
        write_conn.row_factory = sqlite3.Row
        ensure_fts_table(write_conn)
        # Don't call rebuild_fts_index, so content exists but FTS is empty
        write_conn.commit()
        write_conn.close()

        check_context.close()

        check = FtsStaleCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "fts-stale"
        assert "missing" in findings[0].message
        assert findings[0].context["missing_count"] > 0


class TestFtsIntegrityCheck:
    """Tests for the fts-integrity check."""

    def test_no_fts_table(self, tmp_path):
        """Returns no findings when FTS table doesn't exist."""
        import sqlite3

        # Create a minimal DB without FTS table
        db_path = tmp_path / "no_fts.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
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
            check = FtsIntegrityCheck()
            findings = check.run(ctx)
            assert findings == []
        finally:
            ctx.close()

    def test_healthy_fts(self, check_context):
        """Returns no findings when FTS integrity is OK."""
        from siftd.storage.fts import ensure_fts_table, rebuild_fts_index

        import sqlite3
        write_conn = sqlite3.connect(check_context.db_path)
        write_conn.row_factory = sqlite3.Row
        ensure_fts_table(write_conn)
        rebuild_fts_index(write_conn, commit=True)
        write_conn.close()

        check_context.close()

        check = FtsIntegrityCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_finding_structure(self, check_context):
        """Check has correct attributes."""
        check = FtsIntegrityCheck()
        assert check.name == "fts-integrity"
        assert check.has_fix is True
        assert check.requires_db is True
        assert check.cost == "fast"

    def test_stale_schema_skips_without_migrating(self, tmp_path):
        """C03: a diagnostic must not migrate the live DB.

        When the on-disk schema is below SCHEMA_VERSION the check returns an
        info finding and leaves the file untouched, rather than taking the
        write path (which would migrate, back up, and switch to WAL).
        """
        import sqlite3

        from siftd.storage.sqlite import create_database

        db_path = tmp_path / "stale.db"
        conn = create_database(db_path)  # full current-schema DB (has content_fts)
        conn.close()

        # Pretend the file is from an older schema version.
        stale = sqlite3.connect(db_path)
        stale.execute("PRAGMA user_version = 7")
        stale.commit()
        stale.close()

        ctx = CheckContext(
            db_path=db_path,
            embed_db_path=tmp_path / "embed.db",
            adapters_dir=tmp_path / "adapters",
            formatters_dir=tmp_path / "formatters",
            queries_dir=tmp_path / "queries",
        )
        try:
            findings = FtsIntegrityCheck().run(ctx)
        finally:
            ctx.close()

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "Skipped" in findings[0].message

        # The diagnostic must not have migrated the DB or created a backup.
        check = sqlite3.connect(db_path)
        assert check.execute("PRAGMA user_version").fetchone()[0] == 7
        check.close()
        assert not list(tmp_path.glob("*.bak*")), "doctor must not create a migration backup"


class TestConfigValidCheck:
    """Tests for the config-valid check."""

    def test_no_config_file(self, check_context, monkeypatch, tmp_path):
        """Returns no findings when config file doesn't exist."""
        # Point to non-existent config
        monkeypatch.setattr(
            "siftd.paths.config_file",
            lambda: tmp_path / "nonexistent" / "config.toml"
        )

        check = ConfigValidCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_valid_config(self, check_context, monkeypatch, tmp_path):
        """Returns no findings for valid config file."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[search]\nformatter = "terminal"\n')

        monkeypatch.setattr(
            "siftd.paths.config_file",
            lambda: config_path
        )

        check = ConfigValidCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_invalid_toml_syntax(self, check_context, monkeypatch, tmp_path):
        """Reports error for invalid TOML syntax."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[search\nformatter = "broken')

        monkeypatch.setattr(
            "siftd.paths.config_file",
            lambda: config_path
        )

        check = ConfigValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "config-valid"
        assert findings[0].severity == "error"
        assert "syntax" in findings[0].message.lower() or "TOML" in findings[0].message

    def test_unknown_formatter(self, check_context, monkeypatch, tmp_path):
        """Reports warning for unknown formatter name."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[search]\nformatter = "nonexistent_formatter"\n')

        monkeypatch.setattr(
            "siftd.paths.config_file",
            lambda: config_path
        )

        check = ConfigValidCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "config-valid"
        assert findings[0].severity == "warning"
        assert "nonexistent_formatter" in findings[0].message
        assert "valid" in findings[0].message.lower()

    def test_valid_theme(self, check_context, monkeypatch, tmp_path):
        """Returns no findings for a known ui.theme name."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[ui]\ntheme = "nord"\n')

        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)

        findings = ConfigValidCheck().run(check_context)
        assert findings == []

    def test_unknown_theme(self, check_context, monkeypatch, tmp_path):
        """Reports warning for an unknown ui.theme name."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[ui]\ntheme = "dracula"\n')

        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)

        findings = ConfigValidCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "config-valid"
        assert findings[0].severity == "warning"
        assert "dracula" in findings[0].message
        assert "ui.theme" in findings[0].message

    def test_theme_validation_mirrors_resolver_normalization(
        self, check_context, monkeypatch, tmp_path
    ):
        """A non-canonical case/whitespace theme the resolver accepts is NOT flagged.

        theme_for_name() normalizes (.strip().lower()), so 'Nord' renders the nord
        theme; the doctor check must agree rather than warn on a value that works.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text('[ui]\ntheme = "Nord"\n')

        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)

        assert ConfigValidCheck().run(check_context) == []

    def test_valid_embed_backend(self, check_context, monkeypatch, tmp_path):
        """A known embed.backend with a literal key yields no findings."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "voyage"\napi_key = "sk-literal"\n')

        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        assert ConfigValidCheck().run(check_context) == []

    def test_unknown_embed_backend(self, check_context, monkeypatch, tmp_path):
        """Reports a warning for an unknown embed.backend name."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "cohere"\n')

        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        findings = ConfigValidCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "cohere" in findings[0].message
        assert "embed.backend" in findings[0].message

    def test_invalid_embed_dimensions(self, check_context, monkeypatch, tmp_path):
        """Reports a warning for non-positive embed.dimensions."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "voyage"\napi_key = "k"\ndimensions = 0\n')

        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        findings = ConfigValidCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "dimensions" in findings[0].message

    def test_unresolvable_embed_api_key(self, check_context, monkeypatch, tmp_path):
        """Reports a warning when embed.api_key references an unset env var (no network)."""
        monkeypatch.delenv("SIFTD_DOCTOR_NOPE", raising=False)
        config_path = tmp_path / "config.toml"
        config_path.write_text('[embed]\nbackend = "voyage"\napi_key = "env:SIFTD_DOCTOR_NOPE"\n')

        monkeypatch.setattr("siftd.paths.config_file", lambda: config_path)
        findings = ConfigValidCheck().run(check_context)

        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "api_key" in findings[0].message

    def test_finding_structure(self, check_context):
        """Check has correct attributes."""
        check = ConfigValidCheck()
        assert check.name == "config-valid"
        assert check.has_fix is False
        assert check.requires_db is False
        assert check.requires_embed_db is False
        assert check.cost == "fast"


class TestWorkspaceIdentityCheck:
    """Tests for the workspace-identity check."""

    def test_no_issues(self, check_context, monkeypatch):
        """Returns no findings when all workspaces have remotes and no duplicates."""
        monkeypatch.setattr(
            "siftd.storage.migrate_workspaces.verify_workspace_identity",
            lambda conn: {
                "total": 5,
                "with_remote": 5,
                "without_remote": 0,
                "duplicate_groups": 0,
                "duplicate_workspaces": 0,
            },
        )
        check = WorkspaceIdentityCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_missing_remotes(self, check_context, monkeypatch):
        """Reports info finding for workspaces without git remote."""
        monkeypatch.setattr(
            "siftd.storage.migrate_workspaces.verify_workspace_identity",
            lambda conn: {
                "total": 5,
                "with_remote": 3,
                "without_remote": 2,
                "duplicate_groups": 0,
                "duplicate_workspaces": 0,
            },
        )
        check = WorkspaceIdentityCheck()
        findings = check.run(check_context)
        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "2 workspace" in findings[0].message
        assert findings[0].fix_command == "siftd backfill --git-remote"

    def test_duplicates(self, check_context, monkeypatch):
        """Reports manual-only warning for duplicate workspace groups."""
        monkeypatch.setattr(
            "siftd.storage.migrate_workspaces.verify_workspace_identity",
            lambda conn: {
                "total": 5,
                "with_remote": 5,
                "without_remote": 0,
                "duplicate_groups": 2,
                "duplicate_workspaces": 4,
            },
        )
        check = WorkspaceIdentityCheck()
        findings = check.run(check_context)
        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert "2 workspace group" in findings[0].message
        assert findings[0].fix_available is False
        assert "siftd migrate --merge-workspaces" in findings[0].message

    def test_finding_structure(self):
        """Check has correct attributes."""
        check = WorkspaceIdentityCheck()
        assert check.name == "workspace-identity"
        assert check.has_fix is True
        assert check.requires_db is True
        assert check.cost == "fast"


# ---------------------------------------------------------------------------
# Helpers for deep-check tests
# ---------------------------------------------------------------------------

def _make_deep_ctx(tmp_path):
    """Create a writable DB and a matching read-only CheckContext."""
    from siftd.storage.sqlite import open_database

    db_path = tmp_path / "deep_test.db"
    conn = open_database(db_path)
    return conn, db_path


def _ctx_for(db_path, tmp_path):
    ctx = CheckContext(
        db_path=db_path,
        embed_db_path=tmp_path / "embeddings.db",
        adapters_dir=tmp_path / "adapters",
        formatters_dir=tmp_path / "formatters",
        queries_dir=tmp_path / "queries",
    )
    for d in [tmp_path / "adapters", tmp_path / "formatters", tmp_path / "queries"]:
        d.mkdir(exist_ok=True)
    return ctx


class TestDbFkIntegrityCheck:
    """Tests for the db-fk-integrity check."""

    def test_healthy_db_empty_findings(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        conn.close()
        ctx = _ctx_for(db_path, tmp_path)
        check = DbFkIntegrityCheck()
        findings = check.run(ctx)
        ctx.close()
        assert findings == []

    def test_fk_violation_detected(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        # Insert a conversation referencing a non-existent harness_id with FK off
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            INSERT INTO conversations (id, external_id, harness_id, started_at)
            VALUES ('fake-conv-001', 'ext-001', 'nonexistent-harness', '2024-01-01T00:00:00Z')
        """)
        conn.commit()
        conn.close()

        ctx = _ctx_for(db_path, tmp_path)
        check = DbFkIntegrityCheck()
        findings = check.run(ctx)
        ctx.close()

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].fix_available is False
        assert findings[0].context["total"] >= 1

    def test_check_attributes(self):
        check = DbFkIntegrityCheck()
        assert check.name == "db-fk-integrity"
        assert check.has_fix is False
        assert check.requires_db is True
        assert check.cost == "deep"

    def test_severe_corruption_emits_overflow_summary(self, tmp_path):
        """>50 FK violations emit a single 'severely corrupt' summary, not a misleading capped count."""
        conn, db_path = _make_deep_ctx(tmp_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        for i in range(60):
            conn.execute(
                "INSERT INTO conversations (id, external_id, harness_id, started_at) "
                "VALUES (?, ?, 'nonexistent-harness', '2024-01-01T00:00:00Z')",
                (f"fake-{i:03d}", f"ext-{i:03d}"),
            )
        conn.commit()
        conn.close()

        ctx = _ctx_for(db_path, tmp_path)
        check = DbFkIntegrityCheck()
        findings = check.run(ctx)
        ctx.close()

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "More than 50" in findings[0].message
        assert findings[0].context == {"total_ge": 51}


class TestDbBlobRefcountDriftCheck:
    """Tests for the db-blob-refcount-drift check."""

    def test_healthy_db_empty_findings(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        conn.close()
        ctx = _ctx_for(db_path, tmp_path)
        check = DbBlobRefcountDriftCheck()
        findings = check.run(ctx)
        ctx.close()
        assert findings == []

    def test_drifted_refcount_detected(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            INSERT INTO content_blobs (hash, content, ref_count, created_at)
            VALUES ('abc123', 'data', 99, '2024-01-01T00:00:00Z')
        """)
        conn.commit()
        conn.close()

        ctx = _ctx_for(db_path, tmp_path)
        check = DbBlobRefcountDriftCheck()
        findings = check.run(ctx)
        ctx.close()

        assert len(findings) >= 1
        assert findings[0].severity == "warning"
        assert findings[0].fix_command == "siftd doctor fix --blob-refcount"

    def test_check_attributes(self):
        check = DbBlobRefcountDriftCheck()
        assert check.name == "db-blob-refcount-drift"
        assert check.has_fix is True
        assert check.requires_db is True
        assert check.cost == "deep"


class TestDbBlobOrphansCheck:
    """Tests for the db-blob-orphans check."""

    def test_healthy_db_empty_findings(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        conn.close()
        ctx = _ctx_for(db_path, tmp_path)
        check = DbBlobOrphansCheck()
        findings = check.run(ctx)
        ctx.close()
        assert findings == []

    def test_zero_refcount_blob_detected(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        # The CHECK constraint prevents ref_count < 0 but allows 0
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            INSERT INTO content_blobs (hash, content, ref_count, created_at)
            VALUES ('orphan123', 'orphan data', 0, '2024-01-01T00:00:00Z')
        """)
        conn.commit()
        conn.close()

        ctx = _ctx_for(db_path, tmp_path)
        check = DbBlobOrphansCheck()
        findings = check.run(ctx)
        ctx.close()

        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert findings[0].context["count"] == 1
        assert findings[0].fix_command == "siftd doctor fix --blob-refcount"

    def test_check_attributes(self):
        check = DbBlobOrphansCheck()
        assert check.name == "db-blob-orphans"
        assert check.has_fix is True
        assert check.requires_db is True
        assert check.cost == "deep"


class TestDbTriggerPresenceCheck:
    """Tests for the db-trigger-presence check."""

    def test_healthy_db_empty_findings(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        conn.close()
        ctx = _ctx_for(db_path, tmp_path)
        check = DbTriggerPresenceCheck()
        findings = check.run(ctx)
        ctx.close()
        assert findings == []

    def test_missing_trigger_detected(self, tmp_path):
        conn, db_path = _make_deep_ctx(tmp_path)
        conn.execute("DROP TRIGGER IF EXISTS tr_event_tool_call_delete_release_blob")
        conn.commit()
        conn.close()

        ctx = _ctx_for(db_path, tmp_path)
        check = DbTriggerPresenceCheck()
        findings = check.run(ctx)
        ctx.close()

        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "tr_event_tool_call_delete_release_blob" in findings[0].context["missing"]
        assert findings[0].fix_command == "siftd doctor fix --triggers"

    def test_check_attributes(self):
        check = DbTriggerPresenceCheck()
        assert check.name == "db-trigger-presence"
        assert check.has_fix is True
        assert check.requires_db is True
        assert check.cost == "deep"


class TestRunChecksDeepFilter:
    """Tests for the deep= parameter on run_checks()."""

    def test_deep_false_excludes_deep_checks(self, test_db):
        findings = run_checks(db_path=test_db, deep=False)
        # No finding should come from a deep check
        deep_names = {"db-fk-integrity", "db-blob-refcount-drift", "db-blob-orphans", "db-trigger-presence"}
        for f in findings:
            assert f.check not in deep_names

    def test_deep_true_includes_deep_checks(self, test_db):
        findings = run_checks(db_path=test_db, checks=["db-fk-integrity"], deep=True)
        assert isinstance(findings, list)
        # No error from unknown check; healthy DB returns empty
        assert all(isinstance(f, Finding) for f in findings)

    def test_deep_false_specific_deep_check_returns_empty(self, test_db):
        """Requesting a deep check by name without deep=True returns no findings."""
        findings = run_checks(db_path=test_db, checks=["db-fk-integrity"], deep=False)
        assert findings == []


class TestDoctorDbSubcommand:
    """Tests for `siftd doctor db` check name filtering."""

    def test_db_subcommand_includes_db_prefixed_checks(self):
        from siftd.doctor.checks import BUILTIN_CHECKS

        _DB_CHECKS = {
            "schema-current", "fts-integrity", "fts-stale",
            "freelist", "blob-migration", "orphaned-chunks",
        }
        db_check_names = {
            c.name for c in BUILTIN_CHECKS
            if c.name.startswith("db-") or c.name in _DB_CHECKS
        }
        assert "db-fk-integrity" in db_check_names
        assert "db-blob-refcount-drift" in db_check_names
        assert "db-blob-orphans" in db_check_names
        assert "db-trigger-presence" in db_check_names
        assert "schema-current" in db_check_names
        assert "fts-integrity" in db_check_names
        # Non-db checks are excluded
        assert "ingest-pending" not in db_check_names
        assert "embeddings-stale" not in db_check_names


class TestFixFunctions:
    """Tests for the fix functions registered in _FIX_REGISTRY."""

    def test_fix_blob_refcount_removes_drifted_blob(self, tmp_path):
        """_fix_blob_refcount deletes a blob whose ref_count != actual references."""
        from siftd.cli.data import _fix_blob_refcount

        conn, db_path = _make_deep_ctx(tmp_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
            "VALUES ('deadbeef01', 'data', 99, '2024-01-01T00:00:00Z')"
        )
        conn.commit()

        result = _fix_blob_refcount(conn, db_path)
        conn.close()

        # Row should be gone (ref_count corrected to 0 then deleted)
        from siftd.storage.sqlite import open_database
        check_conn = open_database(db_path)
        row = check_conn.execute(
            "SELECT 1 FROM content_blobs WHERE hash = 'deadbeef01'"
        ).fetchone()
        check_conn.close()
        assert row is None
        assert "repaired" in result

    def test_fix_blob_refcount_count_matches_drifted_rows_only(self, tmp_path):
        """Reported 'repaired' count reflects drifted rows, not the full table size."""
        from siftd.cli.data import _fix_blob_refcount

        conn, db_path = _make_deep_ctx(tmp_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        # 5 healthy blobs (ref_count = 0 with no references — orphans, will be deleted by sweep)
        for i in range(5):
            conn.execute(
                "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
                "VALUES (?, 'data', 0, '2024-01-01T00:00:00Z')",
                (f"healthy{i:02d}",),
            )
        # 1 drifted blob (ref_count = 99 but no references)
        conn.execute(
            "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
            "VALUES ('drifted01', 'data', 99, '2024-01-01T00:00:00Z')"
        )
        conn.commit()

        result = _fix_blob_refcount(conn, db_path)
        conn.close()

        # Only the drifted row should be reported as 'repaired' (UPDATE only matched it).
        # The 5 healthy orphans had ref_count = 0 and stayed at 0, so the UPDATE skipped them.
        assert result.startswith("1 blob(s) repaired"), (
            f"Expected count to reflect drifted rows only, got: {result}"
        )

    def test_fix_blob_refcount_removes_orphan(self, tmp_path):
        """_fix_blob_refcount deletes a blob with ref_count=0 (no references)."""
        from siftd.cli.data import _fix_blob_refcount

        conn, db_path = _make_deep_ctx(tmp_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO content_blobs (hash, content, ref_count, created_at) "
            "VALUES ('orphanhash1', 'orphan', 0, '2024-01-01T00:00:00Z')"
        )
        conn.commit()

        result = _fix_blob_refcount(conn, db_path)
        conn.close()

        from siftd.storage.sqlite import open_database
        check_conn = open_database(db_path)
        row = check_conn.execute(
            "SELECT 1 FROM content_blobs WHERE hash = 'orphanhash1'"
        ).fetchone()
        check_conn.close()
        assert row is None
        assert "orphan" in result

    def test_fix_blob_triggers_recreates_missing_trigger(self, tmp_path):
        """_fix_blob_triggers recreates both triggers after one is dropped."""
        from siftd.cli.data import _fix_blob_triggers

        conn, db_path = _make_deep_ctx(tmp_path)
        conn.execute("DROP TRIGGER IF EXISTS tr_event_tool_call_delete_release_blob")
        conn.commit()

        _fix_blob_triggers(conn, db_path)
        conn.close()

        from siftd.storage.sqlite import open_database
        check_conn = open_database(db_path)
        triggers = {
            row[0]
            for row in check_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        check_conn.close()
        assert "tr_event_tool_call_delete_release_blob" in triggers
        assert "tr_event_tool_call_update_release_blob" in triggers


class TestEmbeddingsAvailableCheck:
    """Tests for the embeddings-available check."""

    def test_no_embed_db_no_findings(self, check_context, monkeypatch):
        """No findings when embeddings are unavailable and no DB exists."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        check_context.embed_db_path.unlink(missing_ok=True)

        from siftd.doctor.checks.embeddings_available import EmbeddingsAvailableCheck
        check = EmbeddingsAvailableCheck()
        findings = check.run(check_context)
        assert findings == []

    def test_embed_db_exists_without_extra_is_warning(self, check_context, monkeypatch):
        """Warning (not info) when embed DB exists but embed extra is not installed."""
        import siftd.embeddings.availability as avail
        monkeypatch.setattr(avail, "embedding_status", lambda: avail.EmbedStatus(None, False, "not configured"))

        embed_db = check_context.embed_db_path
        embed_db.touch()

        from siftd.doctor.checks.embeddings_available import EmbeddingsAvailableCheck
        check = EmbeddingsAvailableCheck()
        findings = check.run(check_context)

        assert len(findings) == 1
        assert findings[0].check == "embeddings-available"
        assert findings[0].severity == "warning"
        assert findings[0].fix_available is False


class TestFindingSubstrate:
    """Tests for Finding dataclass extensions: hint severity, field, channel."""

    def test_hint_severity_valid(self):
        f = Finding(check="x", severity="hint", message="m", fix_available=False)
        assert f.severity == "hint"

    def test_channel_default_is_both(self):
        f = Finding(check="x", severity="info", message="m", fix_available=False)
        assert f.channel == "both"

    def test_channel_text_excludes_from_json(self, monkeypatch, capsys):
        """channel="text" findings are dropped from --json doctor output."""
        import json
        from types import SimpleNamespace

        from siftd.cli.data import _doctor_run_json

        monkeypatch.setattr(
            "siftd.api.run_checks",
            lambda **_k: [
                Finding(check="a", severity="info", message="visible", fix_available=False, channel="both"),
                Finding(check="b", severity="info", message="tty-only", fix_available=False, channel="text"),
            ],
        )
        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda _: None)

        args = SimpleNamespace(db=None, strict=False, no_hints=False)
        rc = _doctor_run_json(args, None, False, None)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        messages = [f["message"] for f in data["findings"]]
        assert "visible" in messages
        assert "tty-only" not in messages

    def test_no_hints_flag_filters_hints(self, monkeypatch, capsys):
        """--no-hints drops severity="hint" findings before rendering."""
        import json
        from types import SimpleNamespace

        from siftd.cli.data import _doctor_run_json

        monkeypatch.setattr(
            "siftd.api.run_checks",
            lambda **_k: [
                Finding(check="a", severity="info", message="keep", fix_available=False),
                Finding(check="b", severity="hint", message="drop", fix_available=False),
            ],
        )
        monkeypatch.setattr("siftd.doctor.fixes.save_findings_cache", lambda _: None)

        args = SimpleNamespace(db=None, strict=False, no_hints=True)
        rc = _doctor_run_json(args, None, False, None)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        messages = [f["message"] for f in data["findings"]]
        assert "keep" in messages
        assert "drop" not in messages
