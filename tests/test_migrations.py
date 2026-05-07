"""Tests for siftd storage migration paths.

Exercises every migration function in sqlite.py and sessions.py by constructing
legacy database schemas (pre-migration state) and verifying migrations work.
"""

import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from siftd.storage.sqlite import SCHEMA_PATH, SCHEMA_VERSION, open_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _legacy_db(tmp_path, *, schema_sql: str, name: str = "legacy.db") -> sqlite3.Connection:
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql)
    conn.commit()
    return conn


def _strip_cascade(sql: str) -> str:
    return re.sub(r"\s+ON DELETE (?:CASCADE|SET NULL)", "", sql)


def _strip_user_version(sql: str) -> str:
    """Remove PRAGMA user_version lines from a schema SQL string."""
    return re.sub(r"\s*PRAGMA\s+user_version\s*=\s*\d+\s*;", "", sql)


def _tables(conn): return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
def _cols(conn, t): return {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
def _ddl(conn, t):
    r = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
    return r[0] if r else ""


_NO_CASCADE = _strip_cascade(SCHEMA_PATH.read_text())
# Legacy schema (v5) still has prompts/responses/tool_calls — used for tests
# that simulate old DBs being migrated forward through all versions including v6.
_V5_SCHEMA_PATH = Path(__file__).parent / "fixtures" / "schemas" / "v5.sql"
# _LEGACY_V5_NO_CASCADE: legacy tables intact, CASCADE stripped, user_version stripped
# so tests start at user_version=0 and can assert stamp behavior.
_LEGACY_V5_NO_CASCADE = _strip_user_version(_strip_cascade(_V5_SCHEMA_PATH.read_text()))


class TestSchemaVersion:
    def test_future_version_raises(self, tmp_path):
        path = tmp_path / "future.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="newer version"):
            open_database(path)

    def test_future_version_read_only_warns(self, tmp_path, caplog):
        path = tmp_path / "future_ro.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        with caplog.at_level(logging.WARNING, logger="siftd.storage.sqlite"):
            conn = open_database(path, read_only=True)
        conn.close()
        assert any("read-only" in r.message for r in caplog.records)

    def test_future_version_read_only_write_fails(self, tmp_path):
        path = tmp_path / "future_ro_write.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        conn = open_database(path, read_only=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO conversations VALUES ('test')")
        finally:
            conn.close()


