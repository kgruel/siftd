"""Tests for siftd db slice — filtered database export."""

import sqlite3

import pytest

from siftd.api.slice import slice_database
from siftd.cli import main


def test_slice_no_filters_copies_all(test_db):
    """Slice with no filters copies all conversations."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        result = slice_database(test_db, target)

        assert result["conversations"] == 2
        assert target.exists()
        assert result["size_bytes"] > 0

        # Verify conversations exist in target
        conn = sqlite3.connect(str(target))
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 2


def test_slice_by_workspace(test_db):
    """Slice by workspace filters correctly."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        result = slice_database(test_db, target, workspace="test/project")

        # test_db has workspace /test/project with 2 conversations
        assert result["conversations"] == 2


def test_slice_by_nonexistent_workspace(test_db):
    """Slice with non-matching workspace produces empty but valid DB."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        result = slice_database(test_db, target, workspace="nonexistent")

        assert result["conversations"] == 0
        assert target.exists()

        # DB should be valid with schema but no data
        conn = sqlite3.connect(str(target))
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        conn.close()
        assert count == 0


def test_slice_fts_works_in_target(test_db):
    """FTS5 search works in the sliced database."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        slice_database(test_db, target)

        conn = sqlite3.connect(str(target))
        conn.row_factory = sqlite3.Row
        # FTS should have been rebuilt
        results = conn.execute(
            "SELECT COUNT(*) FROM content_fts WHERE content_fts MATCH 'Python'"
        ).fetchone()[0]
        conn.close()
        assert results >= 1


def test_slice_no_fts_skips_rebuild(test_db):
    """Slice with --no-fts produces empty FTS index."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        slice_database(test_db, target, rebuild_fts=False)

        conn = sqlite3.connect(str(target))
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0]
        conn.close()
        assert count == 0


def test_slice_foreign_key_check(test_db):
    """PRAGMA foreign_key_check passes on sliced output."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        slice_database(test_db, target)

        conn = sqlite3.connect(str(target))
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert violations == []


def test_slice_ephemeral_tables_empty(test_db):
    """Ephemeral tables (ingested_files) are empty in sliced output."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        slice_database(test_db, target)

        conn = sqlite3.connect(str(target))
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) FROM ingested_files").fetchone()[0]
        conn.close()
        assert count == 0


def test_slice_preserves_vocabulary(test_db):
    """Slice copies referenced vocabulary entities (harnesses, models)."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sliced.db"
        slice_database(test_db, target)

        conn = sqlite3.connect(str(target))
        conn.row_factory = sqlite3.Row

        harnesses = conn.execute("SELECT COUNT(*) FROM harnesses").fetchone()[0]
        models = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        workspaces = conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        conn.close()

        assert harnesses >= 1
        assert models >= 1
        assert workspaces >= 1


