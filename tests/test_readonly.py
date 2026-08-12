"""Tests for read-only database mode."""

import os
import sqlite3
import stat

import pytest

from conftest import skip_if_root
from siftd.storage.sqlite import (
    SCHEMA_PATH,
    SCHEMA_VERSION,
    connect_read_only,
    open_database,
)


class TestReadOnlyMode:
    """Tests for open_database read_only parameter."""

    @skip_if_root
    def test_read_only_succeeds_on_readonly_file(self, tmp_path):
        """read_only=True opens successfully when file is chmod read-only."""
        db_path = tmp_path / "test.db"

        # Create DB with some data
        conn = open_database(db_path)
        conn.execute("SELECT 1")
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Should succeed with read_only=True
            conn = open_database(db_path, read_only=True)
            result = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()
            assert result[0] == 0
            conn.close()
        finally:
            # Restore permissions for cleanup
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    @skip_if_root
    def test_read_only_false_fails_on_readonly_file(self, tmp_path):
        """read_only=False fails when file is chmod read-only."""
        db_path = tmp_path / "test.db"

        # Create DB
        conn = open_database(db_path)
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        conn = None
        try:
            # Should fail with read_only=False (default)
            with pytest.raises(Exception):
                conn = open_database(db_path, read_only=False)
                # Force a write to trigger the error (some systems may delay)
                conn.execute("INSERT INTO harnesses (id, name) VALUES ('x', 'x')")
                conn.commit()
        finally:
            if conn:
                conn.close()
            # Restore permissions for cleanup
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    def test_read_only_raises_on_missing_file(self, tmp_path):
        """read_only=True raises FileNotFoundError if DB doesn't exist."""
        db_path = tmp_path / "nonexistent.db"

        with pytest.raises(FileNotFoundError, match="Database not found"):
            open_database(db_path, read_only=True)

    @skip_if_root
    def test_read_only_stale_unwritable_raises_schema_upgrade_required(self, tmp_path):
        """RO open of a stale-schema DB on a non-writable file raises a clear error
        instead of crashing later with a cryptic 'no such table: events'.
        See plans/2026-05-03-events-polymorphic-followup.md finding #1.
        """
        from siftd.storage.sqlite import SCHEMA_VERSION, SchemaUpgradeRequiredError

        db_path = tmp_path / "test.db"

        # Build a writable DB then stamp it at an older schema version.
        conn = open_database(db_path)
        conn.close()
        import sqlite3
        raw = sqlite3.connect(db_path)
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        raw.commit()
        raw.close()

        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            with pytest.raises(SchemaUpgradeRequiredError, match="not writable"):
                open_database(db_path, read_only=True)
        finally:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

    @skip_if_root
    def test_cli_main_translates_schema_upgrade_required_to_clean_error(self, tmp_path, capsys):
        """CLI main() must catch SchemaUpgradeRequiredError so the user sees a
        friendly message rather than a Python traceback. cmd_query (and other
        RO subcommands) only catch FileNotFoundError / OperationalError.
        Regression for PR #16 review feedback.
        """
        from siftd.cli import main
        from siftd.storage.sqlite import SCHEMA_VERSION

        db_path = tmp_path / "test.db"
        conn = open_database(db_path)
        conn.close()
        import sqlite3
        raw = sqlite3.connect(db_path)
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        raw.commit()
        raw.close()

        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            rc = main(["--db", str(db_path), "query", "--limit", "1"])
        finally:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Traceback" not in err  # a clean error, not a Python traceback
        assert "not writable" in err

    def test_read_only_stale_writable_auto_upgrades(self, tmp_path):
        """RO open of a stale-schema DB on a writable file auto-upgrades, then
        the RO connection sees the up-to-date schema. See finding #1.
        """
        from siftd.storage.sqlite import SCHEMA_VERSION

        db_path = tmp_path / "test.db"

        conn = open_database(db_path)
        conn.close()
        import sqlite3
        raw = sqlite3.connect(db_path)
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        raw.commit()
        raw.close()
        assert sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION - 1

        # File is writable — RO open should auto-upgrade and succeed.
        conn = open_database(db_path, read_only=True)
        try:
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            assert v == SCHEMA_VERSION
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "events" in tables
        finally:
            conn.close()

    def test_read_only_sees_commits_a_writer_has_not_checkpointed(self, tmp_path, wal_writer):
        """The read path is not pinned to the last-checkpointed snapshot.

        This is what `mode=ro&immutable=1` cost: an immutable reader ignores the
        `-wal` file outright, so against a database a live `serve` or concurrent
        `ingest` has committed to but not checkpointed, `query`/`search`/`show`
        answered from a stale snapshot and reported it as current. Deterministic,
        not racy — no checkpoint, no visibility, ever. (#42)
        """
        db_path = tmp_path / "test.db"
        open_database(db_path).close()

        writer = wal_writer(db_path)
        writer.commit_to_wal(
            "INSERT INTO harnesses (id, name) VALUES ('h-wal', 'wal_probe')"
        )

        conn = open_database(db_path, read_only=True)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM harnesses").fetchall()}
            assert "wal_probe" in names
        finally:
            conn.close()

    @skip_if_root
    def test_read_only_falls_back_to_immutable_on_read_only_media(self, readonly_media):
        """Immutability is derived from the medium, not asserted by the caller.

        The plain `mode=ro` open needs to create a `-shm` sidecar, which fails
        only where no writer could reach the file either — so the failure is the
        signal, and the `immutable=1` fallback is then true rather than assumed.
        """
        db_path = readonly_media.seed(
            "frozen.db",
            lambda conn: (
                conn.executescript(SCHEMA_PATH.read_text()),
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}"),
            ),
        )

        # Positive control: without the fallback there is nothing to read.
        plain = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            plain.execute("SELECT count(*) FROM conversations")
        plain.close()

        conn = open_database(db_path, read_only=True)
        try:
            assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 0
        finally:
            conn.close()
        assert not (readonly_media.path / "frozen.db-shm").exists(), (
            "probe left a sidecar behind"
        )