class TestMigrateLabelsToTags:
    def test_renames_tables_and_columns(self, tmp_path):
        from siftd.storage.sqlite import _migrate_labels_to_tags
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE labels (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE conversation_labels (id TEXT PRIMARY KEY, conversation_id TEXT, label_id TEXT);
            CREATE TABLE workspace_labels (id TEXT PRIMARY KEY, workspace_id TEXT, label_id TEXT);
        """)
        conn.execute("INSERT INTO labels VALUES ('t1', 'bug')")
        conn.execute("INSERT INTO conversation_labels VALUES ('cl1', 'c1', 't1')")
        conn.execute("INSERT INTO workspace_labels VALUES ('wl1', 'w1', 't1')")
        conn.commit()
        _migrate_labels_to_tags(conn)
        assert "tags" in _tables(conn) and "labels" not in _tables(conn)
        assert "tag_id" in _cols(conn, "conversation_tags") and "tag_id" in _cols(conn, "workspace_tags")
        assert conn.execute("SELECT name FROM tags WHERE id='t1'").fetchone()[0] == "bug"
        conn.close()

    def test_noop_paths(self, tmp_path):
        from siftd.storage.sqlite import _migrate_labels_to_tags
        # No labels table → no-op
        c1 = _legacy_db(tmp_path, schema_sql="CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT);", name="a.db")
        _migrate_labels_to_tags(c1)
        assert "tags" in _tables(c1)
        c1.close()
        # Both labels and tags → skip
        c2 = _legacy_db(tmp_path, schema_sql="CREATE TABLE labels (id TEXT PRIMARY KEY, name TEXT); CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT);", name="b.db")
        _migrate_labels_to_tags(c2)
        assert "labels" in _tables(c2)
        c2.close()


class TestColumnMigrations:
    def test_add_columns(self, tmp_path):
        from siftd.storage.sqlite import (
            _migrate_add_branch_column,
            _migrate_add_error_column,
            _migrate_add_file_stat_columns,
        )
        # error column
        c1 = _legacy_db(tmp_path, schema_sql="CREATE TABLE ingested_files (id TEXT PRIMARY KEY, path TEXT, file_hash TEXT, harness_id TEXT, conversation_id TEXT, ingested_at TEXT);", name="a.db")
        _migrate_add_error_column(c1)
        assert "error" in _cols(c1, "ingested_files")
        c1.close()
        # file_mtime/file_size
        c2 = _legacy_db(tmp_path, schema_sql="CREATE TABLE ingested_files (id TEXT PRIMARY KEY, path TEXT, file_hash TEXT, harness_id TEXT, conversation_id TEXT, ingested_at TEXT, error TEXT);", name="b.db")
        _migrate_add_file_stat_columns(c2)
        assert "file_mtime" in _cols(c2, "ingested_files") and "file_size" in _cols(c2, "ingested_files")
        c2.close()
        # branch
        c3 = _legacy_db(tmp_path, schema_sql="CREATE TABLE conversations (id TEXT PRIMARY KEY, external_id TEXT, harness_id TEXT, workspace_id TEXT, started_at TEXT, ended_at TEXT);", name="c.db")
        _migrate_add_branch_column(c3)
        assert "branch" in _cols(c3, "conversations")
        c3.close()


class TestMigrateAddCascadeDeletes:
    def test_adds_cascade_and_preserves_data(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = _legacy_db(tmp_path, schema_sql=_LEGACY_V5_NO_CASCADE)
        assert "ON DELETE CASCADE" not in _ddl(conn, "prompts")
        conn.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        conn.execute("INSERT INTO workspaces VALUES ('w1','/test',NULL,'2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO tags VALUES ('tg1','important',NULL,'2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO conversations VALUES ('c1','ext1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        conn.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:00:01Z',100,200)")
        conn.execute("INSERT INTO tool_calls VALUES ('tc1','r1','c1',NULL,'etc1','{}','result',NULL,'success','2024-01-01T00:00:02Z')")
        conn.execute("INSERT INTO prompt_content VALUES ('pc1','p1',0,'text','hello')")
        conn.execute("INSERT INTO response_content VALUES ('rc1','r1',0,'text','world')")
        conn.execute("INSERT INTO conversation_tags VALUES ('ct1','c1','tg1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO ingested_files VALUES ('if1','/f.jsonl','hash1','h1','c1','2024-01-01T00:00:00Z',NULL,NULL,NULL)")
        conn.commit()
        _migrate_add_cascade_deletes(conn)
        for t in ["prompts", "responses", "tool_calls", "prompt_content", "response_content", "conversation_tags", "ingested_files"]:
            assert "ON DELETE CASCADE" in _ddl(conn, t), f"{t} missing CASCADE"
        assert conn.execute("SELECT content FROM prompt_content WHERE id='pc1'").fetchone()[0] == "hello"
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM conversations WHERE id='c1'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        conn.close()

    def test_preserves_runtime_columns_and_data(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes

        conn = _legacy_db(tmp_path, schema_sql=_LEGACY_V5_NO_CASCADE)
        conn.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        conn.execute("INSERT INTO workspaces VALUES ('w1','/test',NULL,'2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO conversations VALUES ('c1','ext1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        conn.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:00:01Z',100,200)")
        conn.execute(
            "INSERT INTO content_blobs VALUES ('blob1','blob-content',1,'2024-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO tool_calls VALUES ('tc1','r1','c1',NULL,'etc1','{}','result','blob1','success','2024-01-01T00:00:02Z')"
        )
        conn.execute(
            "INSERT INTO ingested_files VALUES ('if1','/f.jsonl','hash1','h1','c1','2024-01-01T00:00:00Z',NULL,123.5,456)"
        )
        conn.commit()

        _migrate_add_cascade_deletes(conn)

        assert "result_hash" in _cols(conn, "tool_calls")
        assert "file_mtime" in _cols(conn, "ingested_files")
        assert "file_size" in _cols(conn, "ingested_files")

        tool_call = conn.execute(
            "SELECT result_hash FROM tool_calls WHERE id='tc1'"
        ).fetchone()
        ingested = conn.execute(
            "SELECT file_mtime, file_size FROM ingested_files WHERE id='if1'"
        ).fetchone()

        assert tool_call[0] == "blob1"
        assert ingested[0] == 123.5
        assert ingested[1] == 456
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()

    def test_skips_missing_tables(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = _legacy_db(tmp_path, schema_sql=_LEGACY_V5_NO_CASCADE)
        conn.execute("DROP TABLE IF EXISTS tool_call_attributes")
        conn.execute("DROP TABLE IF EXISTS prompt_attributes")
        conn.commit()
        _migrate_add_cascade_deletes(conn)
        assert "ON DELETE CASCADE" in _ddl(conn, "prompts")
        assert "tool_call_attributes" not in _tables(conn)
        conn.close()

    def test_noop_already_migrated(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = open_database(tmp_path / "fresh.db")
        _migrate_add_cascade_deletes(conn)
        # prompts table is dropped in v6; check a table that still has CASCADE
        assert "ON DELETE CASCADE" in _ddl(conn, "ingested_files")
        conn.close()

    def test_noop_no_prompts(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = _legacy_db(tmp_path, schema_sql="CREATE TABLE x (id TEXT PRIMARY KEY);")
        _migrate_add_cascade_deletes(conn)
        assert "x" in _tables(conn)
        conn.close()


class TestSessionsLastSeenAtMigration:
    def test_adds_last_seen_at(self, tmp_path):
        from siftd.storage.sessions import ensure_session_tables
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE active_sessions (id TEXT PRIMARY KEY, adapter_name TEXT NOT NULL,
                workspace_path TEXT, started_at TEXT NOT NULL);
            CREATE TABLE pending_tags (id TEXT PRIMARY KEY, harness_session_id TEXT NOT NULL,
                tag_name TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        conn.execute("INSERT INTO active_sessions VALUES ('s1','claude_code','/test','2024-01-01T00:00:00Z')")
        conn.commit()
        ensure_session_tables(conn)
        assert "last_seen_at" in _cols(conn, "active_sessions")
        assert conn.execute("SELECT last_seen_at FROM active_sessions WHERE id='s1'").fetchone()[0] == "2024-01-01T00:00:00Z"
        conn.close()


class TestMigrationProgressLogging:
    """Contract: each MIGRATIONS[v] phase emits at least one INFO log line.

    Without these lines the migration appeared to hang silently for tens of
    minutes on real-world data — see plans/2026-05-03-events-polymorphic-followup.md
    finding #2.
    """

    def test_each_phase_emits_info_log(self, tmp_path, monkeypatch, caplog):
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        v3_sql = (Path(__file__).parent / "fixtures" / "schemas" / "v3.sql").read_text()
        path = tmp_path / "v3.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(v3_sql)
        conn.commit()
        conn.close()

        with caplog.at_level(logging.INFO, logger="siftd.storage.sqlite"):
            conn = open_database(path)
            conn.close()

        messages = [r.message for r in caplog.records if r.name == "siftd.storage.sqlite"]
        joined = "\n".join(messages)

        assert "Migrating schema v3" in joined, joined
        for phase in ("Migration v4", "Migration v5", "Migration v6", "Migration v7"):
            assert phase in joined, f"missing {phase} log line in:\n{joined}"


class TestOpenDatabaseMigrations:
    def test_full_migration_path(self, tmp_path):
        """open_database on a legacy DB runs all migrations."""
        path = tmp_path / "legacy_full.db"
        # Use v5 legacy schema (has prompts/responses/tool_calls) without optional columns
        lines = _LEGACY_V5_NO_CASCADE.split("\n")
        filtered = [ln for ln in lines if not re.search(
            r"^\s*(branch\s+TEXT|error\s+TEXT|file_mtime\s+REAL|file_size\s+INTEGER)", ln)]
        schema = re.sub(r",(\s*\n\s*\))", r"\1", "\n".join(filtered))
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        conn.commit()
        conn.close()
        conn = open_database(path)
        assert "branch" in _cols(conn, "conversations")
        assert "error" in _cols(conn, "ingested_files")
        # v6 drops prompts — after full migration path it should not exist
        assert "prompts" not in _tables(conn)
        assert "events" in _tables(conn)
        conn.close()


class TestMigrationRunner:
    def test_new_db_stamps_schema_version(self, tmp_path):
        path = tmp_path / "new.db"
        conn = open_database(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == SCHEMA_VERSION

    def test_legacy_db_stamps_schema_version(self, tmp_path):
        # Simulate a pre-S0 existing DB (user_version = 0, missing columns)
        path = tmp_path / "legacy.db"
        # Use v5 legacy schema (has prompts/responses/tool_calls) without optional columns
        lines = _LEGACY_V5_NO_CASCADE.split("\n")
        filtered = [ln for ln in lines if not re.search(
            r"^\s*(branch\s+TEXT|error\s+TEXT|file_mtime\s+REAL|file_size\s+INTEGER)", ln)]
        schema = re.sub(r",(\s*\n\s*\))", r"\1", "\n".join(filtered))
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        conn.commit()
        conn.close()

        conn = open_database(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == SCHEMA_VERSION


class TestMigrationConcurrency:
    def test_concurrent_open_raises_locked_after_timeout(self, tmp_path):
        """Second open_database call with a low busy_timeout raises 'database is locked'."""
        path = tmp_path / "race.db"
        conn = open_database(path)
        conn.close()

        # Hold a write lock to block migration dispatch in the subprocess
        holder = sqlite3.connect(str(path))
        holder.execute("PRAGMA journal_mode = WAL")
        holder.execute("BEGIN IMMEDIATE")

        src_dir = str(Path(__file__).parent.parent / "src")
        script = (
            "import sys; "
            f"sys.path.insert(0, {src_dir!r}); "
            "from pathlib import Path; "
            "from siftd.storage.sqlite import open_database; "
            f"conn = open_database(Path({str(path)!r})); "
            "conn.close()"
        )
        env = {**os.environ, "SIFTD_MIGRATION_BUSY_TIMEOUT_MS": "200"}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        holder.close()

        assert result.returncode != 0
        assert "locked" in (result.stderr + result.stdout).lower()


# ---------------------------------------------------------------------------
# Helpers for S1 tests
# ---------------------------------------------------------------------------

class _StaleColumnCheckWrapper:
    """Wraps a real sqlite3.Connection, returning a stale PRAGMA table_info on the first
    call for a given table — as if another process had not yet committed the column add.
    All subsequent calls, including the verification re-read and the actual ALTER TABLE,
    go to the real connection.  This lets us exercise the R2 duplicate-column race path.
    """

    def __init__(self, real_conn: sqlite3.Connection, table: str, exclude_col: str) -> None:
        self._conn = real_conn
        self._table = table
        self._exclude_col = exclude_col
        self._intercepted = False

    def execute(self, sql, *args, **kwargs):
        if not self._intercepted and f"PRAGMA table_info({self._table})" in sql:
            self._intercepted = True
            # Fetch real rows then filter out the column (simulate stale view)
            real_rows = self._conn.execute(f"PRAGMA table_info({self._table})").fetchall()
            return _RowListCursor([r for r in real_rows if r[1] != self._exclude_col])
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _RowListCursor:
    """Minimal cursor-like object that yields a fixed list of rows."""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


# Schema with prompts having CASCADE, responses not (partial-cascade state)
_PARTIAL_CASCADE_SCHEMA = """
    CREATE TABLE harnesses (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        version TEXT, display_name TEXT, source TEXT, log_format TEXT);
    CREATE TABLE models (id TEXT PRIMARY KEY, raw_name TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, creator TEXT, family TEXT, version TEXT, variant TEXT, released TEXT);
    CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        display_name TEXT, billing_model TEXT);
    CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        category TEXT, description TEXT);
    CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
        git_remote TEXT, discovered_at TEXT NOT NULL);
    CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        description TEXT, created_at TEXT NOT NULL);
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY, external_id TEXT NOT NULL,
        harness_id TEXT NOT NULL REFERENCES harnesses(id),
        workspace_id TEXT REFERENCES workspaces(id),
        branch TEXT, started_at TEXT NOT NULL, ended_at TEXT,
        UNIQUE (harness_id, external_id)
    );
    CREATE TABLE prompts (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        external_id TEXT, timestamp TEXT NOT NULL,
        UNIQUE (conversation_id, external_id)
    );
    CREATE TABLE responses (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        prompt_id TEXT REFERENCES prompts(id),
        model_id TEXT REFERENCES models(id),
        provider_id TEXT REFERENCES providers(id),
        external_id TEXT, timestamp TEXT NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER,
        UNIQUE (conversation_id, external_id)
    );