def test_slice_cli_command(test_db, tmp_path, capsys):
    """siftd db slice works via CLI."""
    target = tmp_path / "sliced.db"
    rc = main(["--db", str(test_db), "db", "slice", str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Sliced 2 conversation(s)" in out


def test_slice_cli_refuses_overwrite(test_db, tmp_path, capsys):
    """siftd db slice refuses to overwrite without --force."""
    target = tmp_path / "sliced.db"
    target.write_text("existing")
    rc = main(["--db", str(test_db), "db", "slice", str(target)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_slice_with_content_blobs_fk(test_db_with_tool_tags, tmp_path):
    """Slice copies content_blobs before tool_calls (FK ordering)."""
    target = tmp_path / "sliced.db"
    result = slice_database(test_db_with_tool_tags, target)

    assert result["conversations"] == 3

    # Verify FK integrity with foreign_keys enforcement
    conn = sqlite3.connect(str(target))
    conn.execute("PRAGMA foreign_keys = ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    assert violations == []


def test_slice_migrated_column_order(tmp_path):
    """Slice handles source DBs with pre-migration column order (ALTER TABLE appended columns).

    Tests that a legacy v1 DB (conversations.branch and tool_calls.result_hash as
    last columns due to ALTER TABLE) migrates cleanly to v6 and the slice target
    has correct data in event_tool_call. The original tool_calls column-order
    corruption bug is now impossible (v4 migration uses explicit column names and
    v6 drops tool_calls), but conversations.branch ordering is still exercised.
    """
    from siftd.storage.sqlite import _ulid

    source = tmp_path / "migrated.db"
    conn = sqlite3.connect(str(source))
    conn.execute("PRAGMA foreign_keys = ON")

    # Create tables with pre-migration column order (no branch, no result_hash)
    conn.executescript("""
        CREATE TABLE harnesses (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            version TEXT, display_name TEXT, source TEXT, log_format TEXT
        );
        CREATE TABLE models (
            id TEXT PRIMARY KEY, raw_name TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, creator TEXT, family TEXT, version TEXT,
            variant TEXT, released TEXT
        );
        CREATE TABLE providers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            display_name TEXT, billing_model TEXT
        );
        CREATE TABLE tools (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            category TEXT, description TEXT
        );
        CREATE TABLE tool_aliases (
            id TEXT PRIMARY KEY, raw_name TEXT NOT NULL,
            harness_id TEXT NOT NULL REFERENCES harnesses(id),
            tool_id TEXT NOT NULL REFERENCES tools(id),
            UNIQUE (raw_name, harness_id)
        );
        CREATE TABLE pricing (
            id TEXT PRIMARY KEY,
            model_id TEXT NOT NULL REFERENCES models(id),
            provider_id TEXT NOT NULL REFERENCES providers(id),
            input_per_mtok REAL, output_per_mtok REAL,
            UNIQUE (model_id, provider_id)
        );
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            git_remote TEXT, discovered_at TEXT NOT NULL
        );
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, external_id TEXT NOT NULL,
            harness_id TEXT NOT NULL REFERENCES harnesses(id),
            workspace_id TEXT REFERENCES workspaces(id),
            started_at TEXT NOT NULL, ended_at TEXT,
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
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            prompt_id TEXT REFERENCES prompts(id) ON DELETE CASCADE,
            model_id TEXT REFERENCES models(id),
            provider_id TEXT REFERENCES providers(id),
            external_id TEXT, timestamp TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER,
            UNIQUE (conversation_id, external_id)
        );
        CREATE TABLE tool_calls (
            id TEXT PRIMARY KEY,
            response_id TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            tool_id TEXT REFERENCES tools(id),
            external_id TEXT, input TEXT, result TEXT,
            status TEXT, timestamp TEXT
        );
        CREATE TABLE prompt_content (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            block_index INTEGER NOT NULL, block_type TEXT NOT NULL,
            content TEXT NOT NULL, UNIQUE (prompt_id, block_index)
        );
        CREATE TABLE response_content (
            id TEXT PRIMARY KEY,
            response_id TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
            block_index INTEGER NOT NULL, block_type TEXT NOT NULL,
            content TEXT NOT NULL, UNIQUE (response_id, block_index)
        );
        CREATE TABLE content_blobs (
            hash TEXT PRIMARY KEY, content TEXT NOT NULL,
            ref_count INTEGER DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE conversation_attributes (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (conversation_id, key, scope)
        );
        CREATE TABLE prompt_attributes (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (prompt_id, key, scope)
        );
        CREATE TABLE response_attributes (
            id TEXT PRIMARY KEY,
            response_id TEXT NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (response_id, key, scope)
        );
        CREATE TABLE tool_call_attributes (
            id TEXT PRIMARY KEY,
            tool_call_id TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
            key TEXT NOT NULL, value TEXT NOT NULL, scope TEXT,
            UNIQUE (tool_call_id, key, scope)
        );
        CREATE TABLE tags (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            description TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE workspace_tags (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            applied_at TEXT NOT NULL, UNIQUE (workspace_id, tag_id)
        );
        CREATE TABLE conversation_tags (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            applied_at TEXT NOT NULL, UNIQUE (conversation_id, tag_id)
        );
        CREATE TABLE tool_call_tags (
            id TEXT PRIMARY KEY,
            tool_call_id TEXT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            applied_at TEXT NOT NULL, UNIQUE (tool_call_id, tag_id)
        );
        CREATE TABLE ingested_files (
            id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            file_hash TEXT NOT NULL,
            harness_id TEXT NOT NULL REFERENCES harnesses(id),
            conversation_id TEXT REFERENCES conversations(id),
            ingested_at TEXT NOT NULL, error TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
            text_content, event_content_id UNINDEXED,
            event_id UNINDEXED, conversation_id UNINDEXED
        );
    """)

    # Simulate migrations: ALTER TABLE adds columns at the END
    conn.execute("ALTER TABLE conversations ADD COLUMN branch TEXT")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN result_hash TEXT REFERENCES content_blobs(hash)")
    conn.execute("PRAGMA user_version = 1")

    # Insert test data with tool_calls that have result_hash and status
    h_id = _ulid()
    w_id = _ulid()
    m_id = _ulid()
    t_id = _ulid()
    conv_id = _ulid()
    p_id = _ulid()
    r_id = _ulid()
    tc_id = _ulid()
    blob_hash = "abc123deadbeef"

    conn.execute("INSERT INTO harnesses VALUES (?, 'test', NULL, NULL, NULL, NULL)", (h_id,))
    conn.execute("INSERT INTO workspaces VALUES (?, '/test', NULL, '2024-01-01')", (w_id,))
    conn.execute("INSERT INTO models VALUES (?, 'test-model', 'test', NULL, NULL, NULL, NULL, NULL)", (m_id,))
    conn.execute("INSERT INTO tools VALUES (?, 'shell.execute', 'shell', NULL)", (t_id,))
    # conversations: migrated order is (id, external_id, harness_id, workspace_id, started_at, ended_at, branch)
    conn.execute(
        "INSERT INTO conversations VALUES (?, 'ext1', ?, ?, '2024-01-15T10:00:00Z', NULL, 'main')",
        (conv_id, h_id, w_id),
    )
    conn.execute("INSERT INTO prompts VALUES (?, ?, 'p1', '2024-01-15T10:00:00Z')", (p_id, conv_id))
    conn.execute(
        "INSERT INTO prompt_content VALUES (?, ?, 0, 'text', 'Hello')",
        (_ulid(), p_id),
    )
    conn.execute(
        "INSERT INTO responses VALUES (?, ?, ?, ?, NULL, 'r1', '2024-01-15T10:00:01Z', 100, 50)",
        (r_id, conv_id, p_id, m_id),
    )
    conn.execute("INSERT INTO content_blobs VALUES (?, 'blob content', 1, '2024-01-15')", (blob_hash,))
    # tool_calls: migrated order is (..., status, timestamp, result_hash)
    conn.execute(
        "INSERT INTO tool_calls VALUES (?, ?, ?, ?, NULL, '{}', NULL, 'success', '2024-01-15T10:00:01Z', ?)",
        (tc_id, r_id, conv_id, t_id, blob_hash),
    )
    conn.commit()
    conn.close()

    # Slice should work despite column order mismatch
    target = tmp_path / "sliced.db"
    result = slice_database(source, target, rebuild_fts=False)

    assert result["conversations"] == 1

    # Verify data landed in correct columns
    tgt = sqlite3.connect(str(target))
    tgt.row_factory = sqlite3.Row

    conv = tgt.execute("SELECT * FROM conversations").fetchone()
    assert conv["branch"] == "main"
    assert conv["started_at"] == "2024-01-15T10:00:00Z"

    # After migration, tool_call data lives in event_tool_call (not tool_calls)
    tc = tgt.execute(
        "SELECT etc.status, etc.result_hash FROM event_tool_call etc"
        " JOIN events e ON e.id = etc.event_id WHERE e.kind='tool_call'"
    ).fetchone()
    assert tc is not None
    assert tc["status"] == "success"
    assert tc["result_hash"] == blob_hash

    violations = tgt.execute("PRAGMA foreign_key_check").fetchall()
    tgt.close()
    assert violations == []


def test_slice_by_tags(test_db, tmp_path):
    """Slice by tags filters correctly."""
    from siftd.storage.sqlite import open_database
    from siftd.storage.tags import apply_tag, get_or_create_tag

    # Tag one conversation
    conn = open_database(test_db)
    conv_id = conn.execute("SELECT id FROM conversations LIMIT 1").fetchone()["id"]
    tag_id = get_or_create_tag(conn, "test-slice-tag")
    apply_tag(conn, "conversation", conv_id, tag_id)
    conn.commit()
    conn.close()

    target = tmp_path / "sliced.db"
    result = slice_database(test_db, target, tag=["test-slice-tag"])
    assert result["conversations"] == 1
