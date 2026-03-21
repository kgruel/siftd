"""Tests for siftd storage migration paths.

Exercises every migration function in sqlite.py and sessions.py by constructing
legacy database schemas (pre-migration state) and verifying migrations work.
"""

import re
import sqlite3

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


def _tables(conn): return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
def _cols(conn, t): return {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
def _ddl(conn, t):
    r = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
    return r[0] if r else ""


_NO_CASCADE = _strip_cascade(SCHEMA_PATH.read_text())


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
        conn = _legacy_db(tmp_path, schema_sql=_NO_CASCADE)
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

    def test_skips_missing_tables(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = _legacy_db(tmp_path, schema_sql=_NO_CASCADE)
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
        assert "ON DELETE CASCADE" in _ddl(conn, "prompts")
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


class TestOpenDatabaseMigrations:
    def test_full_migration_path(self, tmp_path):
        """open_database on a legacy DB runs all migrations."""
        path = tmp_path / "legacy_full.db"
        lines = _NO_CASCADE.split("\n")
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
        assert "ON DELETE CASCADE" in _ddl(conn, "prompts")
        conn.close()