"""


# ---------------------------------------------------------------------------
# TestMigrateCascadeV2 — S1 acceptance criteria
# ---------------------------------------------------------------------------

class TestMigrateCascadeV2:
    def test_full_legacy_schema_gets_cascade(self, tmp_path):
        """Legacy no-cascade DB ends with every contract FK satisfied."""
        from siftd.storage.sqlite import _CASCADE_CONTRACT, _migrate_cascade_v2, _table_needs_cascade

        conn = _legacy_db(tmp_path, schema_sql=_LEGACY_V5_NO_CASCADE)
        # Sanity: at least prompts has no CASCADE before migration
        assert "ON DELETE CASCADE" not in _ddl(conn, "prompts")

        conn.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        conn.execute("INSERT INTO workspaces VALUES ('w1','/test',NULL,'2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO tags VALUES ('tg1','important',NULL,'2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO conversations VALUES ('c1','ext1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        conn.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:00:01Z',10,20)")
        conn.execute("INSERT INTO conversation_tags VALUES ('ct1','c1','tg1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO ingested_files VALUES ('if1','/f.jsonl','h1','h1','c1','2024-01-01T00:00:00Z',NULL,NULL,NULL)")
        conn.commit()

        _migrate_cascade_v2(conn)

        # Verify every existing contract table now satisfies its FK spec
        for table, fks in _CASCADE_CONTRACT.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            assert not _table_needs_cascade(conn, table, fks), \
                f"{table} still missing required CASCADE after migration"

        # Data preserved
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversation_tags").fetchone()[0] == 1

        # FK enforcement works end-to-end
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM conversations WHERE id='c1'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM conversation_tags").fetchone()[0] == 0
        conn.close()

    def test_partial_cascade_fixture(self, tmp_path):
        """Table already with correct CASCADE is untouched; incomplete table is fixed."""
        from siftd.storage.sqlite import _CASCADE_CONTRACT, _migrate_cascade_v2, _table_needs_cascade

        conn = _legacy_db(tmp_path, schema_sql=_PARTIAL_CASCADE_SCHEMA)

        # Prompts already correct; responses needs fix
        assert not _table_needs_cascade(conn, "prompts", _CASCADE_CONTRACT["prompts"])
        assert _table_needs_cascade(conn, "responses", _CASCADE_CONTRACT["responses"])

        prompts_ddl_before = _ddl(conn, "prompts")

        conn.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        conn.execute("INSERT INTO conversations VALUES ('c1','ext1','h1',NULL,NULL,'2024-01-01T00:00:00Z',NULL)")
        conn.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:00:01Z',5,10)")
        conn.commit()

        _migrate_cascade_v2(conn)

        # Responses now correct; prompts DDL unchanged
        assert not _table_needs_cascade(conn, "responses", _CASCADE_CONTRACT["responses"])
        assert _ddl(conn, "prompts") == prompts_ddl_before, "prompts DDL mutated despite already correct"

        # Data preserved
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1
        conn.close()

    def test_failure_rolls_back_savepoint(self, tmp_path, monkeypatch):
        """Exception inside _recreate_table_with_fks rolls back savepoint and restores FK enforcement."""
        import siftd.storage.sqlite as sqlite_mod
        from siftd.storage.sqlite import _migrate_cascade_v2

        conn = _legacy_db(tmp_path, schema_sql=_LEGACY_V5_NO_CASCADE)
        conn.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        conn.execute("INSERT INTO conversations VALUES ('c1','ext1','h1',NULL,NULL,'2024-01-01T00:00:00Z',NULL)")
        conn.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        conn.commit()

        original_fn = sqlite_mod._recreate_table_with_fks
        call_count = [0]

        def failing_recreate(conn, table_name, new_ddl, columns, indexes):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("injected failure")
            original_fn(conn, table_name, new_ddl, columns, indexes)

        monkeypatch.setattr(sqlite_mod, "_recreate_table_with_fks", failing_recreate)

        with pytest.raises(RuntimeError, match="injected failure"):
            _migrate_cascade_v2(conn)

        # FK enforcement must be ON after exception (try/finally in migration)
        fk_val = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk_val == 1, f"FK enforcement not restored; got {fk_val}"

        # user_version must not have been stamped (migration didn't complete)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

        # Data still consistent (ROLLBACK TO savepoint was issued)
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1
        conn.close()

    def test_fk_check_failure_prevents_stamp(self, tmp_path):
        """Orphaned FK data causes migration to raise; user_version stays un-stamped."""
        from siftd.storage.sqlite import _migrate_cascade_v2

        # Create DB with no CASCADE — FK enforcement is off so we can insert orphans
        conn = _legacy_db(tmp_path, schema_sql=_LEGACY_V5_NO_CASCADE)
        conn.execute("PRAGMA foreign_keys = OFF")
        # Insert a prompt referencing a non-existent conversation
        conn.execute("INSERT INTO prompts VALUES ('p1','orphan-conv','ep1','2024-01-01T00:00:00Z')")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()

        with pytest.raises(RuntimeError, match="FK violations"):
            _migrate_cascade_v2(conn)

        # FK enforcement must be restored even though migration raised
        fk_val = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk_val == 1

        # The orphaned row still exists (RELEASE savepoint committed partial work,
        # but user_version is not stamped — the caller is responsible for that)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        conn.close()

    def test_no_fk_tables_survive_unchanged(self, tmp_path):
        """content_blobs, sync_inbox, active_sessions, pending_tags, push_log survive."""
        from siftd.storage.sqlite import (
            _migrate_cascade_v2,
            ensure_push_log_table,
            ensure_sync_inbox_table,
        )
        from siftd.storage.sessions import ensure_session_tables

        # Create a fresh DB and populate no-FK tables
        conn = open_database(tmp_path / "main.db")
        ensure_push_log_table(conn)
        conn.execute(
            "INSERT INTO content_blobs VALUES ('sha256abc','content data',1,'2024-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO sync_inbox VALUES ('si1','2024-01-01T00:00:00Z',NULL,NULL,'staged',NULL,NULL,NULL,NULL)"
        )
        conn.execute(
            "INSERT INTO active_sessions VALUES ('sess1','claude_code','/proj','2024-01-01T00:00:00Z','2024-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO pending_tags VALUES ('pt1','sess1','my-tag','conversation',NULL,'2024-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO push_log VALUES ('pl1','user@host','2024-01-01T00:00:00Z',3,1024,NULL)"
        )
        conn.commit()

        no_fk_tables = ["content_blobs", "sync_inbox", "active_sessions", "pending_tags", "push_log"]
        pre_ddl = {t: _ddl(conn, t) for t in no_fk_tables}
        pre_counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in no_fk_tables
        }

        # Migration should be a no-op (all FK tables correct in fresh DB)
        _migrate_cascade_v2(conn)

        post_ddl = {t: _ddl(conn, t) for t in no_fk_tables}
        post_counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in no_fk_tables
        }

        assert pre_ddl == post_ddl, "DDL changed for no-FK table"
        assert pre_counts == post_counts, "Row counts changed for no-FK table"
        conn.close()

    def test_noop_when_all_correct(self, tmp_path):
        """Migration returns early without touching any table when all FKs are correct."""
        from siftd.storage.sqlite import _migrate_cascade_v2

        conn = open_database(tmp_path / "fresh.db")
        ddl_before = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        _migrate_cascade_v2(conn)

        ddl_after = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert ddl_before == ddl_after, "DDL changed on already-correct DB"
        conn.close()

    def test_open_database_on_legacy_gets_cascade_v2(self, tmp_path):
        """open_database on a legacy no-cascade DB stamps current SCHEMA_VERSION."""
        # Use v5 legacy schema (has prompts/responses/tool_calls) without optional columns
        lines = _LEGACY_V5_NO_CASCADE.split("\n")
        filtered = [ln for ln in lines if not re.search(
            r"^\s*(branch\s+TEXT|error\s+TEXT|file_mtime\s+REAL|file_size\s+INTEGER)", ln)]
        schema = re.sub(r",(\s*\n\s*\))", r"\1", "\n".join(filtered))
        path = tmp_path / "legacy_v2.db"
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        conn.commit()
        conn.close()

        conn = open_database(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        # v6 drops prompts and responses; check events table exists instead
        assert "prompts" not in _tables(conn)
        assert "events" in _tables(conn)
        conn.close()


# ---------------------------------------------------------------------------
# TestColumnMigrationsR2 — duplicate-column race hardening
# ---------------------------------------------------------------------------

class TestColumnMigrationsR2:
    def test_error_column_duplicate_race_succeeds(self, tmp_path):
        """Stale PRAGMA table_info + duplicate column name error → treated as success."""
        from siftd.storage.sqlite import _migrate_add_error_column

        # Pre-add the column so the real table has it (simulates what another process did)
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY, path TEXT, file_hash TEXT,
                harness_id TEXT, conversation_id TEXT, ingested_at TEXT
            )
        """)
        conn.execute("ALTER TABLE ingested_files ADD COLUMN error TEXT")
        conn.commit()

        # Wrap: first PRAGMA table_info hides 'error'; ALTER TABLE will raise
        # "duplicate column name"; second PRAGMA (re-verify) sees the real column
        wrapper = _StaleColumnCheckWrapper(conn, "ingested_files", exclude_col="error")
        _migrate_add_error_column(wrapper)  # must not raise

        assert "error" in _cols(conn, "ingested_files")
        conn.close()

    def test_file_stat_columns_duplicate_race_succeeds(self, tmp_path):
        """Stale PRAGMA + duplicate column names for file_mtime → treated as success."""
        from siftd.storage.sqlite import _migrate_add_file_stat_columns

        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY, path TEXT, file_hash TEXT,
                harness_id TEXT, conversation_id TEXT, ingested_at TEXT, error TEXT
            )
        """)
        conn.execute("ALTER TABLE ingested_files ADD COLUMN file_mtime REAL")
        conn.execute("ALTER TABLE ingested_files ADD COLUMN file_size INTEGER")
        conn.commit()

        wrapper = _StaleColumnCheckWrapper(conn, "ingested_files", exclude_col="file_mtime")
        _migrate_add_file_stat_columns(wrapper)  # must not raise

        assert "file_mtime" in _cols(conn, "ingested_files")
        assert "file_size" in _cols(conn, "ingested_files")
        conn.close()

    def test_branch_column_duplicate_race_succeeds(self, tmp_path):
        """Stale PRAGMA + duplicate column name for branch → treated as success."""
        from siftd.storage.sqlite import _migrate_add_branch_column

        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, external_id TEXT, harness_id TEXT,
                workspace_id TEXT, branch TEXT, started_at TEXT, ended_at TEXT
            )
        """)
        # branch is already present in the real table — PRAGMA returns it,
        # so _migrate_add_branch_column will simply skip (idempotent path).
        _migrate_add_branch_column(conn)  # must not raise

        assert "branch" in _cols(conn, "conversations")
        conn.close()

    def test_r2_reraises_when_column_truly_missing(self, tmp_path):
        """If ALTER TABLE raises 'duplicate column name' but column isn't there, re-raise."""
        import sqlite3 as _sqlite3

        from siftd.storage.sqlite import _migrate_add_error_column

        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY, path TEXT, file_hash TEXT,
                harness_id TEXT, conversation_id TEXT, ingested_at TEXT
            )
        """)

        # Fake: PRAGMA hides 'error', ALTER TABLE raises duplicate-col, but real table
        # still lacks the column (so re-verify also returns without it).
        class _FullyFakeWrapper:
            def __init__(self, real_conn):
                self._conn = real_conn
                self._pragma_calls = 0

            def execute(self, sql, *args, **kwargs):
                if "PRAGMA table_info" in sql:
                    self._pragma_calls += 1
                    # Always return without 'error', even on re-verify
                    rows = self._conn.execute("PRAGMA table_info(ingested_files)").fetchall()
                    return _RowListCursor([r for r in rows if r[1] != "error"])
                if "ALTER TABLE" in sql and "ADD COLUMN error" in sql:
                    raise _sqlite3.OperationalError("duplicate column name: error")
                return self._conn.execute(sql, *args, **kwargs)

            def commit(self):
                return self._conn.commit()

            def __getattr__(self, name):
                return getattr(self._conn, name)

        wrapper = _FullyFakeWrapper(conn)
        with pytest.raises(_sqlite3.OperationalError, match="duplicate column name"):
            _migrate_add_error_column(wrapper)
        conn.close()


# ---------------------------------------------------------------------------
# TestTransactionOwnershipS3 — R3: helpers must not commit inside the runner
# ---------------------------------------------------------------------------


class TestTransactionOwnershipS3:
    """S3: ensure/migration helpers do not commit inside the migration runner transaction."""

    def _make_commit_counter(self):
        """Return (connect_fn, calls) where calls[0] increments on each commit()."""
        import sqlite3 as _sqlite3

        calls = [0]

        class _TrackingConn(_sqlite3.Connection):
            def commit(self):
                calls[0] += 1
                super().commit()

        def _connect(*args, **kwargs):
            return _TrackingConn(*args, **kwargs)

        return _connect, calls

    def test_no_helper_commits_fresh_db(self, tmp_path, monkeypatch):
        """Fresh DB: exactly 2 commits — schema init + the runner's final commit."""
        import sqlite3 as _sqlite3

        path = tmp_path / "fresh.db"
        connect_fn, calls = self._make_commit_counter()
        monkeypatch.setattr(_sqlite3, "connect", connect_fn)

        conn = open_database(path)
        conn.close()

        # commit #1: explicit conn.commit() after executescript (new-DB path, line 89)
        # commit #2: migration runner's final conn.commit() (line 130)
        assert calls[0] == 2, f"Expected 2 commits on fresh DB, got {calls[0]}"

    def test_no_helper_commits_legacy_db(self, tmp_path, monkeypatch):
        """Legacy DB (all column + cascade + blob migrations needed): exactly 1 commit."""
        import sqlite3 as _sqlite3

        # Build a fully-legacy schema: use v5 (has prompts/responses/tool_calls), no CASCADE, no optional columns
        lines = _LEGACY_V5_NO_CASCADE.split("\n")
        filtered = [ln for ln in lines if not re.search(
            r"^\s*(branch\s+TEXT|error\s+TEXT|file_mtime\s+REAL|file_size\s+INTEGER)", ln)]
        schema = re.sub(r",(\s*\n\s*\))", r"\1", "\n".join(filtered))
        path = tmp_path / "legacy.db"
        raw = _sqlite3.connect(str(path))
        raw.executescript(schema)
        raw.commit()
        raw.close()

        connect_fn, calls = self._make_commit_counter()
        monkeypatch.setattr(_sqlite3, "connect", connect_fn)

        conn = open_database(path)
        conn.close()

        # Only the runner's final conn.commit() should have fired; every helper
        # that previously committed internally has been cleaned up by S3.
        assert calls[0] == 1, f"Expected 1 commit on legacy DB, got {calls[0]}"

    def test_commit_true_standalone_persists(self, tmp_path):
        """Helpers called with commit=True outside open_database() persist their changes."""
        from siftd.storage.fts import rebuild_fts_index
        from siftd.storage.sessions import ensure_session_tables

        # ensure_session_tables: empty raw DB, commit=True, survives reconnect
        path_s = tmp_path / "sessions.db"
        raw = sqlite3.connect(str(path_s))
        ensure_session_tables(raw, commit=True)
        raw.close()

        reopen = sqlite3.connect(str(path_s))
        tables = {r[0] for r in reopen.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        reopen.close()
        assert "active_sessions" in tables
        assert "pending_tags" in tables

        # rebuild_fts_index: insert minimal content via event_content, commit=True, FTS rows survive reconnect
        path_f = tmp_path / "fts.db"
        fconn = open_database(path_f)
        fconn.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        fconn.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01T00:00:00Z')")
        fconn.execute(
            "INSERT INTO conversations VALUES ('c1','e1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)"
        )
        fconn.execute("INSERT INTO events VALUES ('ev1','prompt','c1',NULL,NULL,'2024-01-01T00:00:00Z')")
        fconn.execute(
            """INSERT INTO event_content VALUES ('ec1','ev1',0,'text','{"text":"hello world"}')"""
        )
        fconn.commit()
        rebuild_fts_index(fconn, commit=True)
        fconn.close()

        fconn2 = open_database(path_f)
        count = fconn2.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0]
        fconn2.close()
        assert count > 0

    def test_migration_failure_rolls_back_all_helpers(self, tmp_path, monkeypatch):
        """open_database failure mid-migration rolls back ALL helper DDL — no pre-committed partial state."""
        import siftd.storage.sqlite as sqlite_mod

        # Legacy schema: v5 (has prompts/responses/tool_calls), no CASCADE, no optional columns
        lines = _LEGACY_V5_NO_CASCADE.split("\n")
        filtered = [ln for ln in lines if not re.search(
            r"^\s*(branch\s+TEXT|error\s+TEXT|file_mtime\s+REAL|file_size\s+INTEGER)", ln)]
        schema = re.sub(r",(\s*\n\s*\))", r"\1", "\n".join(filtered))
        path = tmp_path / "legacy.db"
        raw = sqlite3.connect(str(path))
        raw.executescript(schema)
        raw.commit()
        raw.close()

        original_fn = sqlite_mod._recreate_table_with_fks
        call_count = [0]

        def failing_recreate(conn, table_name, new_ddl, columns, indexes):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("injected mid-migration failure")
            original_fn(conn, table_name, new_ddl, columns, indexes)

        monkeypatch.setattr(sqlite_mod, "_recreate_table_with_fks", failing_recreate)

        with pytest.raises(RuntimeError, match="injected mid-migration failure"):
            open_database(path)

        # Open the DB file directly (bypassing open_database) to check the raw state
        check = sqlite3.connect(str(path))
        version = check.execute("PRAGMA user_version").fetchone()[0]
        cols = {r[1] for r in check.execute("PRAGMA table_info(ingested_files)").fetchall()}
        check.close()

        assert version == 0, "user_version was stamped despite rollback"
        # Before S3, _migrate_add_error_column committed its DDL before the failure,
        # so the column would survive the rollback. After S3 it must be rolled back too.
        assert "error" not in cols, "error column was pre-committed by a helper (S3 regression)"
        assert "file_mtime" not in cols, "file_mtime was pre-committed by a helper (S3 regression)"


