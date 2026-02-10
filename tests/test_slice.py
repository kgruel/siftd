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
    result = slice_database(test_db, target, tags=["test-slice-tag"])
    assert result["conversations"] == 1
