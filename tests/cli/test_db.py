"""Tests for siftd db namespace commands."""

import io
import sqlite3
from pathlib import Path
from types import SimpleNamespace

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


def _remote_cfg(path="/remote.db", host=None, name="r"):
    return {
        "name": name,
        "host": host,
        "path": path,
        "last_push": None,
        "last_pull": None,
    }


class TestDbRemoteSubcommands:
    def test_remote_add_host_path(self, monkeypatch, capsys):
        called = []
        monkeypatch.setattr("siftd.config.set_sync_remote", lambda n, h, p: called.append((n, h, p)))
        rc = main(["db", "remote", "add", "team", "host:/data/team.db"])
        assert rc == 0
        assert called == [("team", "host", "/data/team.db")]

    def test_remote_add_local_path(self, monkeypatch):
        called = []
        monkeypatch.setattr("siftd.config.set_sync_remote", lambda n, h, p: called.append((n, h, p)))
        rc = main(["db", "remote", "add", "nas", "/mnt/team.db"])
        assert rc == 0
        assert called == [("nas", None, "/mnt/team.db")]

    def test_remote_list_empty(self, monkeypatch, capsys):
        monkeypatch.setattr("siftd.config.get_sync_remotes", lambda: [])
        rc = main(["db", "remote", "list"])
        assert rc == 0
        assert "No remotes configured" in capsys.readouterr().out

    def test_remote_list_with_entries(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "siftd.config.get_sync_remotes",
            lambda: [{
                "name": "team",
                "host": "box",
                "path": "/data/team.db",
                "last_push": "2024-01-01T00:00:00+00:00",
                "last_pull": "2024-01-02T00:00:00+00:00",
            }],
        )
        rc = main(["db", "remote", "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "team" in out and "last push" in out and "last pull" in out

    def test_remote_remove(self, monkeypatch, capsys):
        monkeypatch.setattr("siftd.config.remove_sync_remote", lambda n: True)
        assert main(["db", "remote", "remove", "team"]) == 0
        monkeypatch.setattr("siftd.config.remove_sync_remote", lambda n: False)
        assert main(["db", "remote", "remove", "team"]) == 1


class TestDbPushPull:
    def test_push_remote_missing(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        rc = main(["--db", str(test_db), "db", "push", "missing"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_pull_remote_missing(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: None)
        rc = main(["--db", str(test_db), "db", "pull", "missing"])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_push_db_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: _remote_cfg())
        rc = main(["--db", str(tmp_path / "missing.db"), "db", "push", "r"])
        assert rc == 1
        assert "Database not found" in capsys.readouterr().out

    def test_push_sync_error(self, test_db, monkeypatch, capsys):
        from siftd.api.sync import SyncError

        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: _remote_cfg())
        monkeypatch.setattr("siftd.api.sync.sync_push", lambda **kw: (_ for _ in ()).throw(SyncError("boom")))
        rc = main(["--db", str(test_db), "db", "push", "r"])
        assert rc == 1
        assert "Push failed" in capsys.readouterr().err

    def test_push_dry_run_and_success(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: _remote_cfg())
        monkeypatch.setattr(
            "siftd.api.sync.sync_push",
            lambda **kw: SimpleNamespace(conversations=2, size_bytes=2048, dry_run=True, remote_existed=True),
        )
        rc = main(["--db", str(test_db), "db", "push", "r", "--dry-run"])
        assert rc == 0
        assert "Would push 2 conversations" in capsys.readouterr().out

        monkeypatch.setattr(
            "siftd.api.sync.sync_push",
            lambda **kw: SimpleNamespace(conversations=2, size_bytes=2048, dry_run=False, remote_existed=False),
        )
        rc = main(["--db", str(test_db), "db", "push", "r"])
        assert rc == 0
        assert "new remote database" in capsys.readouterr().out

    def test_pull_sync_error_empty_dry_run_and_success(self, test_db, monkeypatch, capsys):
        from siftd.api.sync import SyncError

        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: _remote_cfg())
        monkeypatch.setattr("siftd.api.sync.sync_pull", lambda **kw: (_ for _ in ()).throw(SyncError("boom")))
        rc = main(["--db", str(test_db), "db", "pull", "r"])
        assert rc == 1
        assert "Pull failed" in capsys.readouterr().err

        monkeypatch.setattr(
            "siftd.api.sync.sync_pull",
            lambda **kw: SimpleNamespace(conversations=0, size_bytes=0, dry_run=False),
        )
        rc = main(["--db", str(test_db), "db", "pull", "r"])
        assert rc == 0
        assert "Nothing new to pull" in capsys.readouterr().out

        monkeypatch.setattr(
            "siftd.api.sync.sync_pull",
            lambda **kw: SimpleNamespace(conversations=3, size_bytes=1024, dry_run=True),
        )
        rc = main(["--db", str(test_db), "db", "pull", "r", "--dry-run"])
        assert rc == 0
        assert "Would pull 3 conversations" in capsys.readouterr().out

        monkeypatch.setattr(
            "siftd.api.sync.sync_pull",
            lambda **kw: SimpleNamespace(conversations=3, size_bytes=1024, dry_run=False),
        )
        rc = main(["--db", str(test_db), "db", "pull", "r"])
        assert rc == 0
        assert "Pulled 3 conversations" in capsys.readouterr().out

    def test_push_file_not_found_and_zero(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr("siftd.config.get_sync_remote", lambda n: _remote_cfg())
        monkeypatch.setattr("siftd.api.sync.sync_push", lambda **kw: (_ for _ in ()).throw(FileNotFoundError("no db")))
        rc = main(["--db", str(test_db), "db", "push", "r"])
        assert rc == 1
        assert "no db" in capsys.readouterr().out

        monkeypatch.setattr(
            "siftd.api.sync.sync_push",
            lambda **kw: SimpleNamespace(conversations=0, size_bytes=0, dry_run=False, remote_existed=True),
        )
        rc = main(["--db", str(test_db), "db", "push", "r"])
        assert rc == 0
        assert "Nothing new to push" in capsys.readouterr().out


class _FakeStdin:
    def __init__(self, data=b"", *, is_tty=False):
        self.buffer = io.BytesIO(data)
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


class _FakeStdout:
    def __init__(self, *, is_tty=False):
        self.buffer = io.BytesIO()
        self._is_tty = is_tty
        self.out = []

    def isatty(self):
        return self._is_tty

    def write(self, s):
        self.out.append(s)
        return len(s)

    def flush(self):
        pass


class TestDbSendReceive:
    def test_send_missing_db_and_tty_stdout(self, tmp_path, monkeypatch, capsys):
        rc = main(["--db", str(tmp_path / "missing.db"), "db", "send"])
        assert rc == 1
        assert "Database not found" in capsys.readouterr().err

        fake_out = _FakeStdout(is_tty=True)
        monkeypatch.setattr("sys.stdout", fake_out)
        rc = main(["--db", str(tmp_path / "x.db"), "db", "send"])
        assert rc == 1

    def test_send_zero_success_and_slice_error(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr(
            "siftd.api.slice.slice_database",
            lambda **kw: {"conversations": 0, "size_bytes": 0},
        )
        rc = main(["--db", str(test_db), "db", "send"])
        assert rc == 0
        assert '"conversations": 0' in capsys.readouterr().err

        def _missing(**kw):
            raise FileNotFoundError("bad source")

        monkeypatch.setattr("siftd.api.slice.slice_database", _missing)
        rc = main(["--db", str(test_db), "db", "send"])
        assert rc == 1
        assert "bad source" in capsys.readouterr().err

    def test_send_streams_binary_when_nonzero(self, test_db, monkeypatch):
        fake_out = _FakeStdout(is_tty=False)
        monkeypatch.setattr("sys.stdout", fake_out)

        def _slice(**kw):
            Path(kw["target_path"]).write_bytes(b"sqlite")
            return {"conversations": 1, "size_bytes": 6}

        monkeypatch.setattr("siftd.api.slice.slice_database", _slice)
        monkeypatch.setattr("shutil.copyfileobj", lambda src, dst: dst.write(src.read()))
        assert main(["--db", str(test_db), "db", "send", "--since", "2024-01-01", "-w", "proj"]) == 0
        assert fake_out.buffer.getvalue() == b"sqlite"

    def test_receive_tty_empty_success_and_errors(self, test_db, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", _FakeStdin(is_tty=True))
        rc = main(["--db", str(test_db), "db", "receive"])
        assert rc == 1
        assert "No data on stdin" in capsys.readouterr().err

        monkeypatch.setattr("sys.stdin", _FakeStdin(b""))
        rc = main(["--db", str(test_db), "db", "receive"])
        assert rc == 1
        assert "Empty input" in capsys.readouterr().err

        monkeypatch.setattr("sys.stdin", _FakeStdin(b"sqlite-bytes"))
        monkeypatch.setattr("siftd.api.receive.receive_database", lambda *a, **k: {"status": "merged", "conversations": 1})
        rc = main(["--db", str(test_db), "db", "receive", "--no-fts"])
        assert rc == 0
        assert '"status": "merged"' in capsys.readouterr().out

        monkeypatch.setattr("sys.stdin", _FakeStdin(b"sqlite-bytes"))
        monkeypatch.setattr("siftd.api.receive.receive_database", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad db")))
        rc = main(["--db", str(test_db), "db", "receive"])
        assert rc == 1
        assert "bad db" in capsys.readouterr().err

        monkeypatch.setattr("sys.stdin", _FakeStdin(b"sqlite-bytes"))
        monkeypatch.setattr("siftd.api.receive.receive_database", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        rc = main(["--db", str(test_db), "db", "receive"])
        assert rc == 1
        assert '"error_type": "database_locked"' in capsys.readouterr().err