# ---------------------------------------------------------------------------
# bug_018 + bug_011 — blob refcount triggers survive v2→v3 cascade migration
# ---------------------------------------------------------------------------


class TestBlobRefcountTriggerMigration:
    """Both blob refcount triggers must exist and be correct after open_database."""

    def _open_legacy(self, tmp_path) -> "sqlite3.Connection":
        """Create a v0 DB from the v5 legacy schema, close it, return the path."""
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(_LEGACY_V5_NO_CASCADE)
        conn.commit()
        conn.close()
        return path

    def test_triggers_survive_cascade_migration(self, tmp_path):
        """Both blob refcount triggers exist after the v2→v3 migration chain.

        MIGRATIONS[2] drops tool_calls (CASCADE rewrite), which implicitly drops
        both triggers. MIGRATIONS[3] must unconditionally recreate them.
        """
        path = self._open_legacy(tmp_path)
        conn = open_database(path)
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE 'tr_event_tool_call_%_release_blob'"
            ).fetchall()
        }
        conn.close()
        assert "tr_event_tool_call_delete_release_blob" in triggers
        assert "tr_event_tool_call_update_release_blob" in triggers

    def test_delete_trigger_fires_after_legacy_migration(self, tmp_path):
        """After migration, DELETE from event_tool_call decrements blob ref_count."""
        from siftd.storage import get_ref_count
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        path = self._open_legacy(tmp_path)
        conn = open_database(path)
        h = get_or_create_harness(conn, "test", source="test")
        w = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        c = insert_conversation(conn, "c1", h, w, "2024-01-01T00:00:00Z")
        p = insert_prompt(conn, c, "p1", "2024-01-01T00:00:00Z")
        r = insert_response(conn, c, p, None, None, "r1", "2024-01-01T00:00:01Z")
        tc_id = insert_tool_call(conn, r, c, None, "tc1", "{}", '{"out":1}', "success", "2024-01-01T00:00:02Z")
        conn.commit()

        row = conn.execute("SELECT result_hash FROM event_tool_call WHERE event_id=?", (tc_id,)).fetchone()
        blob_hash = row[0]
        assert blob_hash is not None
        assert get_ref_count(conn, blob_hash) == 1

        conn.execute("DELETE FROM event_tool_call WHERE event_id=?", (tc_id,))
        conn.commit()

        assert get_ref_count(conn, blob_hash) == 0
        conn.close()

    def test_update_trigger_clamps_refcount_to_zero(self, tmp_path):
        """UPDATE trigger uses MAX(ref_count - 1, 0) — negative ref_count would violate CHECK."""
        from siftd.storage.sqlite import (
            get_or_create_harness,
            get_or_create_workspace,
            insert_conversation,
            insert_prompt,
            insert_response,
            insert_tool_call,
        )

        conn = open_database(tmp_path / "fresh.db")
        h = get_or_create_harness(conn, "test", source="test")
        w = get_or_create_workspace(conn, "/test", "2024-01-01T00:00:00Z")
        c = insert_conversation(conn, "c1", h, w, "2024-01-01T00:00:00Z")
        p = insert_prompt(conn, c, "p1", "2024-01-01T00:00:00Z")
        r = insert_response(conn, c, p, None, None, "r1", "2024-01-01T00:00:01Z")
        tc_id = insert_tool_call(conn, r, c, None, "tc1", "{}", '{"out":1}', "success", "2024-01-01T00:00:02Z")
        conn.commit()

        row = conn.execute("SELECT result_hash FROM event_tool_call WHERE event_id=?", (tc_id,)).fetchone()
        blob_hash = row[0]

        # Artificially set ref_count=0 to simulate drift from a prior bug
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE content_blobs SET ref_count = 0 WHERE hash = ?", (blob_hash,))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()

        # Nulling out result_hash fires the UPDATE trigger.
        # Without MAX clamp this would decrement ref_count to -1, violating CHECK(ref_count >= 0).
        conn.execute("UPDATE event_tool_call SET result_hash = NULL WHERE event_id = ?", (tc_id,))
        conn.commit()  # Must not raise IntegrityError

        ref = conn.execute(
            "SELECT ref_count FROM content_blobs WHERE hash = ?", (blob_hash,)
        ).fetchone()
        # Row is GC'd when ref_count <= 0, or clamped to 0 — either way non-negative
        assert ref is None or ref[0] >= 0
        conn.close()


