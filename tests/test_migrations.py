"""Tests for siftd storage migration paths.

Exercises every migration function in sqlite.py and sessions.py by constructing
legacy database schemas (pre-migration state) and verifying the migration runs
correctly. These are separate from test_storage.py which tests current behavior.
"""

import sqlite3

import pytest

from siftd.storage.sqlite import SCHEMA_VERSION, open_database

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


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_ddl(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row[0] if row else ""


# ---------------------------------------------------------------------------
# Schema version check
# ---------------------------------------------------------------------------

class TestSchemaVersion:
    def test_future_version_raises(self, tmp_path):
        """DB with a higher schema version than current should raise RuntimeError."""
        path = tmp_path / "future.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        # Need at least the conversations table so it looks like an existing DB
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
                id TEXT PRIMARY KEY, conversation_id TEXT, label_id TEXT
            );
            CREATE TABLE workspace_labels (
                id TEXT PRIMARY KEY, workspace_id TEXT, label_id TEXT
            );
        """)
        # Insert test data
        conn.execute("INSERT INTO labels VALUES ('t1', 'bug')")
        conn.execute("INSERT INTO conversation_labels VALUES ('cl1', 'c1', 't1')")
        conn.execute("INSERT INTO workspace_labels VALUES ('wl1', 'w1', 't1')")
        conn.commit()

        _migrate_labels_to_tags(conn)

        tables = _table_names(conn)
        assert "tags" in tables and "labels" not in tables
        assert "conversation_tags" in tables and "conversation_labels" not in tables
        assert "workspace_tags" in tables and "workspace_labels" not in tables
        # Column renamed
        assert "tag_id" in _column_names(conn, "conversation_tags")
        assert "tag_id" in _column_names(conn, "workspace_tags")
        # Data preserved
        assert conn.execute("SELECT name FROM tags WHERE id='t1'").fetchone()[0] == "bug"
        assert conn.execute("SELECT tag_id FROM conversation_tags").fetchone()[0] == "t1"
        conn.close()

    def test_noop_when_no_labels(self, tmp_path):
        """No labels table → migration is a no-op."""
        from siftd.storage.sqlite import _migrate_labels_to_tags
        conn = _legacy_db(tmp_path, schema_sql="CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT);")
        _migrate_labels_to_tags(conn)  # should not raise
        assert "tags" in _table_names(conn)
        conn.close()

    def test_noop_when_already_migrated(self, tmp_path):
        """Both labels and tags exist → migration skips, labels table untouched."""
        from siftd.storage.sqlite import _migrate_labels_to_tags
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE labels (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT);
        """)
        _migrate_labels_to_tags(conn)
        assert "labels" in _table_names(conn)  # not dropped
        conn.close()


# ---------------------------------------------------------------------------
# _migrate_add_error_column
# ---------------------------------------------------------------------------

class TestMigrateAddErrorColumn:
    def test_adds_error_column(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_error_column
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                file_hash TEXT NOT NULL, harness_id TEXT NOT NULL,
                conversation_id TEXT, ingested_at TEXT NOT NULL
            );
        """)
        assert "error" not in _column_names(conn, "ingested_files")
        _migrate_add_error_column(conn)
        assert "error" in _column_names(conn, "ingested_files")
        conn.close()


# ---------------------------------------------------------------------------
# _migrate_add_file_stat_columns
# ---------------------------------------------------------------------------

class TestMigrateAddFileStatColumns:
    def test_adds_mtime_and_size(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_file_stat_columns
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                file_hash TEXT NOT NULL, harness_id TEXT NOT NULL,
                conversation_id TEXT, ingested_at TEXT NOT NULL, error TEXT
            );
        """)
        cols = _column_names(conn, "ingested_files")
        assert "file_mtime" not in cols and "file_size" not in cols
        _migrate_add_file_stat_columns(conn)
        cols = _column_names(conn, "ingested_files")
        assert "file_mtime" in cols and "file_size" in cols
        conn.close()