class TestConnectReadOnly:
    """The derived-immutability helper every read-only open routes through."""

    def test_locked_database_propagates_rather_than_going_immutable(self, tmp_path):
        """SQLITE_BUSY must not fall back — a lock means a writer is active.

        The fallback is only correct where nothing can write. A locked database
        is the opposite: it is exactly the case where `immutable=1` gives the
        undefined results #42 removed, so degrading to it here would reinstate
        the defect at its worst. Only the READONLY/CANTOPEN families fall back;
        everything else propagates.

        `timeout=0` is what makes this cheap enough to test — the discrimination
        was measured but left untested in #38 because provoking SQLITE_BUSY cost
        SQLite's five-second default wait.
        """
        db_path = tmp_path / "locked.db"
        open_database(db_path).close()

        blocker = sqlite3.connect(db_path)
        try:
            blocker.execute("PRAGMA journal_mode = DELETE")
            blocker.execute("BEGIN EXCLUSIVE")
            blocker.execute(
                "INSERT INTO harnesses (id, name) VALUES ('h-lock', 'locker')"
            )

            with pytest.raises(sqlite3.OperationalError) as excinfo:
                connect_read_only(db_path, timeout=0)
            assert excinfo.value.sqlite_errorname == "SQLITE_BUSY"
        finally:
            blocker.rollback()
            blocker.close()

    @skip_if_root
    def test_read_only_media_takes_the_fallback(self, readonly_media):
        """The one case where `immutable=1` is true rather than asserted."""
        db_path = readonly_media.seed(
            "frozen.db", lambda conn: conn.execute("CREATE TABLE t (x INTEGER)")
        )

        conn = connect_read_only(db_path)
        try:
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
        finally:
            conn.close()
        assert not (readonly_media.path / "frozen.db-shm").exists()


class TestSearchReadOnlyMode:
    """Tests for read-only database access in search code paths."""

    @skip_if_root
    def test_filter_conversations_works_on_readonly_media(self, readonly_media):
        """filter_conversations() works where the sidecar cannot be created.

        Replaces a no-WAL-sidecar assertion. That was a mechanism standing in
        for this goal, and the mechanism changed in #42 — the derived open takes
        WAL read marks on writable media (and so does leave sidecars) precisely
        so it can see a concurrent writer's commits. What has to keep working is
        the read itself on media no writer can reach.
        """
        from siftd.search import filter_conversations

        db_path = readonly_media.seed(
            "test.db",
            lambda conn: (
                conn.executescript(SCHEMA_PATH.read_text()),
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}"),
            ),
        )

        assert filter_conversations(db_path, workspace="test") == set()

    @skip_if_root
    def test_get_active_conversation_ids_works_on_readonly_media(self, readonly_media):
        """get_active_conversation_ids() works where the sidecar cannot be created."""
        from siftd.search import get_active_conversation_ids

        db_path = readonly_media.seed(
            "test.db",
            lambda conn: (
                conn.executescript(SCHEMA_PATH.read_text()),
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}"),
            ),
        )

        assert isinstance(get_active_conversation_ids(db_path), set)

    @skip_if_root
    def test_filter_conversations_works_on_readonly_file(self, tmp_path):
        """filter_conversations() works when DB file is chmod read-only."""
        from siftd.search import filter_conversations

        db_path = tmp_path / "test.db"

        # Create DB with schema and some data
        conn = open_database(db_path)
        conn.execute(
            "INSERT INTO harnesses (id, name) VALUES (?, ?)",
            ("h1", "test_harness"),
        )
        conn.execute(
            "INSERT INTO workspaces (id, path, discovered_at) VALUES (?, ?, ?)",
            ("ws1", "/path/to/project", "2024-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO conversations (id, external_id, harness_id, workspace_id, started_at) VALUES (?, ?, ?, ?, ?)",
            ("conv1", "ext1", "h1", "ws1", "2024-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        # Make file read-only
        os.chmod(db_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        try:
            # Should succeed on read-only file
            result = filter_conversations(db_path, workspace="project")
            assert result == {"conv1"}
        finally:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)