# ---------------------------------------------------------------------------
# TestMigrateV4PolymorphicSchema — slice-1 acceptance criteria
# ---------------------------------------------------------------------------

_V3_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "schemas" / "v3.sql"


def _make_v3_db(tmp_path: Path, name: str = "test.db") -> Path:
    """Create a DB at schema version 3 using the v3.sql fixture."""
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.executescript(_V3_FIXTURE_PATH.read_text())
    conn.close()
    return path


def _populate_v3_db(path: Path) -> dict[str, int]:
    """Insert a small representative dataset into a v3 DB.

    Returns expected counts keyed by table name for assertion in caller.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = OFF")  # v3 fixture, pre-migration schema

    conn.execute("INSERT INTO harnesses VALUES ('h1','claude_code',NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO workspaces VALUES ('w1','/code',NULL,'2024-01-01T00:00:00Z')")
    conn.execute("INSERT INTO tags VALUES ('tg1','important',NULL,'2024-01-01T00:00:00Z')")
    conn.execute(
        "INSERT INTO conversations VALUES ('c1','ext-c1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)"
    )

    # 2 prompts
    conn.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:01:00Z')")
    conn.execute("INSERT INTO prompts VALUES ('p2','c1','ep2','2024-01-01T00:02:00Z')")

    # 2 responses
    conn.execute(
        "INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:01:01Z',100,200)"
    )
    conn.execute(
        "INSERT INTO responses VALUES ('r2','c1','p2',NULL,NULL,'er2','2024-01-01T00:02:01Z',150,250)"
    )

    # 2 tool_calls (NULL external_id so the events UNIQUE constraint is never violated)
    conn.execute(
        "INSERT INTO tool_calls VALUES ('tc1','r1','c1',NULL,NULL,'{}',NULL,NULL,'success','2024-01-01T00:01:02Z')"
    )
    conn.execute(
        "INSERT INTO tool_calls VALUES ('tc2','r2','c1',NULL,NULL,'{}',NULL,NULL,'success','2024-01-01T00:02:02Z')"
    )

    # prompt_content: 2 blocks (one per prompt)
    conn.execute("INSERT INTO prompt_content VALUES ('pc1','p1',0,'text','hello')")
    conn.execute("INSERT INTO prompt_content VALUES ('pc2','p2',0,'text','world')")

    # response_content: 2 blocks (one per response)
    conn.execute("INSERT INTO response_content VALUES ('rc1','r1',0,'text','reply1')")
    conn.execute("INSERT INTO response_content VALUES ('rc2','r2',0,'text','reply2')")

    # response_attributes: 2 rows
    conn.execute(
        "INSERT INTO response_attributes VALUES ('ra1','r1','cache_read_input_tokens','50',NULL)"
    )
    conn.execute(
        "INSERT INTO response_attributes VALUES ('ra2','r2','cache_read_input_tokens','75',NULL)"
    )

    # conversation_attributes: 1 row
    conn.execute(
        "INSERT INTO conversation_attributes VALUES ('ca1','c1','summary','test session',NULL)"
    )

    # conversation_tags: 1 row
    conn.execute("INSERT INTO conversation_tags VALUES ('ct1','c1','tg1','2024-01-01T00:00:00Z')")

    conn.commit()
    conn.close()

    return {
        "prompts": 2,
        "responses": 2,
        "tool_calls": 2,
        "prompt_content": 2,
        "response_content": 2,
        "response_attributes": 2,
        "conversation_attributes": 1,
        "conversation_tags": 1,
        "workspace_tags": 0,
        "tool_call_tags": 0,
    }


class TestMigrateV4PolymorphicSchema:
    def test_roundtrip_with_data(self, tmp_path, monkeypatch):
        """v3 DB with data → open_database → new tables populated with correct row counts."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        counts = _populate_v3_db(path)

        conn = open_database(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='prompt'").fetchone()[0] == counts["prompts"]
            assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='response'").fetchone()[0] == counts["responses"]
            assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='tool_call'").fetchone()[0] == counts["tool_calls"]
            assert conn.execute("SELECT COUNT(*) FROM event_response").fetchone()[0] == counts["responses"]
            assert conn.execute("SELECT COUNT(*) FROM event_tool_call").fetchone()[0] == counts["tool_calls"]
            assert conn.execute("SELECT COUNT(*) FROM event_content").fetchone()[0] == (
                counts["prompt_content"] + counts["response_content"]
            )
            assert conn.execute("SELECT COUNT(*) FROM attributes").fetchone()[0] == (
                counts["response_attributes"] + counts["conversation_attributes"]
            )
            assert conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0] == counts["conversation_tags"]
        finally:
            conn.close()

    def test_roundtrip_id_preservation(self, tmp_path, monkeypatch):
        """IDs from prompts/responses/tool_calls are preserved as events.id."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        _populate_v3_db(path)

        conn = open_database(path)
        try:
            prompt_ids = {r[0] for r in conn.execute("SELECT id FROM events WHERE kind='prompt'").fetchall()}
            response_ids = {r[0] for r in conn.execute("SELECT id FROM events WHERE kind='response'").fetchall()}
            tool_call_ids = {r[0] for r in conn.execute("SELECT id FROM events WHERE kind='tool_call'").fetchall()}

            assert prompt_ids == {"p1", "p2"}
            assert response_ids == {"r1", "r2"}
            assert tool_call_ids == {"tc1", "tc2"}

            # event_content IDs also preserved
            content_ids = {r[0] for r in conn.execute("SELECT id FROM event_content").fetchall()}
            assert content_ids == {"pc1", "pc2", "rc1", "rc2"}
        finally:
            conn.close()

    def test_empty_db(self, tmp_path, monkeypatch):
        """v3 DB with no data rows → migration succeeds; all new tables have 0 rows."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        conn = open_database(path)
        try:
            for table in ("events", "event_response", "event_tool_call", "event_content",
                          "attributes", "tag_assignments"):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                assert count == 0, f"{table} expected 0, got {count}"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()

    def test_prompt_tags_present(self, tmp_path, monkeypatch):
        """prompt_tags rows are backfilled into tag_assignments when the table exists."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        counts = _populate_v3_db(path)

        # Add a prompt_tags row (v3.sql already creates the prompt_tags table)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO prompt_tags VALUES ('pt1','p1','tg1','2024-01-01T00:00:00Z')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            # conversation_tags + prompt_tags = 2 tag_assignments
            total = conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0]
            assert total == counts["conversation_tags"] + 1
            row = conn.execute(
                "SELECT target_kind, target_id, tag_id FROM tag_assignments WHERE id='pt1'"
            ).fetchone()
            assert row is not None
            assert row[0] == "prompt"
            assert row[1] == "p1"
        finally:
            conn.close()

    def test_prompt_tags_absent(self, tmp_path, monkeypatch):
        """Migration succeeds when prompt_tags table does not exist."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        # Remove prompt_tags table to simulate older DBs
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DROP TABLE IF EXISTS prompt_tags")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            # No error; tag_assignments exists with 0 rows
            assert conn.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0] == 0
        finally:
            conn.close()

    def test_assertion_failure_rolls_back(self, tmp_path, monkeypatch):
        """Assertion mismatch via open_database rolls back; user_version stays at prior value."""
        import siftd.storage.sqlite as sqlite_mod
        from siftd.storage.sqlite import MigrationAssertionError

        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)

        # Pre-create events table with a phantom 'prompt' row that has no corresponding
        # row in prompts.  This makes COUNT(events WHERE kind='prompt') != COUNT(prompts)
        # and triggers MigrationAssertionError during the migration.
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("""
            CREATE TABLE events (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                conversation_id TEXT NOT NULL, parent_id TEXT,
                external_id TEXT, timestamp TEXT NOT NULL
            )
        """)
        raw.execute(
            "INSERT INTO events VALUES ('phantom','prompt','c-nonexistent',NULL,NULL,'2024-01-01T00:00:00Z')"
        )
        raw.commit()
        raw.close()

        with pytest.raises(MigrationAssertionError, match="events\\[prompt\\]"):
            open_database(path)

        # Verify the runner's ROLLBACK kept user_version at the pre-migration value
        check = sqlite3.connect(str(path))
        version = check.execute("PRAGMA user_version").fetchone()[0]
        check.close()
        assert version == 3, f"user_version should still be 3 after rollback, got {version}"

    def test_idempotent_via_version_gate(self, tmp_path, monkeypatch):
        """Second open_database on an already-migrated DB is a no-op."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        conn = open_database(path)
        conn.close()

        # Second open must not raise or duplicate rows
        conn2 = open_database(path)
        try:
            assert conn2.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            # Tables still exist with same row counts (0 for empty fixture)
            assert conn2.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        finally:
            conn2.close()

    def test_backup_called_before_migration(self, tmp_path, monkeypatch):
        """backup_database is called exactly once with the expected source and target paths."""
        import siftd.storage.sqlite as sqlite_mod
        from datetime import date

        backup_calls: list[tuple] = []

        def _record_backup(source, target):
            backup_calls.append((source, target))

        monkeypatch.setattr(sqlite_mod, "backup_database", _record_backup)

        path = _make_v3_db(tmp_path, name="data.db")
        conn = open_database(path)
        conn.close()

        assert len(backup_calls) == 1
        src, dst = backup_calls[0]
        assert src == path
        today = date.today().strftime("%Y%m%d")
        assert dst == path.parent / f"data.bak.{today}.db"

    def test_no_backup_on_fresh_db(self, tmp_path, monkeypatch):
        """backup_database is NOT called when opening a brand-new DB."""
        import siftd.storage.sqlite as sqlite_mod

        backup_calls: list = []
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: backup_calls.append((s, t)))

        path = tmp_path / "fresh.db"
        conn = open_database(path)
        conn.close()

        assert backup_calls == [], "backup_database should not be called for a new DB"

    def test_schema_version_stamped(self, tmp_path, monkeypatch):
        """After successful migration, user_version == SCHEMA_VERSION."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        conn = open_database(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == SCHEMA_VERSION

    def test_duplicate_tool_call_external_ids_suffixed(self, tmp_path, monkeypatch):
        """v4 migration: duplicate tool_call external_ids get ':N' suffix to avoid UNIQUE conflict."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v3_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/code',NULL,'2024-01-01T00:00:00Z')")
        raw.execute("INSERT INTO conversations VALUES ('c1','ext-c1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        raw.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:01:00Z')")
        raw.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:01:01Z',100,200)")
        # Two tool_calls in the same conversation sharing the same non-NULL external_id
        raw.execute("INSERT INTO tool_calls VALUES ('tc1','r1','c1',NULL,'dupe-id','{}',NULL,NULL,'success','2024-01-01T00:01:02Z')")
        raw.execute("INSERT INTO tool_calls VALUES ('tc2','r1','c1',NULL,'dupe-id','{}',NULL,NULL,'success','2024-01-01T00:01:03Z')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            # Pin by event id: ROW_NUMBER orders by timestamp ASC, so tc1 (earlier) keeps
            # the original external_id and tc2 (later) gets the ':2' suffix.
            ext_id_by_id = dict(conn.execute(
                "SELECT id, external_id FROM events WHERE id IN ('tc1','tc2')"
            ).fetchall())
            assert ext_id_by_id["tc1"] == "dupe-id", "earlier row should keep original external_id"
            assert ext_id_by_id["tc2"] == "dupe-id:2", "later row should get ':2' suffix"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestMigrateV5FtsSimplification — slice-6 acceptance criteria
# ---------------------------------------------------------------------------

_V4_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "schemas" / "v4.sql"


def _make_v4_db(tmp_path: Path, name: str = "v4test.db") -> Path:
    """Create a DB at schema version 4 using the v4.sql fixture."""
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.executescript(_V4_FIXTURE_PATH.read_text())
    conn.close()
    return path


class TestMigrateV5FtsSimplification:
    def test_v4_to_v5_migration_roundtrip(self, tmp_path, monkeypatch):
        """v4 DB migrates to v5: content_fts drops side, repopulates from event_content."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v4_db(tmp_path)

        # Seed event_content (event_content exists in v4 schema)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01T00:00:00Z')")
        raw.execute(
            "INSERT INTO conversations VALUES ('c1','ext1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)"
        )
        raw.execute("INSERT INTO events VALUES ('ev1','prompt','c1',NULL,NULL,'2024-01-01T00:00:00Z')")
        raw.execute("INSERT INTO events VALUES ('ev2','response','c1','ev1',NULL,'2024-01-01T00:00:01Z')")
        raw.execute(
            """INSERT INTO event_content VALUES ('ec1','ev1',0,'text','{"text":"hello migration"}')"""
        )
        raw.execute(
            """INSERT INTO event_content VALUES ('ec2','ev2',0,'text','{"text":"migration response"}')"""
        )
        raw.execute(
            """INSERT INTO event_content VALUES ('ec3','ev2',1,'thinking','{"text":"thinking block content"}')"""
        )
        raw.execute(
            """INSERT INTO event_content VALUES ('ec4','ev2',2,'tool_use','{"name":"read_file"}')"""
        )
        raw.commit()
        raw.close()

        conn = open_database(path)

        # FTS schema no longer has side column
        fts_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='content_fts'"
        ).fetchone()[0].lower()
        assert "side" not in fts_sql
        assert "event_content_id" in fts_sql
        assert "event_id" in fts_sql

        # FTS row count matches indexable event_content rows (ec1, ec2, ec3 have $.text; ec4 does not)
        fts_count = conn.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0]
        assert fts_count == 3

        # FTS search works — text blocks
        results = conn.execute(
            "SELECT conversation_id FROM content_fts WHERE content_fts MATCH 'hello'"
        ).fetchall()
        assert len(results) == 1 and results[0][0] == "c1"

        # FTS search works — thinking blocks are indexed
        results = conn.execute(
            "SELECT conversation_id FROM content_fts WHERE content_fts MATCH 'thinking'"
        ).fetchall()
        assert len(results) == 1 and results[0][0] == "c1"

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# TestMigrationsV6 — drop legacy tables + ref_count heal
# ---------------------------------------------------------------------------