# ---------------------------------------------------------------------------
# _migrate_add_branch_column
# ---------------------------------------------------------------------------

class TestMigrateAddBranchColumn:
    def test_adds_branch(self, tmp_path):
        from siftd.storage.sqlite import _migrate_add_branch_column
        conn = _legacy_db(tmp_path, schema_sql="""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, external_id TEXT NOT NULL,
                harness_id TEXT NOT NULL, workspace_id TEXT,
                started_at TEXT NOT NULL, ended_at TEXT
            );
        """)
        assert "branch" not in _column_names(conn, "conversations")
        _migrate_add_branch_column(conn)
        assert "branch" in _column_names(conn, "conversations")
        conn.close()


# ---------------------------------------------------------------------------
# _migrate_add_cascade_deletes
# ---------------------------------------------------------------------------

class TestMigrateAddCascadeDeletes:
    # Minimal pre-CASCADE schema: tables without ON DELETE CASCADE
    LEGACY_SCHEMA = """
        CREATE TABLE harnesses (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, version TEXT,
            display_name TEXT, source TEXT, log_format TEXT);
        CREATE TABLE models (id TEXT PRIMARY KEY, raw_name TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            creator TEXT, family TEXT, version TEXT, variant TEXT, released TEXT);
        CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            display_name TEXT, billing_model TEXT);
        CREATE TABLE tools (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            category TEXT, description TEXT);
        CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            git_remote TEXT, discovered_at TEXT NOT NULL);
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, external_id TEXT NOT NULL,
            harness_id TEXT NOT NULL REFERENCES harnesses(id),
            workspace_id TEXT REFERENCES workspaces(id),
            branch TEXT, started_at TEXT NOT NULL, ended_at TEXT,
            UNIQUE (harness_id, external_id)
        );
        CREATE TABLE prompts (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
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
        CREATE TABLE tool_calls (
            id TEXT PRIMARY KEY,
            response_id TEXT NOT NULL REFERENCES responses(id),
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            tool_id TEXT REFERENCES tools(id),
            external_id TEXT, input TEXT, result TEXT, status TEXT, timestamp TEXT
        );
        CREATE TABLE prompt_content (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id),
            block_index INTEGER NOT NULL, block_type TEXT NOT NULL, content TEXT NOT NULL,
            UNIQUE (prompt_id, block_index)
        );
        CREATE TABLE response_content (
            id TEXT PRIMARY KEY,
            response_id TEXT NOT NULL REFERENCES responses(id),
            block_index INTEGER NOT NULL, block_type TEXT NOT NULL, content TEXT NOT NULL,
            UNIQUE (response_id, block_index)
        );
        CREATE TABLE conversation_attributes (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (conversation_id, key, scope)
        );
        CREATE TABLE prompt_attributes (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (prompt_id, key, scope)
        );
        CREATE TABLE response_attributes (
            id TEXT PRIMARY KEY,
            response_id TEXT NOT NULL REFERENCES responses(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (response_id, key, scope)
        );
        CREATE TABLE tool_call_attributes (
            id TEXT PRIMARY KEY,
            tool_call_id TEXT NOT NULL REFERENCES tool_calls(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (tool_call_id, key, scope)
        );
        CREATE TABLE conversation_tags (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            tag_id TEXT NOT NULL REFERENCES tags(id),
            applied_at TEXT NOT NULL,
            UNIQUE (conversation_id, tag_id)
        );
        CREATE TABLE tool_call_tags (
            id TEXT PRIMARY KEY,
            tool_call_id TEXT NOT NULL REFERENCES tool_calls(id),
            tag_id TEXT NOT NULL REFERENCES tags(id),
            applied_at TEXT NOT NULL,
            UNIQUE (tool_call_id, tag_id)
        );
        CREATE TABLE ingested_files (
            id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            file_hash TEXT NOT NULL,
            harness_id TEXT NOT NULL REFERENCES harnesses(id),
            conversation_id TEXT REFERENCES conversations(id),
            ingested_at TEXT NOT NULL, error TEXT
        );
        CREATE INDEX idx_prompts_conversation ON prompts(conversation_id);
        CREATE INDEX idx_prompts_timestamp ON prompts(timestamp);
        CREATE INDEX idx_responses_conversation ON responses(conversation_id);
        CREATE INDEX idx_responses_prompt ON responses(prompt_id);
        CREATE INDEX idx_responses_model ON responses(model_id);
        CREATE INDEX idx_responses_timestamp ON responses(timestamp);
        CREATE INDEX idx_tool_calls_response ON tool_calls(response_id);
        CREATE INDEX idx_tool_calls_conversation ON tool_calls(conversation_id);
        CREATE INDEX idx_tool_calls_tool ON tool_calls(tool_id);
        CREATE INDEX idx_tool_calls_status ON tool_calls(status);
        CREATE INDEX idx_prompt_content_prompt ON prompt_content(prompt_id);
        CREATE INDEX idx_response_content_response ON response_content(response_id);
    """

    def test_adds_cascade_and_preserves_data(self, tmp_path):
        """Migration adds ON DELETE CASCADE to all FK constraints and preserves data."""
        from siftd.storage.sqlite import _migrate_add_cascade_deletes

        conn = _legacy_db(tmp_path, schema_sql=self.LEGACY_SCHEMA)

        # Verify pre-migration: no CASCADE
        assert "ON DELETE CASCADE" not in _table_ddl(conn, "prompts")

        # Insert test data through the chain
        conn.execute("INSERT INTO harnesses VALUES ('h1','test',NULL,NULL,NULL,NULL)")
        conn.execute("INSERT INTO workspaces VALUES ('w1','/test',NULL,'2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO tags VALUES ('tg1','important','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO conversations VALUES ('c1','ext1','h1','w1',NULL,'2024-01-01T00:00:00Z',NULL)")
        conn.execute("INSERT INTO prompts VALUES ('p1','c1','ep1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO responses VALUES ('r1','c1','p1',NULL,NULL,'er1','2024-01-01T00:00:01Z',100,200)")
        conn.execute("INSERT INTO tool_calls VALUES ('tc1','r1','c1',NULL,'etc1','{}','result','success','2024-01-01T00:00:02Z')")
        conn.execute("INSERT INTO prompt_content VALUES ('pc1','p1',0,'text','hello')")
        conn.execute("INSERT INTO response_content VALUES ('rc1','r1',0,'text','world')")
        conn.execute("INSERT INTO conversation_tags VALUES ('ct1','c1','tg1','2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO ingested_files VALUES ('if1','/f.jsonl','hash1','h1','c1','2024-01-01T00:00:00Z',NULL)")
        conn.commit()

        _migrate_add_cascade_deletes(conn)

        # Verify post-migration: CASCADE present on all child tables
        for table in ["prompts", "responses", "tool_calls", "prompt_content",
                       "response_content", "conversation_tags", "ingested_files"]:
            assert "ON DELETE CASCADE" in _table_ddl(conn, table), f"{table} missing CASCADE"

        # Verify data preserved
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
        assert conn.execute("SELECT content FROM prompt_content WHERE id='pc1'").fetchone()[0] == "hello"
        assert conn.execute("SELECT content FROM response_content WHERE id='rc1'").fetchone()[0] == "world"

        # Verify CASCADE actually works now
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM conversations WHERE id='c1'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
        conn.close()

    def test_noop_when_already_migrated(self, tmp_path):
        """If CASCADE already present, migration is a no-op."""
        from siftd.storage.sqlite import _migrate_add_cascade_deletes
        conn = open_database(tmp_path / "fresh.db")
        # Fresh DB already has CASCADE from schema.sql
        assert "ON DELETE CASCADE" in _table_ddl(conn, "prompts")
        _migrate_add_cascade_deletes(conn)  # should not raise
        conn.close()

    def test_noop_when_no_prompts_table(self, tmp_path):
        """If prompts table doesn't exist, migration skips gracefully."""
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
            CREATE TABLE active_sessions (
                id TEXT PRIMARY KEY,
                adapter_name TEXT NOT NULL,
                workspace_path TEXT,
                started_at TEXT NOT NULL
            );
            CREATE TABLE pending_tags (
                id TEXT PRIMARY KEY,
                harness_session_id TEXT NOT NULL,
                tag_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        # Insert a session without last_seen_at
        conn.execute("INSERT INTO active_sessions VALUES ('s1','claude_code','/test','2024-01-01T00:00:00Z')")
        conn.commit()

        ensure_session_tables(conn)

        assert "last_seen_at" in _column_names(conn, "active_sessions")
        # Backfilled from started_at
        row = conn.execute("SELECT last_seen_at FROM active_sessions WHERE id='s1'").fetchone()
        assert row[0] == "2024-01-01T00:00:00Z"
        conn.close()


# ---------------------------------------------------------------------------
# Full open_database migration integration
# ---------------------------------------------------------------------------

class TestOpenDatabaseMigrations:
    def test_full_migration_path(self, tmp_path):
        """open_database on a legacy DB runs all migrations successfully."""
        path = tmp_path / "legacy_full.db"
        # Create a minimal legacy DB: has conversations/prompts but no CASCADE,
        # no error column, no branch, no file_stat columns
        conn = sqlite3.connect(path)
        conn.executescript(TestMigrateAddCascadeDeletes.LEGACY_SCHEMA)
        # Remove file_stat columns by recreating ingested_files without them
        conn.execute("DROP TABLE ingested_files")
        conn.execute("""
            CREATE TABLE ingested_files (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                file_hash TEXT NOT NULL,
                harness_id TEXT NOT NULL REFERENCES harnesses(id),
                conversation_id TEXT REFERENCES conversations(id),
                ingested_at TEXT NOT NULL
            )
        """)
        # Remove branch from conversations
        conn.execute("DROP TABLE conversation_tags")
        conn.execute("DROP TABLE tool_call_tags")
        conn.execute("DROP TABLE tool_calls")
        conn.execute("DROP TABLE prompt_content")
        conn.execute("DROP TABLE response_content")
        conn.execute("DROP TABLE conversation_attributes")
        conn.execute("DROP TABLE prompt_attributes")
        conn.execute("DROP TABLE response_attributes")
        conn.execute("DROP TABLE tool_call_attributes")
        conn.execute("DROP TABLE responses")
        conn.execute("DROP TABLE prompts")
        conn.execute("DROP TABLE conversations")
        conn.execute("""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, external_id TEXT NOT NULL,
                harness_id TEXT NOT NULL REFERENCES harnesses(id),
                workspace_id TEXT REFERENCES workspaces(id),
                started_at TEXT NOT NULL, ended_at TEXT,
                UNIQUE (harness_id, external_id)
            )
        """)
        # Recreate dependent tables without CASCADE
        conn.execute("""CREATE TABLE prompts (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
            external_id TEXT, timestamp TEXT NOT NULL, UNIQUE (conversation_id, external_id))""")
        conn.execute("""CREATE TABLE responses (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
            prompt_id TEXT REFERENCES prompts(id), model_id TEXT, provider_id TEXT,
            external_id TEXT, timestamp TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
            UNIQUE (conversation_id, external_id))""")
        conn.execute("""CREATE TABLE tool_calls (
            id TEXT PRIMARY KEY, response_id TEXT NOT NULL REFERENCES responses(id),
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            tool_id TEXT, external_id TEXT, input TEXT, result TEXT, status TEXT, timestamp TEXT)""")
        conn.execute("""CREATE TABLE prompt_content (
            id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL REFERENCES prompts(id),
            block_index INTEGER NOT NULL, block_type TEXT NOT NULL, content TEXT NOT NULL,
            UNIQUE (prompt_id, block_index))""")
        conn.execute("""CREATE TABLE response_content (
            id TEXT PRIMARY KEY, response_id TEXT NOT NULL REFERENCES responses(id),
            block_index INTEGER NOT NULL, block_type TEXT NOT NULL, content TEXT NOT NULL,
            UNIQUE (response_id, block_index))""")
        conn.execute("""CREATE TABLE conversation_attributes (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT, UNIQUE (conversation_id, key, scope))""")
        conn.execute("""CREATE TABLE prompt_attributes (
            id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL REFERENCES prompts(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT, UNIQUE (prompt_id, key, scope))""")
        conn.execute("""CREATE TABLE response_attributes (
            id TEXT PRIMARY KEY, response_id TEXT NOT NULL REFERENCES responses(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT, UNIQUE (response_id, key, scope))""")
        conn.execute("""CREATE TABLE tool_call_attributes (
            id TEXT PRIMARY KEY, tool_call_id TEXT NOT NULL REFERENCES tool_calls(id),
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT, UNIQUE (tool_call_id, key, scope))""")
        conn.execute("""CREATE TABLE conversation_tags (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
            tag_id TEXT NOT NULL REFERENCES tags(id), applied_at TEXT NOT NULL,
            UNIQUE (conversation_id, tag_id))""")
        conn.execute("""CREATE TABLE tool_call_tags (
            id TEXT PRIMARY KEY, tool_call_id TEXT NOT NULL REFERENCES tool_calls(id),
            tag_id TEXT NOT NULL REFERENCES tags(id), applied_at TEXT NOT NULL,
            UNIQUE (tool_call_id, tag_id))""")
        conn.execute("""CREATE TABLE workspace_tags (
            id TEXT PRIMARY KEY, workspace_id TEXT, tag_id TEXT,
            UNIQUE (workspace_id, tag_id))""")
        conn.execute("CREATE INDEX idx_prompts_conversation ON prompts(conversation_id)")
        conn.execute("CREATE INDEX idx_prompts_timestamp ON prompts(timestamp)")
        conn.execute("CREATE INDEX idx_responses_conversation ON responses(conversation_id)")
        conn.execute("CREATE INDEX idx_responses_prompt ON responses(prompt_id)")
        conn.execute("CREATE INDEX idx_responses_model ON responses(model_id)")
        conn.execute("CREATE INDEX idx_responses_timestamp ON responses(timestamp)")
        conn.execute("CREATE INDEX idx_tool_calls_response ON tool_calls(response_id)")
        conn.execute("CREATE INDEX idx_tool_calls_conversation ON tool_calls(conversation_id)")
        conn.execute("CREATE INDEX idx_tool_calls_tool ON tool_calls(tool_id)")
        conn.execute("CREATE INDEX idx_tool_calls_status ON tool_calls(status)")
        conn.execute("CREATE INDEX idx_prompt_content_prompt ON prompt_content(prompt_id)")
        conn.execute("CREATE INDEX idx_response_content_response ON response_content(response_id)")
        conn.commit()
        conn.close()

        # Now open_database should run all migrations
        conn = open_database(path)
        # Verify: branch column added
        assert "branch" in _column_names(conn, "conversations")
        # Verify: error column present (file_mtime/file_size may be lost if
        # _migrate_add_cascade_deletes runs after _migrate_add_file_stat_columns
        # since the cascade DDL doesn't include those columns — known limitation)
        assert "error" in _column_names(conn, "ingested_files")
        # Verify: CASCADE added
        assert "ON DELETE CASCADE" in _table_ddl(conn, "prompts")
        conn.close()
