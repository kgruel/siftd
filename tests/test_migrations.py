"""Tests for siftd storage migration paths.

Exercises every migration function in sqlite.py and sessions.py by constructing
legacy database schemas (pre-migration state) and verifying the migration runs
correctly. These are separate from test_storage.py which tests current behavior.
"""

import re
import sqlite3

import pytest

from siftd.storage.sqlite import SCHEMA_PATH, SCHEMA_VERSION, open_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _legacy_db(tmp_path, *, schema_sql: str, name: str = "legacy.db") -> sqlite3.Connection:
    """Create a DB with a custom schema (simulating a pre-migration state)."""
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema_sql)
    conn.commit()
    return conn


def _strip_cascade(sql: str) -> str:
    """Remove ON DELETE CASCADE/SET NULL from schema SQL to simulate pre-migration DB."""
    return re.sub(r"\s+ON DELETE (?:CASCADE|SET NULL)", "", sql)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_ddl(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row[0] if row else ""


# Pre-migration schema: current schema.sql with CASCADE stripped
_NO_CASCADE_SCHEMA = _strip_cascade(SCHEMA_PATH.read_text())


# ---------------------------------------------------------------------------
# Schema version check
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_future_version_raises(self, tmp_path):
        """DB with a higher schema version than current should raise RuntimeError."""
        path = tmp_path / "future.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="newer version"):
            open_database(path)


# ---------------------------------------------------------------------------
# _migrate_labels_to_tags
# ---------------------------------------------------------------------------


class TestMigrateLabelsToTags:
    def test_renames_tables_and_columns(self, tmp_path):
        """Legacy labels/conversation_labels/workspace_labels → tags/conversation_tags/workspace_tags."""
        from siftd.storage.sqlite import _migrate_labels_to_tags

        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE labels (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE conversation_labels (
                id TEXT PRIMARY KEY, conversation_id TEXT, label_id TEXT);
            CREATE TABLE workspace_labels (
                id TEXT PRIMARY KEY, workspace_id TEXT, label_id TEXT);
        """)
        conn.execute("INSERT INTO labels VALUES ('t1', 'bug')")
        conn.execute("INSERT INTO conversation_labels VALUES ('cl1', 'c1', 't1')")
        conn.execute("INSERT INTO workspace_labels VALUES ('wl1', 'w1', 't1')")
        conn.commit()

        _migrate_labels_to_tags(conn)

        tables = _table_names(conn)
        assert "tags" in tables and "labels" not in tables
        assert "conversation_tags" in tables and "conversation_labels" not in tables
        assert "tag_id" in _column_names(conn, "conversation_tags")
        assert "tag_id" in _column_names(conn, "workspace_tags")
        assert conn.execute("SELECT name FROM tags WHERE id='t1'").fetchone()[0] == "bug"
        conn.close()

    def test_noop_when_no_labels(self, tmp_path):
        from siftd.storage.sqlite import _migrate_labels_to_tags
        conn = _legacy_db(tmp_path, schema_sql="CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT);")
        _migrate_labels_to_tags(conn)
        assert "tags" in _table_names(conn)
        conn.close()

    def test_noop_when_already_migrated(self, tmp_path):
        from siftd.storage.sqlite import _migrate_labels_to_tags
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE labels (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT);
        """)
        _migrate_labels_to_tags(conn)
        assert "labels" in _table_names(conn)  # not dropped
        conn.close()


# ---------------------------------------------------------------------------
# Column-add migrations
# ---------------------------------------------------------------------------


class TestColumnMigrations:
    def test_add_error_column(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_error_column
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE ingested_files (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                file_hash TEXT NOT NULL, harness_id TEXT, conversation_id TEXT, ingested_at TEXT NOT NULL);
        """)
        assert "error" not in _column_names(conn, "ingested_files")
        _migrate_add_error_column(conn)
        assert "error" in _column_names(conn, "ingested_files")
        conn.close()

    def test_add_file_stat_columns(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_file_stat_columns
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE ingested_files (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                file_hash TEXT NOT NULL, harness_id TEXT, conversation_id TEXT, ingested_at TEXT NOT NULL, error TEXT);
        """)
        assert "file_mtime" not in _column_names(conn, "ingested_files")
        _migrate_add_file_stat_columns(conn)
        cols = _column_names(conn, "ingested_files")
        assert "file_mtime" in cols and "file_size" in cols
        conn.close()

    def test_add_branch_column(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_branch_column
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE conversations (id TEXT PRIMARY KEY, external_id TEXT NOT NULL,
                harness_id TEXT, workspace_id TEXT, started_at TEXT NOT NULL, ended_at TEXT);
        """)
        assert "branch" not in _column_names(conn, "conversations")
        _migrate_add_branch_column(conn)
        assert "branch" in _column_names(conn, "conversations")
        conn.close()


# ---------------------------------------------------------------------------
# _migrate_add_cascade_deletes
# ---------------------------------------------------------------------------


class TestMigrateAddCascadeDeletes:
    def test_adds_cascade_and_preserves_data(self, tmp_path):
        """Migration adds ON DELETE CASCADE to all FK constraints and preserves data."""
        from siftd.storage.sqlite import _migrate_add_cascade_deletes

        conn = _legacy_db(tmp_path, schema_sql=_NO_CASCADE_SCHEMA)
        assert "ON DELETE CASCADE" not in _table_ddl(conn, "prompts")

        # Insert test data
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

        # CASCADE present on all child tables
        for table in ["prompts", "responses", "tool_calls", "prompt_content",
                       "response_content", "conversation_tags", "ingested_files"]:
            assert "ON DELETE CASCADE" in _table_ddl(conn, table), f"{table} missing CASCADE"

        # Data preserved
        assert conn.execute("SELECT content FROM prompt_content WHERE id='pc1'").fetchone()[0] == "hello"
        assert conn.execute("SELECT content FROM response_content WHERE id='rc1'").fetchone()[0] == "world"

        # CASCADE actually works
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM conversations WHERE id='c1'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
        conn.close()

    def test_noop_when_already_migrated(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = open_database(tmp_path / "fresh.db")
        assert "ON DELETE CASCADE" in _table_ddl(conn, "prompts")
        _migrate_add_cascade_deletes(conn)  # should not raise
        assert "ON DELETE CASCADE" in _table_ddl(conn, "prompts")
        conn.close()

    def test_noop_when_no_prompts_table(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = _legacy_db(tmp_path, schema_sql="CREATE TABLE x (id TEXT PRIMARY KEY);")
        _migrate_add_cascade_deletes(conn)
        assert "x" in _table_names(conn)  # unchanged
        conn.close()


# ---------------------------------------------------------------------------
# Sessions: last_seen_at migration
# ---------------------------------------------------------------------------


class TestSessionsLastSeenAtMigration:
    def test_adds_last_seen_at(self, tmp_path):
        """Sessions table without last_seen_at gets column added and backfilled."""
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

        assert "last_seen_at" in _column_names(conn, "active_sessions")
        assert conn.execute("SELECT last_seen_at FROM active_sessions WHERE id='s1'").fetchone()[0] == "2024-01-01T00:00:00Z"
        conn.close()


# ---------------------------------------------------------------------------
# Full open_database migration integration
# ---------------------------------------------------------------------------


class TestOpenDatabaseMigrations:
    def test_full_migration_path(self, tmp_path):
        """open_database on a legacy DB (no CASCADE, no branch, no error/stat columns) runs all migrations."""
        path = tmp_path / "legacy_full.db"
        # Build legacy schema: strip CASCADE and remove specific columns (line-level removal)
        lines = _NO_CASCADE_SCHEMA.split("\n")
        filtered = [ln for ln in lines if not re.search(
            r"^\s*(branch\s+TEXT|error\s+TEXT|file_mtime\s+REAL|file_size\s+INTEGER)", ln)]
        # Fix trailing commas before closing parens
        schema = re.sub(r",(\s*\n\s*\))", r"\1", "\n".join(filtered))
        conn = sqlite3.connect(path)
        conn.executescript(schema)
        conn.commit()
        conn.close()

        # open_database should run all migrations
        conn = open_database(path)
        assert "branch" in _column_names(conn, "conversations")
        assert "error" in _column_names(conn, "ingested_files")
        assert "ON DELETE CASCADE" in _table_ddl(conn, "prompts")
        conn.close()