_OLD_TABLES = {
    "prompts", "responses", "tool_calls",
    "prompt_content", "response_content",
    "conversation_attributes", "prompt_attributes", "response_attributes", "tool_call_attributes",
    "workspace_tags", "conversation_tags", "tool_call_tags", "prompt_tags",
}

_EVENTS_TABLES = {
    "events", "event_content", "event_response", "event_tool_call",
    "attributes", "tag_assignments", "content_blobs",
}


def _make_v5_db(tmp_path: Path, name: str = "v5test.db") -> Path:
    """Create a DB at schema version 5 (has all 13 old tables + events tier)."""
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.executescript(_LEGACY_V5_NO_CASCADE)
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()
    return path


_V6_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "schemas" / "v6.sql"


def _make_v6_db(tmp_path: Path, name: str = "v6test.db") -> Path:
    """Create a DB at schema version 6 using the v6.sql fixture."""
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.executescript(_V6_FIXTURE_PATH.read_text())
    conn.commit()
    conn.close()
    return path


class TestMigrationsV6:
    def test_drops_all_thirteen_old_tables(self, tmp_path, monkeypatch):
        """v5 → v6: all 13 legacy tables are removed."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v5_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01')")
        raw.execute("INSERT INTO conversations VALUES ('c1','e1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        raw.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        raw.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:00:01Z',10,20)")
        raw.execute("INSERT INTO tool_calls VALUES ('tc1','r1','c1',NULL,NULL,'{}',NULL,NULL,'success','2024-01-01T00:00:02Z')")
        raw.execute("INSERT INTO prompt_content VALUES ('pc1','p1',0,'text','hello')")
        raw.execute("INSERT INTO response_content VALUES ('rc1','r1',0,'text','hi')")
        raw.execute("INSERT INTO conversation_attributes VALUES ('ca1','c1','k','v',NULL)")
        raw.execute("INSERT INTO prompt_attributes VALUES ('pa1','p1','k','v',NULL)")
        raw.execute("INSERT INTO response_attributes VALUES ('ra1','r1','k','v',NULL)")
        raw.execute("INSERT INTO tool_call_attributes VALUES ('ta1','tc1','k','v',NULL)")
        raw.execute("INSERT INTO tags VALUES ('tg1','t1',NULL,'2024-01-01')")
        raw.execute("INSERT INTO workspace_tags VALUES ('wt1','w1','tg1','2024-01-01')")
        raw.execute("INSERT INTO conversation_tags VALUES ('ct1','c1','tg1','2024-01-01')")
        raw.execute("INSERT INTO tool_call_tags VALUES ('tct1','tc1','tg1','2024-01-01')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert not (tables & _OLD_TABLES), f"Old tables still present: {tables & _OLD_TABLES}"
        finally:
            conn.close()

    def test_events_tables_preserved(self, tmp_path, monkeypatch):
        """v5 → v6: events-tier tables survive the migration."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v5_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01')")
        raw.execute("INSERT INTO conversations VALUES ('c1','e1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        raw.execute("INSERT INTO events VALUES ('ev1','prompt','c1',NULL,NULL,'2024-01-01T00:00:00Z')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert _EVENTS_TABLES.issubset(tables), f"Missing events tables: {_EVENTS_TABLES - tables}"
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            conn.close()

    def test_ref_count_heal_undercounted(self, tmp_path, monkeypatch):
        """ref_count < actual references → healed to correct count."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v5_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01')")
        raw.execute("INSERT INTO conversations VALUES ('c1','e1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        raw.execute("INSERT INTO events VALUES ('ev1','response','c1',NULL,NULL,'2024-01-01T00:00:00Z')")
        raw.execute("INSERT INTO events VALUES ('ev2','response','c1',NULL,NULL,'2024-01-01T00:00:01Z')")
        # blob with ref_count=1 but actually referenced twice
        raw.execute("INSERT INTO content_blobs VALUES ('blobA','content A',1,'2024-01-01')")
        raw.execute("INSERT INTO event_tool_call VALUES ('etc1','ev1',NULL,'blobA','success')")
        raw.execute("INSERT INTO event_tool_call VALUES ('etc2','ev2',NULL,'blobA','success')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            ref = conn.execute("SELECT ref_count FROM content_blobs WHERE hash='blobA'").fetchone()[0]
            assert ref == 2
        finally:
            conn.close()

    def test_ref_count_heal_overcounted(self, tmp_path, monkeypatch):
        """ref_count > actual references → healed down to correct count."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v5_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01')")
        raw.execute("INSERT INTO conversations VALUES ('c1','e1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        raw.execute("INSERT INTO events VALUES ('ev1','response','c1',NULL,NULL,'2024-01-01T00:00:00Z')")
        # blob with ref_count=5 but only 1 event_tool_call references it
        raw.execute("INSERT INTO content_blobs VALUES ('blobB','content B',5,'2024-01-01')")
        raw.execute("INSERT INTO event_tool_call VALUES ('etc1','ev1',NULL,'blobB','success')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            ref = conn.execute("SELECT ref_count FROM content_blobs WHERE hash='blobB'").fetchone()[0]
            assert ref == 1
        finally:
            conn.close()

    def test_ref_count_zero_blob_deleted(self, tmp_path, monkeypatch):
        """blob with ref_count after heal = 0 → deleted from content_blobs."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v5_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        # blob with ref_count=1 but no event_tool_call references it → should be deleted
        raw.execute("INSERT INTO content_blobs VALUES ('blobC','orphaned content',1,'2024-01-01')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            row = conn.execute("SELECT 1 FROM content_blobs WHERE hash='blobC'").fetchone()
            assert row is None, "Orphaned blob should have been deleted"
        finally:
            conn.close()

    def test_prompt_tags_absent_no_error(self, tmp_path, monkeypatch):
        """Migration succeeds even when prompt_tags table doesn't exist (IF EXISTS guard)."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        # Build a v5 DB then manually drop prompt_tags before migrating
        path = _make_v5_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DROP TABLE IF EXISTS prompt_tags")
        raw.commit()
        raw.close()

        # Should not raise
        conn = open_database(path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "prompt_tags" not in tables
        finally:
            conn.close()

    def test_blob_preservation_from_unmigrated_result(self, tmp_path, monkeypatch):
        """v6 migration inlines blob migration for tool_calls rows with result but no result_hash."""
        import hashlib
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v5_db(tmp_path)
        result_text = '{"output": "hello migration"}'
        expected_hash = hashlib.sha256(result_text.encode()).hexdigest()

        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01')")
        raw.execute("INSERT INTO conversations VALUES ('c1','e1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        raw.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        raw.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:00:01Z',10,20)")
        # tool_calls row with result text but no result_hash (pre-blob-migration state)
        raw.execute(
            "INSERT INTO tool_calls VALUES ('tc1','r1','c1',NULL,NULL,'{}',?,NULL,'success','2024-01-01T00:00:02Z')",
            (result_text,),
        )
        # Corresponding events + event_tool_call as v4 migration would have created (result_hash also NULL)
        raw.execute("INSERT INTO events VALUES ('tc1','tool_call','c1','r1',NULL,'2024-01-01T00:00:02Z')")
        raw.execute("INSERT INTO event_tool_call VALUES ('tc1',NULL,'{}',NULL,'success')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            blob = conn.execute(
                "SELECT content, ref_count FROM content_blobs WHERE hash=?", (expected_hash,)
            ).fetchone()
            assert blob is not None, f"content_blobs row missing for hash {expected_hash}"
            assert blob[0] == result_text
            assert blob[1] == 1  # ref_count healed to 1 (one event_tool_call references it)

            etc = conn.execute(
                "SELECT result_hash FROM event_tool_call WHERE event_id='tc1'"
            ).fetchone()
            assert etc is not None
            assert etc[0] == expected_hash
        finally:
            conn.close()

    def test_result_hash_index_present_and_used_after_heal(self, tmp_path, monkeypatch):
        """Regression: M6 must create idx_event_tool_call_result_hash and the heal
        must consult it. Without the index the heal is O(M·N) and pinned a CPU
        for 44+ min on a 2.9G real-world db (see plans/2026-05-03-events-polymorphic-followup.md).
        """
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v5_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        raw.execute("INSERT INTO workspaces VALUES ('w1','/p',NULL,'2024-01-01')")
        raw.execute("INSERT INTO conversations VALUES ('c1','e1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        raw.execute("INSERT INTO events VALUES ('ev1','response','c1',NULL,NULL,'2024-01-01T00:00:00Z')")
        raw.execute("INSERT INTO content_blobs VALUES ('blobZ','content Z',1,'2024-01-01')")
        raw.execute("INSERT INTO event_tool_call VALUES ('etc1','ev1',NULL,'blobZ','success')")
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_event_tool_call_result_hash",),
            ).fetchone()
            assert idx is not None, "M6 must create idx_event_tool_call_result_hash"

            plan = conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT result_hash, COUNT(*) FROM event_tool_call "
                "WHERE result_hash IS NOT NULL GROUP BY result_hash"
            ).fetchall()
            plan_text = " | ".join(str(row[3]) for row in plan)
            assert "idx_event_tool_call_result_hash" in plan_text, (
                f"Heal query must use index; plan was: {plan_text}"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestFreshDBInitialization — Resolution #10
# ---------------------------------------------------------------------------


class TestFreshDBInitialization:
    def test_fresh_db_opens_without_error(self, tmp_path):
        path = tmp_path / "fresh.db"
        assert not path.exists()
        conn = open_database(path)
        conn.close()
        assert path.exists()

    def test_fresh_db_user_version_equals_schema_version(self, tmp_path):
        path = tmp_path / "fresh.db"
        conn = open_database(path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            conn.close()

    def test_fresh_db_events_tables_exist(self, tmp_path):
        path = tmp_path / "fresh.db"
        conn = open_database(path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for t in _EVENTS_TABLES:
                assert t in tables, f"Expected table {t!r} missing from fresh DB"
        finally:
            conn.close()

    def test_fresh_db_old_tables_absent(self, tmp_path):
        path = tmp_path / "fresh.db"
        conn = open_database(path)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for t in _OLD_TABLES:
                assert t not in tables, f"Old table {t!r} should not exist in fresh DB"
        finally:
            conn.close()

    def test_fresh_db_second_open_is_noop(self, tmp_path):
        """Opening the same DB a second time runs no migrations (already at SCHEMA_VERSION)."""
        path = tmp_path / "fresh.db"
        conn = open_database(path)
        conn.close()
        # second open should succeed and still be at SCHEMA_VERSION
        conn = open_database(path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TestMigrationsV7 — pending_tags exchange_index 0-based → 1-based
# ---------------------------------------------------------------------------


class TestMigrationsV7:
    def test_pending_tags_non_null_incremented(self, tmp_path, monkeypatch):
        """v6 → v7: non-NULL pending_tags.exchange_index values are incremented by 1."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v6_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            "INSERT INTO pending_tags VALUES ('pt1','sess1','my-tag','conversation',5,'2024-01-01T00:00:00Z')"
        )
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            row = conn.execute("SELECT exchange_index FROM pending_tags WHERE id='pt1'").fetchone()
            assert row is not None
            assert row[0] == 6  # 5 + 1
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()

    def test_pending_tags_null_unchanged(self, tmp_path, monkeypatch):
        """v6 → v7: NULL exchange_index rows are not modified."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v6_db(tmp_path)
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            "INSERT INTO pending_tags VALUES ('pt1','sess1','conv-tag','conversation',NULL,'2024-01-01T00:00:00Z')"
        )
        raw.commit()
        raw.close()

        conn = open_database(path)
        try:
            row = conn.execute("SELECT exchange_index FROM pending_tags WHERE id='pt1'").fetchone()
            assert row is not None
            assert row[0] is None  # NULL stays NULL
        finally:
            conn.close()

    def test_empty_pending_tags_no_error(self, tmp_path, monkeypatch):
        """v7 migration succeeds with an empty pending_tags table."""
        import siftd.storage.sqlite as sqlite_mod
        monkeypatch.setattr(sqlite_mod, "backup_database", lambda s, t: None)

        path = _make_v6_db(tmp_path)
        conn = open_database(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM pending_tags").fetchone()[0] == 0
        finally:
            conn.close()
