"""Tests for siftd db namespace commands."""

import sqlite3

from siftd.cli import main


def test_db_help():
    """siftd db prints help."""
    rc = main(["db"])
    assert rc == 0


def test_db_info(test_db, capsys):
    """siftd db info shows database metadata."""
    rc = main(["--db", str(test_db), "db", "info"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Path:" in out
    assert "Size:" in out
    assert "Schema version:" in out
    assert "FTS5 index:" in out


def test_db_stats(test_db, capsys):
    """siftd db stats delegates to status."""
    rc = main(["--db", str(test_db), "db", "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Conversations:" in out


def test_db_workspaces(test_db, capsys):
    """siftd db workspaces lists workspaces."""
    rc = main(["--db", str(test_db), "db", "workspaces"])
    assert rc == 0


def test_db_path(capsys):
    """siftd db path shows XDG paths."""
    rc = main(["db", "path"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Data directory:" in out


def test_db_vacuum(test_db, capsys):
    """siftd db vacuum compacts without error."""
    rc = main(["--db", str(test_db), "db", "vacuum"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Before:" in out
    assert "After:" in out


def test_db_backup(test_db, tmp_path, capsys):
    """siftd db backup creates a valid SQLite copy."""
    target = tmp_path / "backup.db"
    rc = main(["--db", str(test_db), "db", "backup", str(target)])
    assert rc == 0
    assert target.exists()

    # Verify it's a valid SQLite DB
    conn = sqlite3.connect(str(target))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()
    assert "conversations" in tables


def test_db_backup_refuses_overwrite(test_db, tmp_path, capsys):
    """siftd db backup refuses to overwrite without --force."""
    target = tmp_path / "backup.db"
    target.write_text("existing")
    rc = main(["--db", str(test_db), "db", "backup", str(target)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_db_backup_force_overwrite(test_db, tmp_path):
    """siftd db backup --force overwrites existing file."""
    target = tmp_path / "backup.db"
    target.write_text("existing")
    rc = main(["--db", str(test_db), "db", "backup", str(target), "--force"])
    assert rc == 0


def test_db_restore_roundtrip(test_db, tmp_path, capsys):
    """Backup then restore produces working database."""
    backup_path = tmp_path / "backup.db"
    main(["--db", str(test_db), "db", "backup", str(backup_path)])

    # Restore to a new location
    new_db = tmp_path / "restored.db"
    rc = main(["--db", str(new_db), "db", "restore", str(backup_path)])
    assert rc == 0

    # Query the restored DB
    rc = main(["--db", str(new_db), "query"])
    assert rc == 0


def test_db_restore_validates_sqlite(test_db, tmp_path, capsys):
    """siftd db restore rejects non-SQLite files."""
    bad_file = tmp_path / "not-a-db.txt"
    bad_file.write_text("this is not sqlite")
    rc = main(["--db", str(tmp_path / "out.db"), "db", "restore", str(bad_file)])
    assert rc == 1
    assert "Not a valid SQLite" in capsys.readouterr().err


def test_db_restore_refuses_overwrite(test_db, tmp_path, capsys):
    """siftd db restore refuses to overwrite without --force."""
    backup_path = tmp_path / "backup.db"
    main(["--db", str(test_db), "db", "backup", str(backup_path)])

    # test_db already exists, restore should refuse
    rc = main(["--db", str(test_db), "db", "restore", str(backup_path)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
